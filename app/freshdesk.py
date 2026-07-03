"""Cliente fino (assíncrono) para a API v2 do Freshdesk — Fase 1 (copiloto).

Só o permitido na Fase 1: LER o chamado, criar NOTA INTERNA (privada) e ATRIBUIR
o responsável. NÃO há resposta pública ao cliente aqui (regra inviolável nº 1).

Todas as chamadas passam por retry com tenacity: re-tenta em erros de rede e em
HTTP 429, respeitando o header Retry-After (Padrões de Engenharia). I/O assíncrono
ponta a ponta (ADR-005): usa httpx.AsyncClient.
"""

import contextlib
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.models import TicketFreshdesk

_TENTATIVAS = 4
_ESPERA_MAX = 8.0


def _e_retentavel(exc: BaseException) -> bool:
    """Re-tenta em erro de rede (httpx.RequestError) ou HTTP 429 (rate limit)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return isinstance(exc, httpx.RequestError)


def _esperar(retry_state: RetryCallState) -> float:
    """Em 429, respeita Retry-After (segundos); senão, backoff exponencial."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after is not None:
            with contextlib.suppress(ValueError):  # header pode vir como data HTTP
                return max(0.0, float(retry_after))
    return wait_exponential(multiplier=0.5, max=_ESPERA_MAX)(retry_state)


class FreshdeskClient:
    """Acesso fino e assíncrono à API v2 do Freshdesk. Recebe `Settings` por injeção."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        dominio = settings.freshdesk_domain
        host = dominio if "." in dominio else f"{dominio}.freshdesk.com"
        self._base_url = f"https://{host}/api/v2"
        self._auth = (settings.freshdesk_api_key, "X")  # Basic Auth: chave + senha "X"
        self._client = client or httpx.AsyncClient(timeout=15.0)

    # --- API pública -------------------------------------------------------

    async def get_ticket(self, ticket_id: int) -> TicketFreshdesk:
        """Lê o chamado completo e normaliza para `TicketFreshdesk`."""
        resp = await self._request(
            "GET",
            f"/tickets/{ticket_id}",
            params={"include": "requester,company,stats"},
        )
        return TicketFreshdesk.from_freshdesk(resp.json())

    async def criar_nota_interna(self, ticket_id: int, body: str) -> None:
        """Cria uma NOTA PRIVADA no chamado (nunca visível ao cliente)."""
        await self._request(
            "POST",
            f"/tickets/{ticket_id}/notes",
            json={"body": body, "private": True},
        )

    async def atribuir(self, ticket_id: int, responder_id: int) -> None:
        """Atribui o chamado ao agente responsável."""
        await self._request(
            "PUT",
            f"/tickets/{ticket_id}",
            json={"responder_id": responder_id},
        )

    async def listar_tickets(self, page: int, per_page: int = 100) -> list[dict[str, Any]]:
        """Lista chamados (paginado). Retorna os dicts crus da API (para a ingestão)."""
        resp = await self._request(
            "GET", "/tickets", params={"page": page, "per_page": per_page}
        )
        return resp.json()

    async def get_conversations(self, ticket_id: int) -> list[dict[str, Any]]:
        """Retorna as conversas (respostas públicas e notas) de um chamado."""
        resp = await self._request(
            "GET", f"/tickets/{ticket_id}/conversations", params={"per_page": 100}
        )
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FreshdeskClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # --- interno -----------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(_TENTATIVAS),
        wait=_esperar,
        retry=retry_if_exception(_e_retentavel),
    )
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = await self._client.request(
            method, f"{self._base_url}{path}", auth=self._auth, **kwargs
        )
        resp.raise_for_status()
        return resp
