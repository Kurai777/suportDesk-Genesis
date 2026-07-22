"""Armazém da sessão do Portal (token) — desacopla o browser (login 2FA) da busca (ADR-026).

O REFRESHER (scripts/refrescar_sessao_portal) minta a `SessaoPortal` (login + 2FA via relay)
e GRAVA aqui; o SERVIDOR só LÊ, para o `PortalService` buscar via httpx. Assim o browser roda
FORA do caminho do request (e some o conflito Proactor/Selector do Windows).

O arquivo contém o TOKEN (segredo de sessão) — é local e está no `.gitignore`. `ler` é async
por contrato (é o `ProvedorSessao` do `PortalService`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.portal_totvs import SessaoPortal

logger = logging.getLogger(__name__)


class SessaoStore:
    """Lê/grava a `SessaoPortal` num arquivo JSON local (o token é segredo — gitignored)."""

    def __init__(self, caminho: str) -> None:
        self._path = Path(caminho)

    def gravar(self, sessao: SessaoPortal) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "token": sessao.token,
                    "user_id": sessao.user_id,
                    "customer_code": sessao.customer_code,
                }
            ),
            encoding="utf-8",
        )

    async def ler(self) -> SessaoPortal | None:
        """Provedor de sessão do `PortalService`. None se o arquivo não existe ou é ilegível."""
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            return SessaoPortal(
                token=str(d["token"]),
                user_id=int(d["user_id"]),
                customer_code=str(d["customer_code"]),
            )
        except FileNotFoundError:
            logger.info("SessaoStore: %s ainda não existe (rode o refresher).", self._path)
            return None
        except Exception:
            logger.exception("SessaoStore: falha ao ler %s.", self._path)
            return None
