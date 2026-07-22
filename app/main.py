"""FastAPI: webhook do Freshdesk. Responde 200 na hora e processa em background.

Fase 1 copiloto: valida o segredo, faz o parse mínimo (só ticket_id) e dispara o
pipeline via BackgroundTasks. Clientes de longa vida (httpx, Voyage, Claude) são
criados no startup e reaproveitados; a conexão com o Postgres é aberta por tarefa
(uma query por vez — evita compartilhar conexão entre tarefas concorrentes).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
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
    ImagemTeste,
    ParInspecao,
    Requester,
    TesteRequest,
    TesteResposta,
    TicketFreshdesk,
    WebhookFreshdesk,
)
from app.pipeline import (
    _MAX_IMAGENS,
    IdempotenciaRepository,
    Inspecao,
    concatenar_transcricoes,
    inspecionar,
    processar,
)
from app.portal_service import PortalService
from app.portal_sessao import SessaoStore
from app.portal_totvs import PortalTotvsClient
from app.rag import RagRepository, RagService, Similar, VoyageClient
from app.visao import VisaoClient
from app.whatsapp import InboxWhatsApp, WhatsAppClient, parse_evento_evolution

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
    app.state.whatsapp_inbox = InboxWhatsApp()  # mensagens recebidas (relay de token, ADR-026)
    # Portal do Cliente TOTVS (ADR-026): busca no ESCALAR, só se ligado. O token vem do armazém
    # (o refresher, out-of-band, minta e grava); aqui só LEMOS + buscamos via httpx.
    if settings.portal_totvs_ativo:
        _store = SessaoStore(settings.portal_sessao_arquivo)
        app.state.portal_service = PortalService(
            PortalTotvsClient(settings, client=app.state.http), _store.ler
        )
    else:
        app.state.portal_service = None
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


@app.post("/webhook/whatsapp", status_code=200)
async def webhook_whatsapp(
    payload: dict,
    request: Request,
    x_webhook_secret: str = Header(default=""),
    secret: str = Query(default=""),
) -> dict:
    """Recebe eventos da Evolution (mensagens do grupo) — base do relay de token 2FA (ADR-026).

    Autentica por segredo: header `x-webhook-secret` OU `?secret=` na URL (a Evolution posta na
    URL configurada, então o query param sempre funciona). Só registra mensagens de texto de
    TERCEIROS (ignora as próprias e eventos que não são de mensagem).
    """
    settings: Settings = request.app.state.settings
    recebido = x_webhook_secret or secret
    if not _secret_valido(recebido, settings.whatsapp_webhook_secret):
        raise HTTPException(status_code=401, detail="segredo inválido")
    msg = parse_evento_evolution(payload)
    if msg is None or msg.de_mim or not msg.texto.strip():
        return {"status": "ignorado"}
    request.app.state.whatsapp_inbox.registrar(msg)
    logger.info(
        "[WhatsApp IN] %s de %s (%s): %s",
        "grupo" if msg.eh_grupo else "contato",
        msg.remetente_nome or "?",
        msg.remetente_jid,
        msg.texto[:200],
    )
    return {"status": "recebido"}


@app.get("/webhook/whatsapp/recentes")
async def whatsapp_recentes(
    request: Request,
    x_webhook_secret: str = Header(default=""),
    secret: str = Query(default=""),
    n: int = 10,
) -> dict:
    """Inspeção das últimas mensagens recebidas (mesmo segredo do webhook). Ferramenta de teste."""
    settings: Settings = request.app.state.settings
    if not _secret_valido(x_webhook_secret or secret, settings.whatsapp_webhook_secret):
        raise HTTPException(status_code=401, detail="segredo inválido")
    msgs = request.app.state.whatsapp_inbox.recentes(n)
    return {"total": len(msgs), "recentes": [asdict(m) for m in msgs]}


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
            portal_service=app.state.portal_service,
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
        alcada_admin=r.alcada_admin,
        tipo_alcada=r.tipo_alcada,
        resposta_cliente=r.resposta_cliente,
        resumo_para_responsavel=r.resumo_para_responsavel,
        urgencia=r.urgencia,
        via_web=insp.via_web,
        auto_elegivel=insp.auto_elegivel,
        query_web=insp.query_web,
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


async def _transcrever_enviadas(app: FastAPI, imagens: list[ImagemTeste]) -> list[str]:
    """Transcreve os prints enviados na interface (ADR-025), BEST-EFFORT.

    Espelha o webhook (`_incorporar_imagens`): respeita `LEITURA_IMAGENS_ATIVA` e o teto
    `_MAX_IMAGENS`, e reusa o MESMO VisaoClient. Base64 inválido ou falha na transcrição de
    uma imagem é ignorado — não derruba a inspeção. Fonte diferente do webhook (bytes vêm da
    tela, não de download do Freshdesk), então a decodificação mora aqui.
    """
    settings: Settings = app.state.settings
    visao: VisaoClient | None = getattr(app.state, "visao", None)
    if visao is None or not settings.leitura_imagens_ativa:
        return []

    trechos: list[str] = []
    for img in imagens[:_MAX_IMAGENS]:
        try:
            dados = base64.b64decode(img.dados_base64, validate=True)
            texto = await visao.transcrever(dados, img.content_type)
        except (binascii.Error, ValueError):
            logger.warning("Imagem enviada com base64 inválido — ignorada.")
            continue
        except Exception:
            logger.exception("Falha ao transcrever imagem enviada — ignorada.")
            continue
        if texto.strip():
            trechos.append(texto.strip())
    return trechos


@app.get("/teste", response_class=HTMLResponse)
async def teste_pagina(request: Request) -> HTMLResponse:
    _teste_ativo(request)
    return HTMLResponse(_PAGINA_TESTE)


@app.post("/teste/processar")
async def teste_processar(payload: TesteRequest, request: Request) -> TesteResposta:
    _teste_ativo(request)
    empresa = (payload.empresa or "").strip() or EMPRESA_DESCONHECIDA
    # Prints anexados: transcritos e concatenados ANTES da busca, como no webhook (ADR-023/025).
    trechos = await _transcrever_enviadas(request.app, payload.imagens)
    texto = concatenar_transcricoes(payload.texto, trechos)
    insp = await _inspecao_do_texto(request.app, texto, payload.empresa)
    return _para_resposta(insp, empresa)
