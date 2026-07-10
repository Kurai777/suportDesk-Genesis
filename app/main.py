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
from pathlib import Path

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pgvector.psycopg import register_vector_async

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.busca_web import BuscaWebClient
from app.claude_client import ClaudeClient
from app.config import Settings, get_settings
from app.freshdesk import FreshdeskClient
from app.models import (
    EMPRESA_DESCONHECIDA,
    ParInspecao,
    Requester,
    TesteRequest,
    TesteResposta,
    TicketFreshdesk,
    WebhookFreshdesk,
)
from app.pipeline import IdempotenciaRepository, Inspecao, inspecionar, processar
from app.rag import RagRepository, RagService, Similar, VoyageClient
from app.visao import VisaoClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=15.0)  # reaproveitado; fechado no shutdown
    app.state.voyage = VoyageClient(settings)
    app.state.claude = ClaudeClient(settings)
    app.state.visao = VisaoClient(settings)  # leitura de imagens dos chamados (ADR-023)
    # Único cliente reaproveitado entre chamados: mantém o cache de busca web em memória.
    app.state.busca_web = BuscaWebClient()
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
            busca_web=app.state.busca_web,
            visao=app.state.visao,
        )
    except Exception:
        logger.exception("Erro não tratado no pipeline do ticket %s.", ticket_id)
    finally:
        await conn.close()


# --- Interface de teste local (ADR-019) ------------------------------------
# Porta de entrada VISUAL para o MESMO pipeline (via `inspecionar`), SEM efeitos: não
# escreve no Freshdesk nem envia WhatsApp. Gated por INTERFACE_TESTE_ATIVA (off por padrão).

# Página servida em GET /teste. HTML em arquivo próprio (fora do .py) para não misturar
# marcação com código nem ser lintado como Python.
_PAGINA_TESTE = (Path(__file__).with_name("teste.html")).read_text(encoding="utf-8")


def _teste_ativo(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.interface_teste_ativa:
        raise HTTPException(status_code=404, detail="interface de teste desativada")
    return settings


def _ticket_de_teste(texto: str, empresa: str | None) -> TicketFreshdesk:
    """TicketFreshdesk sintético a partir do texto colado (id=0, sem responsável)."""
    return TicketFreshdesk(
        id=0,
        subject="",
        description_text=texto,
        priority="media",
        status=2,
        requester=Requester(name="(teste)"),
        empresa=(empresa or "").strip() or EMPRESA_DESCONHECIDA,
        responder_id=None,
    )


def _par_para_modelo(p: Similar) -> ParInspecao:
    return ParInspecao(
        fonte=p.fonte,
        titulo=p.titulo,
        ticket_id=p.ticket_id,
        empresa=p.empresa,
        distancia=p.distancia,
        problema=p.problema,
        solucao=p.solucao,
    )


def _para_resposta(insp: Inspecao, empresa: str) -> TesteResposta:
    r = insp.resposta
    return TesteResposta(
        empresa=empresa,
        problema=insp.problema,
        query=insp.query,
        decisao=insp.decisao.value,
        encontrou_solucao=r.encontrou_solucao,
        confianca=r.confianca,
        pedido_operacional=r.pedido_operacional,
        resposta_cliente=r.resposta_cliente,
        resumo_para_responsavel=r.resumo_para_responsavel,
        urgencia=r.urgencia,
        via_web=insp.via_web,
        nota=insp.nota,
        whatsapp=insp.whatsapp,
        pares=[_par_para_modelo(p) for p in insp.pares],
        pares_web=[_par_para_modelo(p) for p in insp.pares_web],
    )


async def _inspecao_do_texto(app: FastAPI, texto: str, empresa: str | None) -> Inspecao:
    """Roda o MIOLO (`inspecionar`) para o texto colado, com conexão própria ao banco.
    SEM I/O de saída: `inspecionar` não recebe Freshdesk/WhatsApp."""
    settings: Settings = app.state.settings
    ticket = _ticket_de_teste(texto, empresa)
    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        rag_service = RagService(app.state.voyage, RagRepository(conn))
        return await inspecionar(
            ticket,
            settings=settings,
            rag_service=rag_service,
            claude=app.state.claude,
            busca_web=app.state.busca_web,
        )
    finally:
        await conn.close()


@app.get("/teste", response_class=HTMLResponse)
async def teste_pagina(request: Request) -> HTMLResponse:
    _teste_ativo(request)
    return HTMLResponse(_PAGINA_TESTE)


@app.post("/teste/processar")
async def teste_processar(payload: TesteRequest, request: Request) -> TesteResposta:
    _teste_ativo(request)
    empresa = (payload.empresa or "").strip() or EMPRESA_DESCONHECIDA
    insp = await _inspecao_do_texto(request.app, payload.texto, payload.empresa)
    return _para_resposta(insp, empresa)
