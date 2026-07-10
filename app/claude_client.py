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
# 2048 (era 1024): respostas ancoradas — sobretudo as da busca web, que resumem
# procedimentos de configuração — truncavam em 1024 (stop_reason=max_tokens), devolvendo
# tool_use com input incompleto → ValidationError. O modelo para sozinho ao terminar
# (temperature=0), então o teto extra não é gasto à toa. Ver ADR-016.
_MAX_TOKENS = 2048

# Texto-modelo FIXO da resposta ao cliente quando o chamado será ESCALADO
# (encontrou_solucao=false). Fase 1: nunca deixamos o modelo gerar livremente esse texto —
# ele já vazou "não consta na base" e pediu que o cliente verificasse versão/erro (ADR-022).
# A saudação é curta e apenas acolhedora; toda a análise técnica vai à nota interna.
RESPOSTA_ESCALAR_PADRAO = (
    "Olá! Seu chamado está sendo analisado pelo nosso time e retornaremos em breve."
)

SYSTEM_PROMPT = """Você é um assistente de suporte técnico para o ERP TOTVS Protheus.

Você recebe o chamado de um cliente e um bloco <contexto> com trechos recuperados da
base de conhecimento. Cada item traz sua ORIGEM: "Documentação oficial TOTVS",
"Chamado anterior" já resolvido, ou "Busca web em site oficial TOTVS" (fonte menos
verificada, obtida na hora).

REGRA DE OURO (anti-alucinação) — inviolável:
1. Responda EXCLUSIVAMENTE com base no que estiver dentro do bloco <contexto>.
2. Se a solução para o chamado não estiver no <contexto>, defina encontrou_solucao=false
   e confianca="baixa".
3. NUNCA cite parâmetro, tabela, campo ou caminho que não apareça no <contexto>. Não
   invente passos nem nomes de parâmetros (ex.: MV_*).
4. Em caso de CONFLITO entre as origens, priorize: Documentação oficial TOTVS > Chamado
   anterior > Busca web.
5. Se a solução vier de "Busca web", seja conservador: confianca no máximo "media".

DOIS TEXTOS, PÚBLICOS DIFERENTES:
- `resumo_para_responsavel`: vai para o TIME interno. Diga a verdade técnica do processo
  (ex.: "sem solução na base, requer análise manual"). Curto e direto.
- `resposta_cliente`: é o texto que VAI AO CLIENTE. Ele NUNCA pode perceber COMO a resposta
  foi produzida. É PROIBIDO mencionar "base de conhecimento", "IA", "não encontrei", "os
  artigos disponíveis", "com base na análise" ou qualquer referência ao processo interno.

COMO ESCREVER resposta_cliente (português, pronta para revisão humana):
- encontrou_solucao=true (há solução no contexto): responda DIRETO e objetivo, como um
  técnico experiente que sabe a resposta — entregue a solução sem preâmbulo e sem repetição.
- encontrou_solucao=false (será escalado a um humano): a resposta_cliente será SUBSTITUÍDA
  pelo sistema por uma saudação-padrão curta e acolhedora — você NÃO precisa escrevê-la. Para
  este caso é PROIBIDO, na resposta_cliente: dar solução, pedir que o cliente verifique versão,
  mensagem de erro ou qualquer coisa, e listar passos de investigação. Toda a análise técnica
  (o que investigar, versão a conferir, hipóteses, próximos passos) vai EXCLUSIVAMENTE em
  resumo_para_responsavel — JAMAIS na resposta_cliente.

PEDIDO OPERACIONAL:
- Se o chamado é uma TAREFA a ser EXECUTADA por uma pessoa (cadastro, liberação, ajuste
  manual — ex.: "incluir cadastro do grupo tributário na SX5"), defina
  pedido_operacional=true e encontrou_solucao=false (não é uma dúvida com resposta na base).
  Como encontrou_solucao=false, a resposta_cliente também será a saudação-padrão; descreva a
  execução pendente em resumo_para_responsavel, para o time providenciar. Caso contrário,
  pedido_operacional=false.

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


def _rotulo_fonte(par: Similar) -> str:
    if par.fonte == "documentacao":
        return f"Fonte: Documentação oficial TOTVS — {par.titulo or 'sem título'}"
    if par.fonte == "web_totvs":
        return (
            "Fonte: Busca web em site oficial TOTVS (MENOS verificada — exige revisão "
            f"humana redobrada) — {par.titulo or 'sem título'}"
        )
    return f"Fonte: Chamado anterior #{par.ticket_id}"


def _montar_contexto(pares: Sequence[Similar]) -> str:
    return "\n\n".join(
        f"[Caso {i}] {_rotulo_fonte(par)}\nProblema: {par.problema}\nSolução: {par.solucao}"
        for i, par in enumerate(pares, start=1)
    )


def _acolhimento_padrao_se_escala(resposta: RespostaIA) -> RespostaIA:
    """No caminho de ESCALAR (encontrou_solucao=false), força a saudação-padrão ao cliente.

    Ponto ÚNICO da garantia (ADR-022): a resposta_cliente do escalar NUNCA é o texto livre do
    modelo — assim o cliente jamais vê menção à base/IA nem pedido de verificação de
    versão/erro. A análise técnica do modelo permanece intacta em `resumo_para_responsavel`
    (usada na nota interna). No caminho de RESOLVER (true), o texto do modelo é preservado.
    """
    if resposta.encontrou_solucao:
        return resposta
    return resposta.model_copy(update={"resposta_cliente": RESPOSTA_ESCALAR_PADRAO})


def _resposta_sem_contexto() -> RespostaIA:
    """Sem nada recuperado, a regra de ouro já garante encontrou_solucao=false.

    O texto ao cliente é a saudação-padrão (acolhe sem revelar o processo — nada de
    "base"/"IA"); o resumo ao time diz a verdade técnica.
    """
    return RespostaIA(
        resposta_cliente=RESPOSTA_ESCALAR_PADRAO,
        encontrou_solucao=False,
        confianca="baixa",
        resumo_para_responsavel=(
            "Nenhum caso similar recuperado na base — requer revisão humana."
        ),
        urgencia="media",
        pedido_operacional=False,
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
        return _acolhimento_padrao_se_escala(self._extrair(message))

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
