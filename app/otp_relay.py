"""Relay de OTP pelo grupo de WhatsApp — a ponte humana do login 2FA (ADR-026).

O login do Portal TOTVS exige um 2FA cujo código é emitido num app do responsável — o sistema
não tem esse código. Então ele PEDE no grupo de WhatsApp e ESPERA a resposta:

1. posta o pedido no grupo (via `WhatsAppClient`);
2. observa o `InboxWhatsApp` (alimentado pelo webhook de entrada) por uma resposta NO GRUPO,
   de um remetente autorizado, contendo o código;
3. extrai e devolve o código.

Human-in-the-loop e SÓ para re-autenticação ocasional (o JWT dura; ver ADR-026). O consumidor
disto é o provedor de token (login 2FA via browser), a construir a seguir.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app.config import Settings
from app.whatsapp import InboxWhatsApp, MensagemRecebida, WhatsAppClient, normalizar_numero

logger = logging.getLogger(__name__)

# Aceita "656-789", "656 789" (dois grupos de 3) ou um bloco de 4–8 dígitos ("123456").
_RE_OTP = re.compile(r"\d{3}[-\s]?\d{3}|\d{4,8}")


def extrair_otp(texto: str) -> str | None:
    """Extrai um código de 4–8 dígitos do texto; None se não houver um plausível."""
    if not texto:
        return None
    m = _RE_OTP.search(texto)
    if not m:
        return None
    digitos = re.sub(r"\D", "", m.group())
    return digitos if 4 <= len(digitos) <= 8 else None


def _numero_do_jid(jid: str) -> str:
    """`5511992568511@s.whatsapp.net` -> `5511992568511` (normalizado)."""
    return normalizar_numero(jid.split("@", 1)[0])


class RelayOtp:
    """Pede o OTP no grupo e espera a resposta de um remetente autorizado.

    Recebe o `WhatsAppClient` (envia o pedido) e o `InboxWhatsApp` (recebe a resposta) por
    injeção — testável sem rede.
    """

    def __init__(
        self, whatsapp: WhatsAppClient, inbox: InboxWhatsApp, settings: Settings
    ) -> None:
        self._whatsapp = whatsapp
        self._inbox = inbox
        self._grupo = settings.whatsapp_grupo_destino.strip()
        self._autorizados = {
            normalizar_numero(n) for n in settings.portal_otp_autorizados if n.strip()
        }

    def _remetente_autorizado(self, msg: MensagemRecebida) -> bool:
        """Só respostas NO GRUPO configurado; se houver lista, só dos números autorizados."""
        if not (msg.eh_grupo and msg.chat_jid == self._grupo):
            return False
        if not self._autorizados:
            return True  # sem lista: o próprio grupo é a fronteira de confiança
        return _numero_do_jid(msg.remetente_jid) in self._autorizados

    async def solicitar(
        self,
        *,
        motivo: str = "acesso ao Portal TOTVS",
        timeout_s: float = 180.0,
        intervalo_s: float = 3.0,
    ) -> str | None:
        """Pede o código no grupo e espera a resposta. Devolve o código ou None (timeout)."""
        if not self._grupo:
            logger.error("Relay OTP: WHATSAPP_GRUPO_DESTINO vazio — não há para onde pedir.")
            return None

        # Só considera respostas que chegarem DEPOIS do pedido (ignora o histórico do inbox).
        vistos = {m.id for m in self._inbox.recentes(50)}
        pedido = (
            f"🔐 Preciso do *código de {motivo}* (2FA). "
            "Responda AQUI, neste grupo, apenas com o código."
        )
        await self._whatsapp.enviar(self._grupo, pedido)

        fim = time.monotonic() + timeout_s
        while True:
            for msg in self._inbox.recentes(50):
                if msg.id in vistos or msg.de_mim:
                    continue
                if not self._remetente_autorizado(msg):
                    continue
                otp = extrair_otp(msg.texto)
                if otp:
                    logger.info(
                        "Relay OTP: código recebido de %s.", msg.remetente_nome or "?"
                    )
                    return otp
            if time.monotonic() >= fim:
                logger.warning(
                    "Relay OTP: timeout (%.0fs) sem resposta válida no grupo.", timeout_s
                )
                return None
            await asyncio.sleep(min(intervalo_s, max(0.0, fim - time.monotonic())))
