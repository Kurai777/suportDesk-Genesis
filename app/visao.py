"""Cliente fino (assíncrono) de VISÃO — transcreve o texto legível de imagens de chamados.

A maioria dos chamados chega por e-mail com PRINTS de erro (logs, mensagens, códigos). Este
cliente usa o Claude (Haiku, visão) só para TRANSCREVER o texto legível dessas imagens, que o
pipeline concatena ao problema antes da busca (RAG) — ADR-023.

Regra de ouro (anti-alucinação): transcreve SOMENTE o que está legível; se ilegível ou sem
texto útil, devolve string VAZIA — NUNCA interpreta, descreve, resume ou inventa. É best-effort
no pipeline: falha aqui não derruba o processamento do chamado.
"""

from __future__ import annotations

import base64

from anthropic import (
    APIConnectionError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings

_MAX_TOKENS = 1024
# Sentinela devolvida pelo modelo quando não há texto legível/útil — mapeada para "".
_MARCADOR_VAZIO = "[SEM_TEXTO]"
# Formatos aceitos: imagens (visão do Claude) + PDF (logs de erro, comprovantes de NF — ADR-037).
_TIPOS_IMAGEM = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TIPO_PDF = "application/pdf"
_TIPOS_SUPORTADOS = _TIPOS_IMAGEM | {_TIPO_PDF}

_PROMPT = (
    "Transcreva SOMENTE o texto legível deste anexo (imagem ou PDF) — logs, mensagens de erro, "
    "códigos (ex.: SCC19070), nomes de parâmetros/tabelas/campos e o conteúdo de tabelas (ex.: "
    "dados de uma nota fiscal) — exatamente como aparece, preservando a ordem. NÃO descreva, NÃO "
    "explique, NÃO resuma e NÃO invente nada. Sem texto legível e útil, responda APENAS com "
    f"{_MARCADOR_VAZIO}."
)

_retry_visao = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=8.0),
    retry=retry_if_exception_type(
        (RateLimitError, APIConnectionError, InternalServerError)
    ),
)


class VisaoClient:
    """Transcreve texto legível de imagens via Claude (visão). Recebe `Settings` por injeção."""

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        # max_retries=0: o retry fica a cargo do tenacity (Padrões de Engenharia).
        self._client = client or AsyncAnthropic(
            api_key=settings.anthropic_api_key, max_retries=0
        )
        self._model = settings.claude_model

    async def transcrever(self, imagem: bytes, content_type: str) -> str:
        """Texto legível do anexo (imagem OU PDF); "" se tipo não suportado, vazio ou sem texto."""
        tipo = (content_type or "").split(";", 1)[0].strip().lower()
        if tipo not in _TIPOS_SUPORTADOS or not imagem:
            return ""
        b64 = base64.standard_b64encode(imagem).decode("ascii")
        message = await self._criar(b64, tipo)
        return self._extrair(message)

    @_retry_visao
    async def _criar(self, b64: str, media_type: str):
        # PDF vai como bloco "document"; imagem, como "image" (ADR-037).
        tipo_bloco = "document" if media_type == _TIPO_PDF else "image"
        anexo = {
            "type": tipo_bloco,
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
        return await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            messages=[
                {"role": "user", "content": [anexo, {"type": "text", "text": _PROMPT}]}
            ],
        )

    @staticmethod
    def _extrair(message) -> str:
        texto = "".join(
            b.text for b in message.content if getattr(b, "type", None) == "text"
        ).strip()
        if not texto or _MARCADOR_VAZIO in texto:
            return ""
        return texto
