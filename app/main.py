"""FastAPI: webhook do Freshdesk. Responde 200 na hora e processa em background.

Fase 1 copiloto: valida o segredo, faz o parse mínimo (só ticket_id) e dispara o
pipeline via BackgroundTasks. Clientes de longa vida (httpx, Voyage, Claude) são
criados no startup e reaproveitados; a conexão com o Postgres é aberta por tarefa
(uma query por vez — evita compartilhar conexão entre tarefas concorrentes).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pgvector.psycopg import register_vector_async

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.claude_client import ClaudeClient
from app.config import Settings, get_settings
from app.freshdesk import FreshdeskClient
from app.models import WebhookFreshdesk
from app.pipeline import IdempotenciaRepository, processar
from app.rag import RagRepository, RagService, VoyageClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=15.0)  # reaproveitado; fechado no shutdown
    app.state.voyage = VoyageClient(settings)
    app.state.claude = ClaudeClient(settings)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Suporte TOTVS IA", lifespan=lifespan)


def _secret_valido(recebido: str, esperado: str) -> bool:
    """Comparação em tempo constante; segredo vazio nunca é válido."""
    return bool(esperado) and hmac.compare_digest(recebido, esperado)


@app.post("/webhook/freshdesk", status_code=200)
async def webhook_freshdesk(
    payload: WebhookFreshdesk,
    background: BackgroundTasks,
    request: Request,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    settings: Settings = request.app.state.settings
    if not _secret_valido(x_webhook_secret, settings.freshdesk_webhook_secret):
        raise HTTPException(status_code=401, detail="segredo inválido")
    # Responde 200 na hora; processa depois, fora do ciclo de resposta do Freshdesk.
    background.add_task(_rodar_pipeline, request.app, payload.ticket_id)
    return {"status": "aceito", "ticket_id": payload.ticket_id}


async def _rodar_pipeline(app: FastAPI, ticket_id: int) -> None:
    settings: Settings = app.state.settings
    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        freshdesk = FreshdeskClient(settings, client=app.state.http)
        whatsapp = WhatsAppClient(settings, client=app.state.http)
        rag_service = RagService(app.state.voyage, RagRepository(conn))
        idempotencia = IdempotenciaRepository(conn)
        await processar(
            ticket_id,
            settings=settings,
            idempotencia=idempotencia,
            freshdesk=freshdesk,
            rag_service=rag_service,
            claude=app.state.claude,
            whatsapp=whatsapp,
        )
    except Exception:
        logger.exception("Erro não tratado no pipeline do ticket %s.", ticket_id)
    finally:
        await conn.close()
