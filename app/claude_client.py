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
from app.models import QueryReformulada, RespostaIA
from app.rag import Similar
from app.texto import extrair_codigos_tecnicos

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
"Chamado anterior" já resolvido, ou "Busca web em referência técnica TOTVS/Protheus"
(oficial TOTVS ou referência técnica confiável da comunidade Protheus, obtida na hora).

REGRA DE OURO (anti-alucinação) — inviolável:
1. Responda EXCLUSIVAMENTE com base no que estiver dentro do bloco <contexto>.
2. Se a solução para o chamado não estiver no <contexto>, defina encontrou_solucao=false
   e confianca="baixa".
3. NUNCA cite parâmetro, tabela, campo ou caminho que não apareça no <contexto>. Não
   invente passos nem nomes de parâmetros (ex.: MV_*).
4. Em caso de CONFLITO entre as origens, priorize: Documentação oficial TOTVS > Chamado
   anterior > Referência técnica da web.
5. A referência técnica da web (oficial ou comunidade) é tratada como fonte confiável — se a
   solução estiver clara no contexto, use a confiança que o conteúdo merece (inclusive "alta").
   Continua valendo a regra 1: só o que estiver no <contexto>, nada inventado.

DOIS TEXTOS, PÚBLICOS DIFERENTES:
- `resumo_para_responsavel`: vai para o TIME interno. Diga a verdade técnica do processo
  (ex.: "sem solução na base, requer análise manual"). Curto e direto.
- `resposta_cliente`: é o texto que VAI AO CLIENTE. Ele NUNCA pode perceber COMO a resposta
  foi produzida. É PROIBIDO mencionar "base de conhecimento", "IA", "não encontrei", "os
  artigos disponíveis", "com base na análise" ou qualquer referência ao processo interno.

COMO ESCREVER resposta_cliente (português, pronta para revisão humana):
- encontrou_solucao=true (há solução no contexto): responda DIRETO e objetivo, como um
  técnico experiente que sabe a resposta — entregue a solução sem preâmbulo e sem repetição.
- Se a solução envolve BAIXAR ou ACESSAR algo (uma ferramenta, um patch, o fonte de um
  relatório, um artigo/download do Portal do Cliente TOTVS), INCLUA o LINK ou o caminho exato
  na resposta_cliente — mas SOMENTE se a URL/caminho estiver no <contexto>. Não invente link.
- encontrou_solucao=false (será escalado a um humano): a resposta_cliente será SUBSTITUÍDA
  pelo sistema por uma saudação-padrão curta e acolhedora — você NÃO precisa escrevê-la. Para
  este caso é PROIBIDO, na resposta_cliente: dar solução, pedir que o cliente verifique versão,
  mensagem de erro ou qualquer coisa, e listar passos de investigação. Toda a análise técnica
  (o que investigar, versão a conferir, hipóteses, próximos passos) vai EXCLUSIVAMENTE em
  resumo_para_responsavel — JAMAIS na resposta_cliente.

PEDIDO OPERACIONAL / AÇÃO DA EQUIPE:
- Se o chamado é uma TAREFA a ser EXECUTADA por uma pessoa (cadastro, liberação, ajuste
  manual — ex.: "incluir cadastro do grupo tributário na SX5"), OU um pedido de AÇÃO /
  COORDENAÇÃO da equipe (agendar/reservar horário, rodar/executar uma rotina, providenciar
  algo — ex.: "podem rodar o MRP na sexta?"), defina pedido_operacional=true e
  encontrou_solucao=false. NÃO é uma dúvida com resposta na base — é execução da equipe.
- NUNCA assuma COMPROMISSOS em nome da equipe na resposta_cliente: é PROIBIDO prometer horário,
  data, prazo ou ação ("reservaremos", "faremos", "agendado para", "vamos rodar"). Como
  encontrou_solucao=false, a resposta_cliente será a saudação-padrão; toda a execução/agendamento
  pendente (o que fazer, quando o cliente pediu, dados) vai em resumo_para_responsavel, para a
  equipe providenciar. Caso contrário, pedido_operacional=false.

ALÇADA ADMINISTRATIVA (só administradores executam):
- Se a solução EXIGE ou ENVOLVE uma destas operações, defina alcada_admin=true e tipo_alcada
  com a categoria: alterar PARÂMETRO (ex.: MV_*) → "parâmetro"; criar/alterar GATILHO →
  "gatilho"; criar ou alterar TABELA/CAMPO (tamanho, tipo, etc.) → "tabela/campo"; criar
  USUÁRIO → "usuário".
- MESMO que você tenha encontrado a solução (encontrou_solucao=true), o cliente NÃO pode
  receber os passos — é operação de admin. A resposta_cliente será SUBSTITUÍDA pelo sistema
  pela saudação-padrão (você NÃO a escreve). Coloque a solução/direção COMPLETA, junto de um
  resumo do que o chamado pede, em resumo_para_responsavel — é o que a EQUIPE vai receber para
  resolver. NÃO force encontrou_solucao=false só por ser alçada admin: se você achou a solução
  no contexto, mantenha encontrou_solucao=true e alcada_admin=true.
- Se NÃO envolve nenhuma dessas operações, alcada_admin=false e tipo_alcada="".

Registre sua resposta chamando a ferramenta responder_chamado."""


# --- reformulação de query (ADR-024) ---------------------------------------

_TOOL_QUERY = "registrar_query"
_MAX_TOKENS_QUERY = 300  # uma frase; o teto é folga, não orçamento
# Teto de entrada: o excedente de um e-mail longo não muda a INTENÇÃO, só custa tokens.
_MAX_CHARS_QUERY = 4000
# Reformulação degenerada (modelo devolveu vazio/token solto) → melhor usar o texto original.
_MIN_CHARS_QUERY = 10

SYSTEM_PROMPT_QUERY = """Você prepara a CONSULTA de busca de um sistema de suporte ao ERP
TOTVS Protheus. Recebe o texto de um chamado e devolve a INTENÇÃO de busca dele.

Essa consulta será usada para buscar, por similaridade semântica, em uma base de
documentação oficial TOTVS e de chamados já resolvidos. Ela NUNCA é mostrada ao cliente.

REGRAS:
1. NÃO responda o chamado. NÃO explique. Apenas reescreva a intenção como uma consulta.
2. Escreva como a DOCUMENTAÇÃO se expressa: impessoal e direto (ex.: "Como cadastrar um
   produto no Protheus", "Erro SCC19070 ao gerar SPED fiscal"). Uma frase, no máximo 25
   palavras.
3. PRESERVE LITERALMENTE todo código técnico do chamado: parâmetros (MV_*), campos
   (B1_COD), rotinas (MATA010), módulos (SIGAFIN), tabelas (SX5) e códigos de erro
   (SCC19070). Eles são o sinal mais forte da busca.
4. NUNCA invente código, parâmetro, rotina ou tabela que não esteja no chamado.
5. DESCARTE o que não ajuda a busca: nomes de pessoas e de empresas, saudações,
   agradecimentos, assinaturas, datas, números de chamado, telefones, e-mails e urgência.
6. Preserve o vocabulário do domínio (nomes de rotina, de módulo e do erro). Não traduza
   nem "melhore" termos do Protheus.

Registre a consulta chamando a ferramenta registrar_query."""


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


def _tool_registrar_query() -> dict:
    """Tool cujo input_schema é o schema de QueryReformulada (saída estruturada forçada)."""
    return {
        "name": _TOOL_QUERY,
        "description": "Registra a consulta de busca derivada do chamado.",
        "input_schema": QueryReformulada.model_json_schema(),
    }


def _preservar_codigos(problema: str, query: str) -> str:
    """Reinjeta na query os códigos técnicos que o modelo descartou ao reformular.

    Ponto ÚNICO da garantia (ADR-024): um `MV_ATFMOED` ou `SCC19070` perdido na reescrita
    apaga o sinal mais discriminante da busca vetorial. Em vez de confiar na regra 3 do
    prompt, conferimos em CÓDIGO e reanexamos o que faltar. Comparação em caixa alta porque
    o extrator só reconhece maiúsculas.
    """
    query_maiuscula = query.upper()
    faltando = [c for c in extrair_codigos_tecnicos(problema) if c not in query_maiuscula]
    return f"{query} {' '.join(faltando)}" if faltando else query


def _rotulo_fonte(par: Similar) -> str:
    if par.fonte == "documentacao":
        return f"Fonte: Documentação oficial TOTVS — {par.titulo or 'sem título'}"
    if par.fonte == "web_totvs":
        return (
            "Fonte: Busca web em referência técnica TOTVS/Protheus (oficial ou comunidade) — "
            f"{par.titulo or 'sem título'}"
        )
    return f"Fonte: Chamado anterior #{par.ticket_id}"


def _montar_contexto(pares: Sequence[Similar]) -> str:
    return "\n\n".join(
        f"[Caso {i}] {_rotulo_fonte(par)}\nProblema: {par.problema}\nSolução: {par.solucao}"
        for i, par in enumerate(pares, start=1)
    )


def _acolhimento_padrao_se_escala(resposta: RespostaIA) -> RespostaIA:
    """Força a saudação-padrão ao cliente quando ele NÃO deve receber a solução técnica.

    Ponto ÚNICO da garantia (ADR-022/031): o cliente recebe o texto livre do modelo APENAS
    quando há solução E ela não é de alçada administrativa. Nos demais casos — sem solução
    (ESCALAR) ou solução de alçada admin (ADR-031, o cliente não pode executar parâmetro/
    gatilho/tabela/usuário) — a resposta_cliente é SUBSTITUÍDA pela saudação-padrão. A solução/
    direção permanece em `resumo_para_responsavel` (nota interna + WhatsApp da equipe).
    """
    if resposta.encontrou_solucao and not resposta.alcada_admin:
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
        alcada_admin=False,
        tipo_alcada="",
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
        self._tool_query = _tool_registrar_query()
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

    async def reformular_query(self, problema: str) -> str:
        """Reescreve o chamado como INTENÇÃO de busca, para o embedding do RAG (ADR-024).

        A saída alimenta SOMENTE a busca vetorial: nunca vira contexto do `gerar_resposta`
        nem chega ao cliente. Por isso uma reformulação ruim degrada a recuperação, mas é
        incapaz de alucinar — a regra de ouro segue ancorada nos pares recuperados.

        Devolve o `problema` original quando ele não tem substância ou quando o modelo
        devolve algo degenerado. Erros de API sobem (o pipeline trata como best-effort).
        """
        texto = problema.strip()
        if len(texto) < _MIN_CHARS_QUERY:
            return problema
        message = await self._criar_query(texto[:_MAX_CHARS_QUERY])
        query = self._extrair_query(message).query.strip()
        if len(query) < _MIN_CHARS_QUERY:
            return problema  # reformulação degenerada — o texto original é mais seguro
        return _preservar_codigos(problema, query)

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

    @_retry_claude
    async def _criar_query(self, problema: str):
        return await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS_QUERY,
            temperature=0,
            system=SYSTEM_PROMPT_QUERY,
            tools=[self._tool_query],
            tool_choice={"type": "tool", "name": _TOOL_QUERY},
            messages=[{"role": "user", "content": f"Chamado:\n{problema}"}],
        )

    @staticmethod
    def _extrair(message) -> RespostaIA:
        for bloco in message.content:
            if getattr(bloco, "type", None) == "tool_use" and bloco.name == _TOOL_NAME:
                return RespostaIA.model_validate(bloco.input)
        raise ValueError("O Claude não retornou a ferramenta responder_chamado.")

    @staticmethod
    def _extrair_query(message) -> QueryReformulada:
        for bloco in message.content:
            if getattr(bloco, "type", None) == "tool_use" and bloco.name == _TOOL_QUERY:
                return QueryReformulada.model_validate(bloco.input)
        raise ValueError("O Claude não retornou a ferramenta registrar_query.")
