"""Testes do ingest_docs (ADR-014) — parsing puro + ingestão com fakes (sem chamada real)."""

import zipfile

from scripts.ingest_docs import (
    _docx_para_texto,
    ingerir_docs,
    parsear_documento,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _criar_docx(caminho, paragrafos):
    """Cria um .docx mínimo (só word/document.xml). paragrafos: list[(texto, nivel)]."""
    corpo = []
    for texto, nivel in paragrafos:
        ppr = f'<w:pPr><w:pStyle w:val="Heading{nivel}"/></w:pPr>' if nivel else ""
        corpo.append(f"<w:p>{ppr}<w:r><w:t>{texto}</w:t></w:r></w:p>")
    doc = (
        f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>'
        + "".join(corpo)
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(caminho, "w") as zf:
        zf.writestr("word/document.xml", doc)

# --- parsing (puro) --------------------------------------------------------


def test_parsear_duvida_solucao():
    conteudo = (
        "# Cálculo do Ativo Fixo\n\n"
        "## Dúvida\n"
        "Qual moeda é usada no cálculo do Ativo Fixo ao lançar a NF?\n\n"
        "## Solução\n"
        "A moeda é definida no parâmetro MV_ATFMOED; atualize a taxa do dia e reprocesse."
    )
    trechos = parsear_documento("ativo_fixo.md", conteudo)

    assert len(trechos) == 1
    t = trechos[0]
    assert t.titulo == "Cálculo do Ativo Fixo"
    assert "Qual moeda" in t.problema
    assert "MV_ATFMOED" in t.solucao


def test_parsear_duvida_solucao_com_label_inline():
    conteudo = "Dúvida: Como emitir a NF?\nSolução: Use a rotina de faturamento MATA461."
    trechos = parsear_documento("nf.txt", conteudo)
    assert len(trechos) == 1
    assert "Como emitir a NF" in trechos[0].problema
    assert "MATA461" in trechos[0].solucao


def test_parsear_ocorrencia_solucao_usa_titulo_real():
    # Artigo com "Ocorrência" (não "Dúvida") deve virar 1 trecho com o título REAL —
    # não fragmentar em seções rotuladas "Ocorrência"/"Ambiente"/"Solução" (ADR-017).
    conteudo = (
        "# Cross Segmento - Linha Protheus - SIGAFIS - Erro X\n\n"
        "## Ocorrência\nAo executar a rotina ocorre o erro X.\n\n"
        "## Ambiente\nProtheus 12.1.\n\n"
        "## Solução\nAjuste o parâmetro MV_XYZ e reprocesse."
    )
    trechos = parsear_documento("fiscal__1__erro-x.txt", conteudo)

    assert len(trechos) == 1
    t = trechos[0]
    assert t.titulo == "Cross Segmento - Linha Protheus - SIGAFIS - Erro X"
    assert "ocorre o erro X" in t.problema  # problema inclui Ocorrência (+ Ambiente)
    assert "MV_XYZ" in t.solucao


def test_titulo_vem_da_primeira_linha_sem_heading():
    # Artigo TOTVS típico (via .docx): sem heading do Word, título = 1ª linha real.
    conteudo = (
        "Nota Fiscal rejeitada na SEFAZ - Linha Protheus\n\n"
        "20 de junho de 2024\n\n"
        "OcorrênciaAo emitir a NF ocorre rejeição. SoluçãoAjuste o cadastro do cliente."
    )
    trechos = parsear_documento("Regra Tributaria.docx", conteudo)
    assert trechos
    assert trechos[0].titulo == "Nota Fiscal rejeitada na SEFAZ - Linha Protheus"


def test_fatiar_documento_corrido_por_secao():
    conteudo = (
        "# Manual de Faturamento\n\n"
        "Introdução ao módulo de faturamento no Protheus.\n\n"
        "## Configuração\n"
        "Passo 1 da configuração.\n\n"
        "Passo 2 da configuração."
    )
    trechos = parsear_documento("manual.md", conteudo)

    titulos = {t.titulo for t in trechos}
    assert "Manual de Faturamento" in titulos
    assert "Configuração" in titulos
    # Documento corrido: o trecho vai tanto na busca quanto no contexto.
    for t in trechos:
        assert t.problema == t.solucao


def test_docx_vira_texto_e_e_parseado(tmp_path):
    caminho = tmp_path / "artigo.docx"
    _criar_docx(
        caminho,
        [
            ("Cálculo do Ativo Fixo", 1),  # heading do Word -> vira '#'
            ("Dúvida", 2),
            ("Qual moeda é usada no cálculo do Ativo Fixo?", 0),
            ("Solução", 2),
            ("Use o parâmetro MV_ATFMOED e atualize a taxa do dia.", 0),
        ],
    )

    texto = _docx_para_texto(caminho)
    assert "# Cálculo do Ativo Fixo" in texto
    assert "## Dúvida" in texto

    trechos = parsear_documento("artigo.docx", texto)
    assert len(trechos) == 1
    assert trechos[0].titulo == "Cálculo do Ativo Fixo"
    assert "MV_ATFMOED" in trechos[0].solucao


# --- ingestão (fakes) ------------------------------------------------------


class FakeVoyage:
    async def embed_document(self, textos: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in textos]


class FakeRepo:
    def __init__(self) -> None:
        self.inseridos: list[dict] = []

    async def inserir(self, **kwargs) -> None:
        self.inseridos.append(kwargs)

    async def doc_ja_ingerido(self, titulo, problema) -> bool:
        # espelha a chave (titulo, problema) do banco (idempotência, ADR-016)
        return any(
            i.get("fonte") == "documentacao"
            and i.get("titulo") == titulo
            and i.get("problema") == problema
            for i in self.inseridos
        )


async def test_ingerir_docs_grava_como_documentacao(tmp_path):
    (tmp_path / "artigo.md").write_text(
        "# Erro SCC19070\n\n## Dúvida\nComo resolver o log SCC19070 no lançamento?\n\n"
        "## Solução\nAtualize a taxa da moeda no parâmetro MV_ATFMOED e reprocesse a NF.",
        encoding="utf-8",
    )
    (tmp_path / "ignorar.pdf").write_text("nada", encoding="utf-8")

    voyage = FakeVoyage()
    repo = FakeRepo()

    resumo = await ingerir_docs(tmp_path, voyage, repo)

    assert resumo.arquivos == 1  # só o .md conta (o .pdf é ignorado)
    assert resumo.ingeridos == 1
    inserido = repo.inseridos[0]
    assert inserido["fonte"] == "documentacao"
    assert inserido["titulo"] == "Erro SCC19070"
    assert inserido["ticket_id"] is None
    assert "MV_ATFMOED" in inserido["solucao"]
    assert len(inserido["embedding"]) == 1024


async def test_ingerir_docs_idempotente(tmp_path):
    (tmp_path / "artigo.md").write_text(
        "# T\n\n## Dúvida\nPergunta longa o suficiente para o parser.\n\n"
        "## Solução\nResposta técnica com detalhes suficientes para valer a pena.",
        encoding="utf-8",
    )
    voyage = FakeVoyage()
    repo = FakeRepo()

    # 2ª rodada: o mesmo (titulo, problema) já está no repo -> idempotência pelo banco
    r1 = await ingerir_docs(tmp_path, voyage, repo)
    r2 = await ingerir_docs(tmp_path, voyage, repo)

    assert r1.ingeridos == 1
    assert r2.ingeridos == 0
    assert r2.ja_processados == 1
    assert len(repo.inseridos) == 1  # não duplicou


async def test_ingerir_docs_suporta_docx(tmp_path):
    _criar_docx(
        tmp_path / "art.docx",
        [
            ("Erro na emissão da NF", 1),
            ("Dúvida", 0),  # rótulo em parágrafo normal também é detectado
            ("Como resolver o erro na emissão da nota?", 0),
            ("Solução", 0),
            ("Atualize a taxa da moeda no parâmetro MV_ATFMOED e reprocesse.", 0),
        ],
    )
    voyage = FakeVoyage()
    repo = FakeRepo()

    resumo = await ingerir_docs(tmp_path, voyage, repo)

    assert resumo.arquivos == 1
    assert resumo.ingeridos == 1
    assert repo.inseridos[0]["fonte"] == "documentacao"
    assert repo.inseridos[0]["titulo"] == "Erro na emissão da NF"
    assert "MV_ATFMOED" in repo.inseridos[0]["solucao"]
