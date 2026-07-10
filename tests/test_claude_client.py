"""Testes do Módulo 5 — ClaudeClient.

Cliente Anthropic FALSO injetado — nenhuma chamada real paga.
"""

from app.claude_client import (
    RESPOSTA_ESCALAR_PADRAO,
    SYSTEM_PROMPT,
    ClaudeClient,
    _resposta_sem_contexto,
)
from app.models import RespostaIA
from app.rag import Similar

PARES = [
    Similar(
        ticket_id=1,
        problema="Erro no lançamento da NF, log SCC19070.",
        solucao="Atualize a taxa da moeda 3 e reprocesse a NF.",
        empresa="Cliente A",
        distancia=0.05,
    )
]

SAIDA_OK = {
    "resposta_cliente": "Atualize a taxa da moeda 3 e reprocesse a NF.",
    "encontrou_solucao": True,
    "confianca": "alta",
    "resumo_para_responsavel": "Moeda 3 do Ativo Fixo sem taxa do dia.",
    "urgencia": "alta",
    "pedido_operacional": False,
}


class _Bloco:
    def __init__(self, type: str, name: str | None = None, input: dict | None = None):
        self.type = type
        self.name = name
        self.input = input


class _Msg:
    def __init__(self, content: list[_Bloco]):
        self.content = content


class _FakeMessages:
    def __init__(self, tool_input: dict):
        self._tool_input = tool_input
        self.chamadas: list[dict] = []

    async def create(self, **kwargs):
        self.chamadas.append(kwargs)
        return _Msg(
            [
                _Bloco("text", None, None),  # bloco de texto antes deve ser ignorado
                _Bloco("tool_use", name="responder_chamado", input=self._tool_input),
            ]
        )


class FakeAnthropic:
    def __init__(self, tool_input: dict):
        self.messages = _FakeMessages(tool_input)


# --- casos -----------------------------------------------------------------


async def test_gerar_resposta_valida_parseia_respostaia(settings):
    fake = FakeAnthropic(SAIDA_OK)
    client = ClaudeClient(settings, client=fake)

    resposta = await client.gerar_resposta("Erro SCC19070 na NF", PARES)

    assert isinstance(resposta, RespostaIA)
    assert resposta.encontrou_solucao is True
    assert resposta.confianca == "alta"


async def test_tool_choice_forca_a_tool_e_parametros(settings):
    fake = FakeAnthropic(SAIDA_OK)
    client = ClaudeClient(settings, client=fake)

    await client.gerar_resposta("problema novo", PARES)

    kwargs = fake.messages.chamadas[-1]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "responder_chamado"}
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["temperature"] == 0
    assert kwargs["max_tokens"] == 2048

    tool = kwargs["tools"][0]
    assert tool["name"] == "responder_chamado"
    props = tool["input_schema"]["properties"]
    assert "encontrou_solucao" in props
    assert "pedido_operacional" in props  # Claude sinaliza pedido operacional (ADR-020)
    assert "empresa" not in props  # empresa saiu do contrato de saída do Claude

    # Contexto vai no user message; system é estático e vem com cache_control.
    user_msg = kwargs["messages"][0]["content"]
    assert "<contexto>" in user_msg
    assert "Atualize a taxa da moeda 3" in user_msg  # solução do par entrou no contexto
    assert "problema novo" in user_msg
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_contexto_rotula_a_fonte(settings):
    fake = FakeAnthropic(SAIDA_OK)
    client = ClaudeClient(settings, client=fake)
    pares = [
        Similar(
            ticket_id=None,
            problema="Cálculo do Ativo Fixo",
            solucao="Atualize a taxa da moeda 3.",
            empresa=None,
            distancia=0.1,
            fonte="documentacao",
            titulo="Ativo Fixo — moeda de cálculo",
        ),
        Similar(
            ticket_id=42,
            problema="Erro na NF",
            solucao="Reprocessar.",
            empresa="Cliente X",
            distancia=0.2,
        ),
    ]

    await client.gerar_resposta("problema", pares)

    user_msg = fake.messages.chamadas[-1]["messages"][0]["content"]
    assert "Fonte: Documentação oficial TOTVS — Ativo Fixo — moeda de cálculo" in user_msg
    assert "Fonte: Chamado anterior #42" in user_msg


async def test_encontrou_solucao_false(settings):
    saida = {**SAIDA_OK, "encontrou_solucao": False, "confianca": "baixa"}
    fake = FakeAnthropic(saida)
    client = ClaudeClient(settings, client=fake)

    resposta = await client.gerar_resposta("problema desconhecido", PARES)

    assert resposta.encontrou_solucao is False
    assert resposta.confianca == "baixa"
    # Escalar: a resposta ao cliente é o texto-modelo FIXO, não o texto do modelo (ADR-022).
    assert resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO


async def test_escalar_forca_texto_modelo_fixo_e_nao_vaza(settings):
    # O modelo tenta VAZAR: menciona a base e manda o cliente verificar versão/erro/passos.
    vazando = {
        **SAIDA_OK,
        "encontrou_solucao": False,
        "confianca": "baixa",
        "resposta_cliente": (
            "Não consta na nossa base. Verifique a versão do Protheus e a mensagem de erro, "
            "e nos informe o passo a passo que você seguiu."
        ),
        "resumo_para_responsavel": "Sem caso na base; conferir versão do release e o log SCC.",
    }
    client = ClaudeClient(settings, client=FakeAnthropic(vazando))

    resposta = await client.gerar_resposta("problema", PARES)

    # resposta ao cliente = saudação-padrão, SEM exceção...
    assert resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO
    baixo = resposta.resposta_cliente.lower()
    for proibido in ("base", "versão", "verifique", "erro", "não consta", "passo a passo"):
        assert proibido not in baixo
    # ...mas a análise técnica do modelo é PRESERVADA para o time (nota interna).
    assert "versão" in resposta.resumo_para_responsavel.lower()


async def test_contexto_vazio_curto_circuita_sem_chamar_modelo(settings):
    # Se o modelo fosse chamado, o input {} falharia na validação de RespostaIA.
    fake = FakeAnthropic({})
    client = ClaudeClient(settings, client=fake)

    resposta = await client.gerar_resposta("qualquer chamado", [])

    assert resposta.encontrou_solucao is False
    assert resposta.confianca == "baixa"
    assert fake.messages.chamadas == []  # o modelo NÃO foi chamado


# --- tom ao cliente × verdade técnica (ADR-020) ----------------------------


def test_system_prompt_protege_o_cliente_e_mantem_regra_de_ouro():
    p = SYSTEM_PROMPT
    # Regra de ouro intacta:
    assert "EXCLUSIVAMENTE com base no que estiver dentro do bloco <contexto>" in p
    assert "encontrou_solucao=false" in p
    # resposta_cliente NÃO pode revelar o processo interno (citados como PROIBIDO):
    for proibido in ('"base de conhecimento"', '"IA"', '"não encontrei"', '"com base na análise"'):
        assert proibido in p
    # escalar: resposta ao cliente é a saudação-padrão; PROIBIDO pedir verificação de versão;
    # a análise técnica vai só no resumo ao time (ADR-022):
    assert "saudação-padrão" in p
    assert "verifique versão" in p  # citado como PROIBIDO na resposta ao cliente
    # pedido operacional + dois públicos:
    assert "pedido_operacional=true" in p
    assert "VAI AO CLIENTE" in p and "resumo_para_responsavel" in p


def test_resposta_sem_contexto_acolhe_sem_revelar_processo():
    r = _resposta_sem_contexto()
    assert r.encontrou_solucao is False and r.pedido_operacional is False
    texto = r.resposta_cliente.lower()
    for vazamento in ("base", "não encontr", "não localiz", "artigos", "inteligência"):
        assert vazamento not in texto
    assert "analisado pelo nosso time" in texto  # acolhimento
    assert "base" in r.resumo_para_responsavel.lower()  # o time vê a verdade técnica


async def test_parseia_pedido_operacional(settings):
    saida = {**SAIDA_OK, "encontrou_solucao": False, "pedido_operacional": True}
    client = ClaudeClient(settings, client=FakeAnthropic(saida))

    resposta = await client.gerar_resposta("Incluir cadastro do grupo tributário na SX5", PARES)

    assert resposta.pedido_operacional is True
    assert resposta.encontrou_solucao is False
    # Pedido operacional também escala → resposta ao cliente é a saudação-padrão (ADR-022).
    assert resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO
