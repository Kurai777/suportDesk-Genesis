"""Cliente fino (assíncrono) do Portal do Cliente TOTVS — API interna JSON (ADR-026).

Descoberto por spike (2026-07-22): a SPA do Portal usa uma API privada. Autentica com um
**JWT no CORPO do POST** (não header/cookie), emitido pelo login **2FA** da sessão. Este
cliente NÃO faz login (o 2FA é humano) — recebe a `SessaoPortal` (token + identidade) já
obtida e chama:

- `buscar_tickets()` — `get-tickets`: pesquisa por `keywords` + filtros catalogV3 (produto/
  módulo Protheus) + paginação. Retorna o histórico (inclusive cross-cliente do parceiro).
- `comentarios()` / `solucao()` — `get-comments`: a SOLUÇÃO são os comentários do AGENTE TOTVS
  (não-cliente e não-privados). O corpo vem em HTML → é limpo aqui.

Fase 1: é uma fonte MENOS verificada (como a busca web, ADR-015) — a regra de ouro
anti-alucinação e a revisão humana continuam obrigatórias para o que vier daqui. O `token`
é SEGREDO: nunca logar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from lxml import html as lxml_html
from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings

_TENTATIVAS = 4
_ESPERA_MAX = 8.0


def _e_retentavel(exc: BaseException) -> bool:
    """Re-tenta em erro de rede, HTTP 429 (rate limit) ou 5xx (falha transitória do servidor)."""
    if isinstance(exc, httpx.HTTPStatusError):
        cod = exc.response.status_code
        return cod == 429 or cod >= 500
    return isinstance(exc, httpx.RequestError)


_retry_portal = retry(
    reraise=True,
    stop=stop_after_attempt(_TENTATIVAS),
    wait=wait_exponential(multiplier=0.5, max=_ESPERA_MAX),
    retry=retry_if_exception(_e_retentavel),
)


def _html_para_texto(bruto: str) -> str:
    """Extrai o texto legível de um corpo HTML (comentário/descrição), preservando quebras.

    Os comentários vêm como HTML (ex.: `<div class="zd-comment">…<br>…</div>`). Convertemos
    `<br>` em nova linha, extraímos o texto e normalizamos espaços — bom o bastante para
    embeddar e para o rascunho (revisado por humano).
    """
    if not bruto:
        return ""
    if "<" not in bruto:
        return "\n".join(" ".join(ln.split()) for ln in bruto.splitlines()).strip()
    try:
        doc = lxml_html.fromstring(bruto)
        for br in doc.iter("br"):
            br.tail = "\n" + (br.tail or "")
        texto = doc.text_content()
    except Exception:
        texto = bruto
    linhas = [" ".join(linha.split()) for linha in texto.splitlines()]
    return "\n".join(linha for linha in linhas if linha).strip()


@dataclass(frozen=True)
class SessaoPortal:
    """Identidade da sessão logada no Portal, obtida no login 2FA (FORA deste cliente).

    `token` é o JWT de sessão (segredo, curta duração). `user_id` e `customer_code` compõem
    o corpo das requisições, junto do token.
    """

    token: str
    user_id: int
    customer_code: str


class TicketPortal(BaseModel):
    """Um chamado do Portal (item de `get-tickets`) — o PROBLEMA do par para o RAG."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: int
    subject: str = ""
    description: str = ""
    status: str = ""
    produto: str | None = None
    modulo: str | None = None
    organizacao: str | None = None  # empresa-origem: METADADO (não vai ao texto embedado)
    criado_em: str | None = None
    atualizado_em: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> TicketPortal:
        cat = d.get("catalogV3") or {}
        org = d.get("organization") or {}
        return cls(
            ticket_id=d["ticketId"],
            subject=d.get("subject") or "",
            description=_html_para_texto(d.get("description") or ""),
            status=d.get("status") or "",
            produto=cat.get("product"),
            modulo=cat.get("module"),
            organizacao=org.get("name"),
            criado_em=d.get("createdAt"),
            atualizado_em=d.get("updatedAt"),
        )


class ComentarioPortal(BaseModel):
    """Um comentário de um chamado (item de `get-comments`)."""

    model_config = ConfigDict(extra="ignore")

    autor: str = ""
    corpo: str = ""
    is_cliente: bool = False  # isEndUser: True = escrito pelo cliente, não pelo agente TOTVS
    is_privado: bool = False  # isPrivate: nota interna, não faz parte da resposta ao cliente
    criado_em: str | None = None

    @property
    def eh_solucao(self) -> bool:
        """True se é a resposta do AGENTE TOTVS ao cliente (pública) — a solução."""
        return not self.is_cliente and not self.is_privado

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> ComentarioPortal:
        return cls(
            autor=d.get("author") or "",
            corpo=_html_para_texto(d.get("body") or ""),
            is_cliente=bool(d.get("isEndUser")),
            is_privado=bool(d.get("isPrivate")),
            criado_em=d.get("createdAt"),
        )


class PortalTotvsClient:
    """Acesso fino e assíncrono à API interna do Portal TOTVS. Recebe `Settings` por injeção.

    NÃO autentica (o login 2FA é humano); cada método recebe a `SessaoPortal` com o token.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.portal_totvs_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)

    # --- API pública -------------------------------------------------------

    async def buscar_tickets(
        self,
        sessao: SessaoPortal,
        *,
        keywords: str = "",
        produtos: Sequence[str] | None = None,
        modulos: Sequence[str] | None = None,
        organizacoes: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        pagina: int = 1,
    ) -> tuple[list[TicketPortal], bool]:
        """Pesquisa/lista chamados. Retorna (tickets, tem_proxima_pagina)."""
        corpo = {
            "keywords": keywords,
            "updatedAtStart": "",
            "updatedAtEnd": "",
            "createdAtStart": "",
            "createdAtEnd": "",
            "catalogV3ProductTags": list(produtos or []),
            "catalogV3ModuleTags": list(modulos or []),
            "catalogV3RoutineGrouperTags": [],
            "organizationsIds": list(organizacoes or []),
            "requesterId": "",
            "isTechnicalConsultancy": None,
            "status": list(status or []),
            "primeRhodium": 0,
            "userId": sessao.user_id,
            "pagination": {"page": pagina},
            "sortBy": "updated_at",
            "sortOrder": "desc",
            "token": sessao.token,
            "customerCode": sessao.customer_code,
        }
        data = await self._post("/help-center/tickets/get-tickets", corpo)
        tickets = [
            TicketPortal.from_api(t)
            for t in data.get("tickets", [])
            if t.get("ticketId") is not None
        ]
        tem_proximo = bool((data.get("pagination") or {}).get("hasNext"))
        return tickets, tem_proximo

    async def comentarios(
        self, sessao: SessaoPortal, ticket_id: int, *, pagina: int = 1
    ) -> list[ComentarioPortal]:
        """Comentários (conversa) de um chamado."""
        corpo = {
            "ticketId": ticket_id,
            "pagination": {"page": pagina},
            "token": sessao.token,
            "customerCode": sessao.customer_code,
        }
        data = await self._post("/help-center/tickets/get-comments", corpo)
        return [ComentarioPortal.from_api(c) for c in data.get("comments", [])]

    async def solucao(self, sessao: SessaoPortal, ticket_id: int) -> str:
        """Junta os comentários do AGENTE TOTVS (públicos) = a solução do chamado.

        String vazia se o chamado ainda não tem resposta do agente (não vira par para o RAG).
        """
        coments = await self.comentarios(sessao, ticket_id)
        agente = [c.corpo for c in coments if c.eh_solucao and c.corpo.strip()]
        return "\n\n".join(agente)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PortalTotvsClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # --- interno -----------------------------------------------------------

    @_retry_portal
    async def _post(self, path: str, corpo: dict[str, Any]) -> dict[str, Any]:
        # Autenticação vai no CORPO (token), não em header. `language=pt` na query.
        resp = await self._client.post(
            f"{self._base}{path}", params={"language": "pt"}, json=corpo
        )
        resp.raise_for_status()
        return resp.json()
