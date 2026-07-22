"""Provedor de sessão do Portal TOTVS — login (2FA via relay) → JWT → `SessaoPortal` (ADR-026).

O login do Portal é 2FA (TOTVS Identity, SSO SAML). Este provedor dirige o browser
(Playwright, perfil PERSISTIDO) e:

1. abre o Portal; se já logado (perfil lembra a sessão), captura o JWT do corpo do `get-tickets`;
2. senão, preenche usuário/senha (`#username`/`#password`, botão "Entrar"); se o Portal pedir o
   2FA (`#mfa-token`), usa o `RelayOtp` — o responsável (Gustavo) responde o código no grupo;
3. captura o JWT + userId + customerCode do `get-tickets` → `SessaoPortal` p/ o `PortalTotvsClient`.

O browser roda SÓ na re-autenticação ocasional (o token dura; o perfil persistido lembra o
dispositivo, então o 2FA fica raro). Credencial vem do `.env` — NUNCA logada. O `playwright`
é importado tardiamente (dependência pesada, só carrega quando o login é acionado).
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from app.otp_relay import RelayOtp
from app.portal_totvs import SessaoPortal

logger = logging.getLogger(__name__)

_PERFIL_PADRAO = str(Path.home() / ".suporte_totvs" / "portal_profile")


def _sessao_de_corpo(body: dict[str, Any]) -> SessaoPortal | None:
    """Monta a `SessaoPortal` do corpo de um `get-tickets` (token/userId/customerCode)."""
    token = body.get("token")
    user_id = body.get("userId")
    customer = body.get("customerCode")
    if not token or user_id is None or not customer:
        return None
    try:
        return SessaoPortal(
            token=str(token), user_id=int(user_id), customer_code=str(customer)
        )
    except (TypeError, ValueError):
        return None


class PortalLoginProvider:
    """Obtém uma `SessaoPortal` válida logando no Portal (2FA via `RelayOtp` quando exigido)."""

    def __init__(self, settings: Settings, relay: RelayOtp | None = None) -> None:
        self._s = settings
        self._relay = relay
        self._url = settings.portal_login_url
        self._perfil = settings.portal_login_profile_dir or _PERFIL_PADRAO
        self._headless = settings.portal_login_headless

    async def obter_sessao(self) -> SessaoPortal | None:
        from playwright.async_api import async_playwright  # import tardio (dep pesada)

        Path(self._perfil).mkdir(parents=True, exist_ok=True)
        capturado: dict[str, Any] = {}

        def on_request(req: Any) -> None:
            with contextlib.suppress(Exception):
                if "/get-tickets?" in req.url and req.method == "POST" and req.post_data:
                    corpo = json.loads(req.post_data)
                    if corpo.get("token"):
                        capturado.clear()
                        capturado.update(corpo)

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                self._perfil, headless=self._headless
            )
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            pg.on("request", on_request)
            try:
                await self._logar(pg)
                if not capturado:  # garante um get-tickets p/ capturar o token
                    await pg.goto(self._url, wait_until="networkidle", timeout=60000)
                    await pg.wait_for_timeout(2500)
            except Exception:
                logger.exception("PortalLoginProvider: falha no login.")
            finally:
                await ctx.close()

        sessao = _sessao_de_corpo(capturado)
        if sessao is None:
            logger.error("PortalLoginProvider: JWT não capturado (login falhou ou expirou).")
        else:
            logger.info("PortalLoginProvider: sessão obtida (user_id=%s).", sessao.user_id)
        return sessao

    async def _logar(self, pg: Any) -> None:
        await pg.goto(self._url, wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(2000)
        if not await pg.locator("#username").count():
            return  # já logado — o get-tickets será capturado

        logger.info("PortalLoginProvider: preenchendo credenciais.")
        await pg.fill("#username", self._s.portal_login_usuario)
        await pg.fill("#password", self._s.portal_login_senha)
        await pg.get_by_role("button", name="Entrar", exact=True).first.click()

        # espera o 2FA aparecer OU o Portal carregar (dispositivo confiável pula o 2FA)
        tem_mfa = False
        try:
            await pg.wait_for_selector("#mfa-token", timeout=12000)
            tem_mfa = True
        except Exception:
            tem_mfa = False

        if tem_mfa:
            if self._relay is None:
                logger.error("2FA exigido, mas sem RelayOtp configurado.")
                return
            logger.info("PortalLoginProvider: 2FA — solicitando o código no grupo.")
            otp = await self._relay.solicitar(motivo="acesso ao Portal TOTVS")
            if not otp:
                logger.error("2FA: o relay não trouxe o código (timeout).")
                return
            await pg.type("#mfa-token", otp, delay=80)
            await pg.get_by_role("button", name="Entrar", exact=True).first.click()

        with contextlib.suppress(Exception):
            await pg.wait_for_load_state("networkidle", timeout=60000)
