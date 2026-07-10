"""Testes do VisaoClient (ADR-023) — transcrição de imagens.

Cliente Anthropic FALSO injetado — nenhuma chamada real paga. Regra de ouro: transcreve só
o legível; ilegível/sem texto/tipo não suportado -> string vazia.
"""

import base64

from app.visao import _MARCADOR_VAZIO, VisaoClient

PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


class _BlocoTexto:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _Msg:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    def __init__(self, texto: str):
        self._texto = texto
        self.chamadas: list[dict] = []

    async def create(self, **kwargs):
        self.chamadas.append(kwargs)
        return _Msg([_BlocoTexto(self._texto)])


class FakeVisaoAnthropic:
    def __init__(self, texto: str):
        self.messages = _FakeMessages(texto)


# --- casos -----------------------------------------------------------------


async def test_transcreve_texto_legivel(settings):
    fake = FakeVisaoAnthropic("SCC19070\nMV_ATFMOED sem taxa da moeda 3")
    client = VisaoClient(settings, client=fake)

    texto = await client.transcrever(PNG, "image/png")

    assert "SCC19070" in texto
    assert "MV_ATFMOED" in texto


async def test_monta_bloco_de_imagem_base64(settings):
    fake = FakeVisaoAnthropic("qualquer")
    client = VisaoClient(settings, client=fake)

    await client.transcrever(PNG, "image/png")

    kwargs = fake.messages.chamadas[-1]
    conteudo = kwargs["messages"][0]["content"]
    img = next(b for b in conteudo if b["type"] == "image")
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"] == base64.standard_b64encode(PNG).decode("ascii")
    assert any(b["type"] == "text" for b in conteudo)  # instrução de transcrição
    assert kwargs["temperature"] == 0


async def test_marcador_vazio_vira_string_vazia(settings):
    client = VisaoClient(settings, client=FakeVisaoAnthropic(_MARCADOR_VAZIO))
    assert await client.transcrever(PNG, "image/png") == ""


async def test_resposta_vazia_do_modelo_vira_vazio(settings):
    client = VisaoClient(settings, client=FakeVisaoAnthropic("   "))
    assert await client.transcrever(PNG, "image/png") == ""


async def test_tipo_nao_suportado_nao_chama_modelo(settings):
    fake = FakeVisaoAnthropic("não deveria ser usado")
    client = VisaoClient(settings, client=fake)

    resultado = await client.transcrever(b"%PDF-1.4", "application/pdf")

    assert resultado == ""
    assert fake.messages.chamadas == []  # modelo NÃO foi chamado


async def test_bytes_vazios_nao_chama_modelo(settings):
    fake = FakeVisaoAnthropic("x")
    client = VisaoClient(settings, client=fake)

    assert await client.transcrever(b"", "image/png") == ""
    assert fake.messages.chamadas == []


async def test_content_type_com_charset_e_normalizado(settings):
    # "image/png; charset=..." deve ser aceito (usa só o mime antes do ';').
    fake = FakeVisaoAnthropic("texto")
    client = VisaoClient(settings, client=fake)

    assert await client.transcrever(PNG, "image/png; charset=binary") == "texto"
    assert fake.messages.chamadas  # chamou o modelo
