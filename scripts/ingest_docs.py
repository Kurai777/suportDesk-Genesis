"""Ingestão da DOCUMENTAÇÃO oficial TOTVS (ADR-014) — ⚠️ CONSOME EMBEDDINGS (Voyage).

Segunda fonte de conhecimento, curada à mão: lê `docs_totvs/` (arquivos .md/.txt/.docx)
e grava trechos em `conhecimento` com `fonte='documentacao'`, reaproveitando o schema.
O .docx é lido pela stdlib (zipfile + xml.etree) — sem dependência nova.

- Artigo da Central de Atendimento (blocos "Dúvida"/"Solução"): problema=Dúvida,
  solucao=Solução.
- Documento corrido (manual TDN): fatia por título/seção em trechos de ~500–800 tokens;
  o trecho vai tanto no campo de busca (problema) quanto no de contexto (solucao).

Idempotente pelo BANCO (ADR-016): antes de gravar, consulta se um trecho com o mesmo
(titulo, problema) já existe em `conhecimento`. A tabela é a única fonte da verdade —
à prova de recriação (sem arquivos de estado). Uso: `python -m scripts.ingest_docs`
"""

from __future__ import annotations

import asyncio
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import psycopg
from pgvector.psycopg import register_vector_async

from app.config import Settings, get_settings
from app.rag import RagRepository, VoyageClient

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PASTA_DOCS = Path("docs_totvs")
_EXTENSOES = {".md", ".txt", ".docx"}
_TOKENS_MAX = 800  # teto por trecho ao fatiar documentos corridos (~4 chars/token)

# Word (Office Open XML): o texto vive em word/document.xml, com este namespace.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# Marcadores de seção dos artigos TOTVS. "Problema" cobre os rótulos equivalentes usados
# na prática (Dúvida OU Ocorrência OU Problema); "Solução" cobre Solução/Resolução/
# Procedimento. Sem isso, artigos com "Ocorrência" (muito comuns) caíam no fatiador e
# viravam vários trechos rotulados (ADR-014/017).
_MARCADOR_PROBLEMA = re.compile(
    r"(?im)^\s*#{0,6}\s*\**\s*(d[úu]vida|ocorr[êe]ncia|problema)\s*\**\s*:?"
)
_MARCADOR_SOLUCAO = re.compile(
    r"(?im)^\s*#{0,6}\s*\**\s*(solu[çc][ãa]o|resolu[çc][ãa]o|procedimento)\s*\**\s*:?"
)


@dataclass
class Trecho:
    titulo: str
    problema: str
    solucao: str


@dataclass
class ResumoDocs:
    arquivos: int = 0
    ingeridos: int = 0
    vazios: int = 0
    ja_processados: int = 0


# --- leitura de arquivos (.md / .txt / .docx) ------------------------------


def _nivel_heading(paragrafo: ET.Element) -> int:
    """Nível do heading do Word (Heading1/Título1 → 1…), ou 0 se for parágrafo normal."""
    ppr = paragrafo.find(f"{{{_W_NS}}}pPr")
    style = ppr.find(f"{{{_W_NS}}}pStyle") if ppr is not None else None
    val = (style.get(f"{{{_W_NS}}}val") or "").lower() if style is not None else ""
    if "heading" in val or "titulo" in val or "ttulo" in val:
        m = re.search(r"\d+", val)
        return min(int(m.group()) if m else 2, 6)
    return 0


def _docx_para_texto(caminho: Path) -> str:
    """Extrai o texto de um .docx como markdown-ish (headings do Word viram '#')."""
    with zipfile.ZipFile(caminho) as zf:
        raiz = ET.fromstring(zf.read("word/document.xml"))
    linhas: list[str] = []
    for paragrafo in raiz.iter(f"{{{_W_NS}}}p"):
        texto = "".join(t.text or "" for t in paragrafo.iter(f"{{{_W_NS}}}t")).strip()
        if not texto:
            continue
        nivel = _nivel_heading(paragrafo)
        linhas.append(f"{'#' * nivel} {texto}" if nivel else texto)
    return "\n\n".join(linhas)


def _ler_conteudo(caminho: Path) -> str:
    if caminho.suffix.lower() == ".docx":
        return _docx_para_texto(caminho)
    return caminho.read_text(encoding="utf-8")


# --- parsing (puro e testável) ---------------------------------------------


def _estimar_tokens(texto: str) -> int:
    return max(1, len(texto) // 4)


def _titulo(nome: str, conteudo: str) -> str:
    """Título: 1º heading; senão a 1ª linha real (não-vazia, não-rótulo); senão o arquivo.

    Artigos da base TOTVS (via .docx) não usam headings — o título é a 1ª linha.
    """
    linhas = conteudo.splitlines()
    for linha in linhas:
        m = _HEADING.match(linha)
        if m and not _MARCADOR_PROBLEMA.match(linha) and not _MARCADOR_SOLUCAO.match(linha):
            return m.group(2).strip()
    for linha in linhas:
        despojada = linha.strip()
        if despojada and not _MARCADOR_PROBLEMA.match(despojada) and not _MARCADOR_SOLUCAO.match(
            despojada
        ):
            return despojada
    return Path(nome).stem


def _parsear_duvida_solucao(nome: str, conteudo: str) -> list[Trecho]:
    m_d = _MARCADOR_PROBLEMA.search(conteudo)
    m_s = _MARCADOR_SOLUCAO.search(conteudo)
    if not (m_d and m_s and m_d.end() <= m_s.start()):
        return []
    problema = conteudo[m_d.end() : m_s.start()].strip()
    solucao = conteudo[m_s.end() :].strip()
    if not problema or not solucao:
        return []
    return [Trecho(titulo=_titulo(nome, conteudo), problema=problema, solucao=solucao)]


def _dividir_em_secoes(conteudo: str, titulo_doc: str) -> list[tuple[str, str]]:
    secoes: list[tuple[str, str]] = []
    titulo_atual = titulo_doc
    buffer: list[str] = []
    for linha in conteudo.splitlines():
        m = _HEADING.match(linha)
        if m:
            if buffer:
                secoes.append((titulo_atual, "\n".join(buffer).strip()))
                buffer = []
            titulo_atual = m.group(2).strip()
        else:
            buffer.append(linha)
    if buffer:
        secoes.append((titulo_atual, "\n".join(buffer).strip()))
    return [(t, txt) for t, txt in secoes if txt]


def _dividir_por_tamanho(texto: str) -> list[str]:
    """Junta parágrafos em trechos até ~_TOKENS_MAX tokens."""
    paragrafos = [p.strip() for p in re.split(r"\n\s*\n", texto) if p.strip()]
    trechos: list[str] = []
    atual = ""
    for p in paragrafos:
        candidato = f"{atual}\n\n{p}" if atual else p
        if atual and _estimar_tokens(candidato) > _TOKENS_MAX:
            trechos.append(atual)
            atual = p
        else:
            atual = candidato
    if atual:
        trechos.append(atual)
    return trechos


def _fatiar_documento(nome: str, conteudo: str) -> list[Trecho]:
    titulo_doc = _titulo(nome, conteudo)
    trechos: list[Trecho] = []
    for titulo_secao, texto in _dividir_em_secoes(conteudo, titulo_doc):
        for pedaco in _dividir_por_tamanho(texto):
            trechos.append(Trecho(titulo=titulo_secao, problema=pedaco, solucao=pedaco))
    return trechos


def parsear_documento(nome: str, conteudo: str) -> list[Trecho]:
    if _MARCADOR_PROBLEMA.search(conteudo) and _MARCADOR_SOLUCAO.search(conteudo):
        return _parsear_duvida_solucao(nome, conteudo)
    return _fatiar_documento(nome, conteudo)


# --- ingestão --------------------------------------------------------------


async def ingerir_docs(
    pasta: Path,
    voyage: VoyageClient,
    repo: RagRepository,
) -> ResumoDocs:
    """Ingere os trechos ainda ausentes; idempotência pelo banco (ADR-016).

    A chave estável de um trecho é (titulo, problema) — ambos gravados na linha —,
    então a verificação sobrevive à recriação do banco (sem arquivos de estado).
    """
    resumo = ResumoDocs()
    for caminho in sorted(pasta.iterdir()):
        if caminho.suffix.lower() not in _EXTENSOES:
            continue
        try:
            conteudo = _ler_conteudo(caminho)
        except Exception as exc:  # arquivo corrompido/ilegível — não derruba o lote
            print(f"  ⚠️ ignorando {caminho.name}: {exc}")
            continue
        resumo.arquivos += 1
        for trecho in parsear_documento(caminho.name, conteudo):
            problema = trecho.problema.strip()
            solucao = trecho.solucao.strip()
            if not problema or not solucao:
                resumo.vazios += 1
                continue
            if await repo.doc_ja_ingerido(trecho.titulo, problema):
                resumo.ja_processados += 1
                continue
            [vetor] = await voyage.embed_document([problema])
            await repo.inserir(
                ticket_id=None,
                empresa=None,
                problema=problema,
                solucao=solucao,
                embedding=vetor,
                fonte="documentacao",
                titulo=trecho.titulo,
            )
            resumo.ingeridos += 1
    return resumo


async def main(settings: Settings | None = None) -> ResumoDocs:
    settings = settings or get_settings()
    if not PASTA_DOCS.is_dir():
        print(f"Crie a pasta {PASTA_DOCS}/ e salve os artigos (.md/.txt) antes de rodar.")
        return ResumoDocs()

    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        repo = RagRepository(conn)
        voyage = VoyageClient(settings)
        resumo = await ingerir_docs(PASTA_DOCS, voyage, repo)
    finally:
        await conn.close()

    print(
        f"Docs: {resumo.arquivos} arquivos | {resumo.ingeridos} trechos ingeridos, "
        f"{resumo.vazios} vazios, {resumo.ja_processados} já processados."
    )
    return resumo


if __name__ == "__main__":
    asyncio.run(main())
