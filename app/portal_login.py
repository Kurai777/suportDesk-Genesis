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
        await self._submeter(pg, "#password")

        # espera o 2FA aparecer OU o Portal carregar (dispositivo confiável pula o 2FA)
        tem_mfa = False
        try:
            await pg.wait_for_selector("#mfa-token", timeout=15000)
            tem_mfa = True
        except Exception:
            tem_mfa = False

        if tem_mfa:
            if self._relay is None:
                logger.error("2FA exigido, mas sem RelayOtp configurado.")
                return
            if not await self._resolver_2fa(pg):
                logger.error(
                    "2FA: não completou após %d tentativa(s).", self._s.portal_login_max_2fa
                )
                return

        with contextlib.suppress(Exception):
            await pg.wait_for_load_state("networkidle", timeout=60000)

    async def _resolver_2fa(self, pg: Any) -> bool:
        """Pede o código no grupo, submete e RE-TENTA se inválido/expirado (feedback no grupo).

        O código 2FA expira (~30s), então uma tentativa pode falhar por timing — aqui avisamos
        o responsável no grupo ("inválido, manda outro") e pedimos de novo, até `max_2fa`.
        """
        relay = self._relay
        for tentativa in range(1, self._s.portal_login_max_2fa + 1):
            logger.info("2FA: tentativa %d — pedindo o código no grupo.", tentativa)
            otp = await relay.solicitar(
                motivo="acesso ao Portal TOTVS",
                timeout_s=self._s.portal_login_2fa_timeout_s,
            )
            if not otp:
                logger.info("2FA: sem resposta a tempo (tentativa %d).", tentativa)
                with contextlib.suppress(Exception):
                    await relay.avisar("⏳ Não recebi o código a tempo. Pedindo de novo…")
                continue

            with contextlib.suppress(Exception):
                await pg.fill("#mfa-token", "")  # limpa um código anterior, se houver
            await pg.type("#mfa-token", otp, delay=80)
            await self._submeter(pg, "#mfa-token")

            # sucesso = a tela do 2FA some (navegou para o Portal)
            try:
                await pg.wait_for_selector("#mfa-token", state="detached", timeout=15000)
            except Exception:
                logger.info("2FA: código inválido/expirado (tentativa %d).", tentativa)
                with contextlib.suppress(Exception):
                    await relay.avisar(
                        "❌ Código inválido ou expirado. Me manda um novo, por favor."
                    )
                continue

            logger.info("2FA: aceito na tentativa %d.", tentativa)
            with contextlib.suppress(Exception):
                await relay.avisar("✅ Código aceito — login realizado com sucesso!")
            return True
        return False

    @staticmethod
    async def _submeter(pg: Any, campo: str) -> None:
        """Submete o form: clica o botão de texto "Entrar" (exato); se falhar, dá Enter no campo.

        Robusto contra o nome acessível do botão não bater exatamente (o texto visível é "Entrar",
        mas o role/name pode diferir).
        """
        try:
            await pg.get_by_text("Entrar", exact=True).first.click(timeout=6000)
        except Exception:
            with contextlib.suppress(Exception):
                await pg.locator(campo).press("Enter")
