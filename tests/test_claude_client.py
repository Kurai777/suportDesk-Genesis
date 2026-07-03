"""Testes do Módulo 5 — ClaudeClient.

Cliente Anthropic FALSO injetado — nenhuma chamada real paga.
"""

from app.claude_client import ClaudeClient
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
    assert kwargs["max_tokens"] == 1024

    tool = kwargs["tools"][0]
    assert tool["name"] == "responder_chamado"
    props = tool["input_schema"]["properties"]
    assert "encontrou_solucao" in props
    assert "empresa" not in props  # empresa saiu do contrato de saída do Claude

    # Contexto vai no user message; system é estático e vem com cache_control.
    user_msg = kwargs["messages"][0]["content"]
    assert "<contexto>" in user_msg
    assert "Atualize a taxa da moeda 3" in user_msg  # solução do par entrou no contexto
    assert "problema novo" in user_msg
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_encontrou_solucao_false(settings):
    saida = {**SAIDA_OK, "encontrou_solucao": False, "confianca": "baixa"}
    fake = FakeAnthropic(saida)
    client = ClaudeClient(settings, client=fake)

    resposta = await client.gerar_resposta("problema desconhecido", PARES)

    assert resposta.encontrou_solucao is False
    assert resposta.confianca == "baixa"


async def test_contexto_vazio_curto_circuita_sem_chamar_modelo(settings):
    # Se o modelo fosse chamado, o input {} falharia na validação de RespostaIA.
    fake = FakeAnthropic({})
    client = ClaudeClient(settings, client=fake)

    resposta = await client.gerar_resposta("qualquer chamado", [])

    assert resposta.encontrou_solucao is False
    assert resposta.confianca == "baixa"
    assert fake.messages.chamadas == []  # o modelo NÃO foi chamado
