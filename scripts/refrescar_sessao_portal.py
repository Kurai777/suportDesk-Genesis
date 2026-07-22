"""Refresher da sessão do Portal (ADR-026): loga (2FA via relay) e grava o token no armazém.

Roda FORA do servidor (no Windows o browser exige o loop Proactor; o server usa Selector p/
psycopg). Rode/agende quando o token expirar. Requer:
  - credenciais do Portal no `.env` (PORTAL_LOGIN_USUARIO/SENHA);
  - Evolution de pé + o número da IA no grupo;
  - o SERVIDOR (webhook) de pé — o relay do OTP lê a resposta do grupo pelo inbox do servidor
    (`GET /webhook/whatsapp/recentes`). Aponte com SERVIDOR_WEBHOOK (default 127.0.0.1:8000).

Uso:
    python -m scripts.refrescar_sessao_portal
"""

import asyncio
import os

# NÃO setar WindowsSelectorEventLoopPolicy — o Playwright precisa do Proactor (default no Windows).
import httpx

from app.config import get_settings
from app.otp_relay import RelayOtp
from app.portal_login import PortalLoginProvider
from app.portal_sessao import SessaoStore
from app.whatsapp import MensagemRecebida, WhatsAppClient

_SERVER = os.environ.get("SERVIDOR_WEBHOOK", "http://127.0.0.1:8000")


class InboxHttp:
    """Lê o inbox do SERVIDOR (via /recentes) — onde o webhook guarda as respostas do grupo."""

    def __init__(self, base: str, secret: str) -> None:
        self._url = f"{base.rstrip('/')}/webhook/whatsapp/recentes"
        self._secret = secret

    def recentes(self, n: int = 10) -> list[MensagemRecebida]:
        try:
            r = httpx.get(self._url, params={"secret": self._secret, "n": n}, timeout=8)
            r.raise_for_status()
            return [MensagemRecebida(**m) for m in r.json().get("recentes", [])]
        except Exception:
            return []


async def main() -> None:
    s = get_settings()
    store = SessaoStore(s.portal_sessao_arquivo)
    async with WhatsAppClient(s) as wa:
        relay = RelayOtp(wa, InboxHttp(_SERVER, s.whatsapp_webhook_secret), s)
        provider = PortalLoginProvider(s, relay)
        print("Refrescando a sessão do Portal (login + 2FA via relay no grupo)...")
        sessao = await provider.obter_sessao()

    if sessao is None:
        print("[FALHA] Sessão não obtida (ver logs / verifique credenciais, Evolution, servidor).")
        raise SystemExit(1)
    store.gravar(sessao)
    print(f"[OK] Sessão gravada em {s.portal_sessao_arquivo} (user_id={sessao.user_id}).")


if __name__ == "__main__":
    asyncio.run(main())
