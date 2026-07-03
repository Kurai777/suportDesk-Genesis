"""Cliente fino (assíncrono) do Claude — gera o rascunho ancorado no <contexto>.

Regra de ouro (anti-alucinação): o modelo responde EXCLUSIVAMENTE com base nos
pares problema→solução recuperados. Se a solução não estiver no <contexto>,
`encontrou_solucao=false`.

O cliente NÃO faz retrieval — recebe os pares já recuperados (isso é do RagService).
Saída estruturada via TOOL USE FORÇADO (ADR-007): uma tool cujo `input_schema` é o
schema de `RespostaIA`, com `tool_choice` obrigando a chamada; o `input` é validado
contra `RespostaIA`.
"""

from __future__ import annotations

from collections.abc import Sequence

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
from app.models import RespostaIA
from app.rag import Similar

_TOOL_NAME = "responder_chamado"
_MAX_TOKENS = 1024

SYSTEM_PROMPT = """Você é um assistente de suporte técnico para o ERP TOTVS Protheus.

Você recebe o chamado de um cliente e um bloco <contexto> com casos anteriores já
resolvidos (pares problema→solução) recuperados da base de conhecimento.

Regras invioláveis:
1. Responda EXCLUSIVAMENTE com base no que estiver dentro do bloco <contexto>.
2. Se a solução para o chamado não estiver no <contexto>, defina
   encontrou_solucao=false e confianca="baixa".
3. NUNCA cite parâmetro, tabela, campo ou caminho que não apareça no <contexto>.
   Não invente passos nem nomes de parâmetros (ex.: MV_*).
4. Escreva resposta_cliente em português, em tom profissional e cordial, pronta para
   revisão humana antes do envio.

Registre sua resposta chamando a ferramenta responder_chamado."""


_retry_claude = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8.0),
    retry=retry_if_exception_type(
        (RateLimitError, APIConnectionError, InternalServerError)
    ),
)


def _tool_responder_chamado() -> dict:
    """Tool cujo input_schema é o schema de RespostaIA (saída estruturada forçada)."""
    return {
        "name": _TOOL_NAME,
        "description": "Registra a resposta estruturada ao chamado de suporte TOTVS Protheus.",
        "input_schema": RespostaIA.model_json_schema(),
    }


def _montar_contexto(pares: Sequence[Similar]) -> str:
    return "\n\n".join(
        f"[Caso {i}]\nProblema: {par.problema}\nSolução: {par.solucao}"
        for i, par in enumerate(pares, start=1)
    )


def _resposta_sem_contexto() -> RespostaIA:
    """Sem nada recuperado, a regra de ouro já garante encontrou_solucao=false."""
    return RespostaIA(
        resposta_cliente=(
            "Não localizei uma solução conhecida na nossa base para este chamado. "
            "Um especialista fará a análise e retornará em seguida."
        ),
        encontrou_solucao=False,
        confianca="baixa",
        resumo_para_responsavel=(
            "Nenhum caso similar recuperado na base — requer revisão humana."
        ),
        urgencia="media",
    )


class ClaudeClient:
    """Gera o rascunho de resposta via Claude Haiku, ancorado no contexto recuperado."""

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        # max_retries=0: o retry fica a cargo do tenacity (Padrões de Engenharia).
        self._client = client or AsyncAnthropic(
            api_key=settings.anthropic_api_key, max_retries=0
        )
        self._model = settings.claude_model
        self._tool = _tool_responder_chamado()
        # Bloco estático de instruções com prompt caching; o contexto variável fica no
        # user message, FORA do cache (ver ADR-007).
        self._system = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def gerar_resposta(
        self, problema: str, contexto_pares: Sequence[Similar]
    ) -> RespostaIA:
        if not contexto_pares:
            return _resposta_sem_contexto()

        contexto = _montar_contexto(contexto_pares)
        conteudo = f"<contexto>\n{contexto}\n</contexto>\n\nChamado do cliente:\n{problema}"
        message = await self._criar(conteudo)
        return self._extrair(message)

    @_retry_claude
    async def _criar(self, conteudo: str):
        return await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=self._system,
            tools=[self._tool],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": conteudo}],
        )

    @staticmethod
    def _extrair(message) -> RespostaIA:
        for bloco in message.content:
            if getattr(bloco, "type", None) == "tool_use" and bloco.name == _TOOL_NAME:
                return RespostaIA.model_validate(bloco.input)
        raise ValueError("O Claude não retornou a ferramenta responder_chamado.")
