"""Busca no Portal do Cliente TOTVS como fonte de pares problema→solução (ADR-026).

Fecha a integração do Portal no pipeline: dado o texto de busca (palavra-chave), consulta a
API do Portal (`PortalTotvsClient`) e devolve pares `Similar` (`fonte='portal_totvs'`) — os
chamados resolvidos do parceiro com a TOTVS — que o Claude usa para rascunhar, do MESMO jeito
que os trechos da busca web.

**Sessão desacoplada:** o service NÃO faz login (browser). Recebe um `provedor_sessao` que
devolve a `SessaoPortal` atual (token). Assim o browser (login 2FA) roda FORA do caminho do
request — um refresher out-of-band minta o token e o service só faz httpx. Se o token expirar
(401/403), o service pede um novo ao provedor uma vez. BEST-EFFORT: qualquer falha → sem pares
(o pipeline segue para a web/escala).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from app.portal_totvs import PortalTotvsClient, SessaoPortal, TicketPortal
from app.rag import Similar

logger = logging.getLogger(__name__)

# Devolve a sessão atual do Portal (token) ou None se não há uma válida.
ProvedorSessao = Callable[[], Awaitable[SessaoPortal | None]]


def _par_portal(ticket: TicketPortal, solucao: str) -> Similar:
    """Um chamado resolvido do Portal vira um par problema→solução para o Claude."""
    problema = f"{ticket.subject}\n{ticket.description}".strip() or ticket.subject
    prod = ticket.produto or "?"
    mod = ticket.modulo or "?"
    return Similar(
        ticket_id=ticket.ticket_id,
        problema=problema,
        solucao=solucao,
        empresa=None,  # empresa-origem NÃO entra no texto (isolamento cross-cliente, ADR-026)
        distancia=1.0,  # sentinela: fonte externa; o guardrail de distância não se aplica
        fonte="portal_totvs",
        titulo=f"Chamado resolvido no Portal TOTVS #{ticket.ticket_id} ({prod}/{mod})",
    )


class PortalService:
    """Busca no Portal e devolve pares problema→solução. Gerencia a sessão (cache + refresh)."""

    def __init__(
        self,
        client: PortalTotvsClient,
        provedor_sessao: ProvedorSessao,
        *,
        top_k: int = 3,
    ) -> None:
        self._client = client
        self._provedor = provedor_sessao
        self._top_k = top_k
        self._cache_sessao: SessaoPortal | None = None

    async def _garantir_sessao(self, *, forcar: bool = False) -> SessaoPortal | None:
        if forcar or self._cache_sessao is None:
            self._cache_sessao = await self._provedor()
        return self._cache_sessao

    async def buscar(self, keywords: str) -> list[Similar]:
        """Pares dos chamados resolvidos do Portal para `keywords`. BEST-EFFORT ([] em falha)."""
        if not keywords.strip():
            return []
        try:
            return await self._buscar(keywords)
        except Exception:
            logger.exception("Busca no Portal TOTVS falhou — segue sem ela.")
            return []

    async def _buscar(self, keywords: str) -> list[Similar]:
        sessao = await self._garantir_sessao()
        if sessao is None:
            logger.info("Portal: sem sessão válida — pulando a busca.")
            return []
        tickets = await self._listar(sessao, keywords)
        if tickets is None:  # token expirou e não deu para renovar
            return []
        pares: list[Similar] = []
        for t in tickets[: self._top_k]:
            solucao = await self._client.solucao(sessao, t.ticket_id)
            if solucao.strip():  # só chamados COM resposta do agente viram par
                pares.append(_par_portal(t, solucao))
        return pares

    async def _listar(self, sessao: SessaoPortal, keywords: str) -> list[TicketPortal] | None:
        """Lista os tickets; renova a sessão UMA vez se o token expirou (401/403)."""
        try:
            tickets, _ = await self._client.buscar_tickets(sessao, keywords=keywords)
            return tickets
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (401, 403):
                raise
            logger.info("Portal: token expirado (%s) — renovando.", exc.response.status_code)
            nova = await self._garantir_sessao(forcar=True)
            if nova is None:
                return None
            tickets, _ = await self._client.buscar_tickets(nova, keywords=keywords)
            return tickets
