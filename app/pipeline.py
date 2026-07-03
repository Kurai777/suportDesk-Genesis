"""Pipeline (maestro) — orquestra o processamento de um chamado. Assíncrono.

Ordem (ADR-009): idempotência → Freshdesk (nota interna + atribuição) → WhatsApp.
A decisão "resolvido × escalar" é uma FUNÇÃO PURA, sem I/O (Padrões de Engenharia).
Se o miolo (ler chamado / buscar / gerar / decidir) falhar, um fallback seguro garante
que o chamado NUNCA fique marcado como processado sem nenhuma ação para um humano.
"""

from __future__ import annotations

import logging
from enum import StrEnum

import psycopg

from app.claude_client import ClaudeClient
from app.config import Settings
from app.freshdesk import FreshdeskClient
from app.models import EMPRESA_DESCONHECIDA, RespostaIA, ResultadoChamado, TicketFreshdesk
from app.rag import RagService
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

_NIVEL_CONFIANCA = {"baixa": 0, "media": 1, "alta": 2}

NOTA_FALLBACK = "⚠️ IA indisponível no momento — chamado encaminhado para análise manual."


class Decisao(StrEnum):
    RESOLVIDO = "resolvido"
    ESCALAR = "escalar"


# --- decisão (função PURA, sem I/O) ----------------------------------------


def decidir(resultado: ResultadoChamado, confianca_minima: str) -> Decisao:
    """Resolver quando encontrou_solucao=true E confiança >= mínimo; senão, escalar."""
    r = resultado.resposta
    if r.encontrou_solucao and _atende_minimo(r.confianca, confianca_minima):
        return Decisao.RESOLVIDO
    return Decisao.ESCALAR


def _atende_minimo(confianca: str, minimo: str) -> bool:
    # Ordem: alta > media > baixa. Mínimo desconhecido => nada atende (escala, seguro).
    return _NIVEL_CONFIANCA.get(confianca, -1) >= _NIVEL_CONFIANCA.get(
        minimo, len(_NIVEL_CONFIANCA)
    )


# --- textos das notificações (puros) ---------------------------------------


def _montar_problema(ticket: TicketFreshdesk) -> str:
    return f"{ticket.subject}\n\n{ticket.description_text}".strip()


def _nota(decisao: Decisao, r: RespostaIA) -> str:
    if decisao is Decisao.RESOLVIDO:
        return (
            f"🤖 Rascunho gerado por IA (revisar antes de enviar). "
            f"Confiança: {r.confianca}.\n\n{r.resposta_cliente}"
        )
    return (
        f"⚠️ IA não encontrou solução na base. Requer análise manual.\n\n"
        f"Resumo: {r.resumo_para_responsavel}"
    )


def _whatsapp(decisao: Decisao, ticket_id: int, empresa: str) -> str:
    if decisao is Decisao.RESOLVIDO:
        return (
            f"✅ Chamado #{ticket_id} da {empresa} — "
            f"rascunho pronto no Freshdesk para revisão."
        )
    return (
        f"🔴 Chamado #{ticket_id} da {empresa} — não encontrei solução na base do "
        f"TOTVS. Recomendo olhar pessoalmente."
    )


# --- idempotência (I/O fino) -----------------------------------------------


class IdempotenciaRepository:
    """Marca o chamado como processado (INSERT ... ON CONFLICT DO NOTHING)."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def marcar_em_processamento(self, ticket_id: int) -> bool:
        """Retorna True se INSERIU (primeira vez); False se já existia (reentrega)."""
        cur = await self._conn.execute(
            """
            INSERT INTO chamado_processado (ticket_id)
            VALUES (%(ticket_id)s)
            ON CONFLICT (ticket_id) DO NOTHING
            """,
            {"ticket_id": ticket_id},
        )
        return cur.rowcount == 1


# --- orquestração ----------------------------------------------------------


async def processar(
    ticket_id: int,
    *,
    settings: Settings,
    idempotencia: IdempotenciaRepository,
    freshdesk: FreshdeskClient,
    rag_service: RagService,
    claude: ClaudeClient,
    whatsapp: WhatsAppClient,
) -> None:
    # 1. Idempotência — reentrega do mesmo ticket é ignorada.
    if not await idempotencia.marcar_em_processamento(ticket_id):
        logger.info("Ticket %s já processado — ignorando reentrega.", ticket_id)
        return

    ticket: TicketFreshdesk | None = None
    try:
        # 2. Ler o chamado. 3. Buscar contexto. 4. Gerar. 5. Decidir.
        ticket = await freshdesk.get_ticket(ticket_id)
        problema = _montar_problema(ticket)
        pares = await rag_service.buscar(problema)
        resposta = await claude.gerar_resposta(problema, pares)
        resultado = ResultadoChamado(
            ticket_id=ticket_id, empresa=ticket.empresa, resposta=resposta
        )
        decisao = decidir(resultado, settings.confianca_minima)
    except Exception:
        logger.exception("Falha no miolo do pipeline (ticket %s) — fallback.", ticket_id)
        await _fallback_seguro(ticket_id, ticket, freshdesk, whatsapp, settings)
        return

    # 6. Ação no Freshdesk (nunca resposta pública — Fase 1 copiloto).
    await freshdesk.criar_nota_interna(ticket_id, _nota(decisao, resposta))
    if ticket.responder_id is not None:
        await freshdesk.atribuir(ticket_id, ticket.responder_id)

    # 7. WhatsApp por último (melhor esforço).
    telefone = settings.telefone_responsavel(ticket.responder_id)
    await whatsapp.enviar(telefone, _whatsapp(decisao, ticket_id, ticket.empresa))


async def _fallback_seguro(
    ticket_id: int,
    ticket: TicketFreshdesk | None,
    freshdesk: FreshdeskClient,
    whatsapp: WhatsAppClient,
    settings: Settings,
) -> None:
    """Garante ação humana mesmo se o miolo falhou: nota + atribuição + WhatsApp."""
    responder_id = ticket.responder_id if ticket else None
    empresa = ticket.empresa if ticket else EMPRESA_DESCONHECIDA
    try:
        await freshdesk.criar_nota_interna(ticket_id, NOTA_FALLBACK)
        if responder_id is not None:
            await freshdesk.atribuir(ticket_id, responder_id)
    except Exception:
        logger.exception("Fallback: falha ao registrar no Freshdesk (ticket %s).", ticket_id)
    # WhatsApp sempre (melhor esforço, não levanta) — o humano precisa ser avisado.
    telefone = settings.telefone_responsavel(responder_id)
    await whatsapp.enviar(telefone, _whatsapp(Decisao.ESCALAR, ticket_id, empresa))
