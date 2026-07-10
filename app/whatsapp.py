"""Cliente fino (assíncrono) do WhatsApp via Evolution API v2 — Módulo 6.

Notifica o responsável pelo chamado. O cliente é BURRO: recebe número + texto e
envia; NÃO monta os textos das notificações (isso é do pipeline).

Resiliência (crítico): o envio é MELHOR ESFORÇO. O chamado já foi tratado no
Freshdesk (nota + atribuição) ANTES desta etapa e não pode ser desfeito por uma
falha de notificação. Por isso `enviar` NUNCA propaga exceção: em qualquer falha
(rede, número inválido, Evolution fora do ar) loga e retorna False. O retry
(tenacity) cobre apenas erros transitórios de rede.
"""

from __future__ import annotations

import logging
import re

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings

logger = logging.getLogger(__name__)

_TENTATIVAS = 3
_ESPERA_MAX = 1.0
_NAO_DIGITO = re.compile(r"\D")


def normalizar_numero(numero: str) -> str:
    """Remove tudo que não for dígito e garante o DDI 55.

    Números locais com 10–11 dígitos (DDD + telefone) recebem o prefixo 55. Números
    que já vêm com DDI (12–13 dígitos) ficam como estão.
    """
    digitos = _NAO_DIGITO.sub("", numero)
    if len(digitos) in (10, 11):
        return f"55{digitos}"
    return digitos


def resolver_destino(destino: str) -> str:
    """Destino aceito pela Evolution: um JID (grupo/contato) passa intacto; telefone é normalizado.

    Grupos do WhatsApp NÃO são telefones — são JIDs terminados em `@g.us` (ex.:
    120363...@g.us); contatos, em `@s.whatsapp.net`. Qualquer coisa com `@` já é um JID e
    NÃO pode passar por `normalizar_numero` (que apagaria o `@g.us`). Ver ADR-029.
    """
    d = destino.strip()
    if "@" in d:
        return d
    return normalizar_numero(d)


class WhatsAppClient:
    """Envia mensagens de texto via Evolution API v2. Recebe `Settings` por injeção."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = settings.whatsapp_api_url.rstrip("/")
        self._instance = settings.whatsapp_instance
        self._apikey = settings.whatsapp_api_key  # token da INSTÂNCIA
        self._dry_run = settings.whatsapp_dry_run
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def enviar(self, numero: str, texto: str) -> bool:
        """Envia `texto` para `numero` (melhor esforço). Retorna sucesso/falha.

        `numero` pode ser um telefone (normalizado) OU um JID de grupo `...@g.us` (ADR-029) —
        o mesmo endpoint da Evolution aceita os dois no campo `number`.
        """
        destino = resolver_destino(numero)

        if self._dry_run:
            logger.info("[WhatsApp dry-run] Para %s enviaria: %s", destino, texto)
            return True

        try:
            await self._post(destino, texto)
            return True
        except Exception as exc:
            # Melhor esforço: a notificação não pode derrubar o processamento do chamado.
            logger.warning("Falha ao enviar WhatsApp para %s: %s", destino, exc)
            return False

    @retry(
        reraise=True,
        stop=stop_after_attempt(_TENTATIVAS),
        wait=wait_exponential(multiplier=0.1, max=_ESPERA_MAX),
        retry=retry_if_exception_type(httpx.RequestError),  # só rede transitória
    )
    async def _post(self, destino: str, texto: str) -> None:
        url = f"{self._base_url}/message/sendText/{self._instance}"
        resp = await self._client.post(
            url,
            headers={"apikey": self._apikey},
            json={"number": destino, "text": texto},
        )
        resp.raise_for_status()

    @retry(
        reraise=True,
        stop=stop_after_attempt(_TENTATIVAS),
        wait=wait_exponential(multiplier=0.1, max=_ESPERA_MAX),
        retry=retry_if_exception_type(httpx.RequestError),
    )
    async def listar_grupos(self) -> list[dict[str, str]]:
        """Lista os grupos da instância como `{"jid", "nome"}` (ADR-029).

        Ferramenta de SETUP: serve para descobrir o JID do grupo que vai em
        WHATSAPP_GRUPO_DESTINO. Não é usada no fluxo do chamado. Levanta em erro HTTP/rede
        (o chamador — o script — trata e reporta).
        """
        url = f"{self._base_url}/group/fetchAllGroups/{self._instance}"
        resp = await self._client.get(
            url, headers={"apikey": self._apikey}, params={"getParticipants": "false"}
        )
        resp.raise_for_status()
        grupos = resp.json() or []
        return [
            {"jid": g.get("id", ""), "nome": g.get("subject", "") or "(sem nome)"}
            for g in grupos
            if g.get("id")
        ]

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> WhatsAppClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
