"""Coletor da Central de Atendimento TOTVS (Zendesk Help Center) — ADR-017.

Baixa em volume os artigos oficiais e salva LIMPOS em `docs_totvs/` (título + corpo),
prontos para o `scripts/ingest_docs` consumir. Usa a API pública do Zendesk Help Center
(categorias → seções → artigos, em JSON paginado) — estável e limpo, sem raspar HTML.

Boas maneiras (obrigatório): respeita o robots.txt (`urllib.robotparser`), usa um
User-Agent identificável e faz pausa de ~1,5 s entre requisições. Coleta comedida, uma vez.

Resumível: pula o que já foi baixado (o próprio `.txt` é o estado — sem arquivos de
checkpoint à parte; a idempotência do `ingest_docs` continua sendo no banco, ADR-016).

Conteúdo público. Para seções que exijam login, exporte os cookies de sessão do navegador
em `ZENDESK_COOKIE` (env) ou no arquivo local `zendesk_cookies.txt` — NÃO automatiza
senha/captcha.

Fonte: centraldeatendimento.totvs.com (Central de Atendimento técnica, Zendesk). A categoria
"Cross Segmentos" concentra os módulos do Backoffice (multi-linha) — por isso ALVOS mapeia
temas por padrão no nome da seção e LINHA_FILTRO restringe à linha (padrão: Protheus).

Uso:
  python -m scripts.coletar_central                                      # todos os ALVOS
  python -m scripts.coletar_central --secao 37095895776791 --limite 3    # modo TESTE
  python -m scripts.coletar_central --linha ""                           # todas as linhas
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from urllib.robotparser import RobotFileParser

import httpx
from lxml import html as lh

# --- configuração (FÁCIL DE EDITAR) ----------------------------------------

BASE = "https://centraldeatendimento.totvs.com"  # Central de Atendimento técnica (Zendesk).
LOCALE = "pt-br"
UA = "GenesisConsulting-SupportBot/1.0 (+https://genesisconsulting.com.br; parceiro TOTVS)"
PASTA_SAIDA = Path("docs_totvs")
PAUSA_S = 1.5  # boas maneiras: 1–2 s entre requisições
MIN_CHARS_CORPO = 200  # abaixo disso, provável shell dinâmico (JS) — pula
TIMEOUT = httpx.Timeout(20.0)

# Filtro de linha ("Cross Segmentos" é multi-linha). REGEX de sinais de Protheus no
# título: "Protheus", "Microsiga" ou um código de módulo SIGAxxx (exclusivo do Protheus).
# Só "Protheus" comia artigos bons titulados "MP - SIGAFIN…" (MP = Microsiga Protheus) ou
# "Cross Segmentos – SIGAFIN". "" = sem filtro. (ADR-017)
LINHA_FILTRO = r"protheus|microsiga|siga[a-z]{3}"


@dataclass(frozen=True)
class Alvo:
    """O que coletar. `tipo`:
    - 'categoria' (todas as seções da categoria `id`),
    - 'secao' (a seção `id`),
    - 'filtro' (seções da categoria `id` cujo NOME casa com o regex `padrao`),
    - 'busca' (API de search do Zendesk pela query `padrao` — para mirar rotinas
      específicas, ex.: MATA010, que aparecem no TÍTULO, não no nome da seção).
    O filtro de linha (LINHA_FILTRO) é aplicado em todos os tipos."""

    tema: str
    tipo: str
    id: int
    padrao: str | None = None  # regex no nome da seção ('filtro') OU query ('busca')


# Temas → seções da categoria "Cross Segmentos" (Backoffice, 650 seções), por padrão no
# nome da seção; RH tem categoria própria. FÁCIL DE EDITAR (calibrar os padrões/linha).
_CROSS = 360005280714  # Cross Segmentos (Backoffice Protheus/RM/Datasul/Logix)
_RH = 1500000346941  # TOTVS RH
ALVOS: list[Alvo] = [
    Alvo("financeiro", "filtro", _CROSS, r"financeiro"),
    Alvo("compras", "filtro", _CROSS, r"compras|suprimento|cotaç"),
    Alvo("relatorios", "filtro", _CROSS, r"relat[oó]ri|smart view|dashboard"),
    Alvo("fiscal", "filtro", _CROSS, r"fiscal|tribut|sped|apuraç"),
    Alvo("nf", "filtro", _CROSS, r"nota fiscal|nf-?e|nfc-?e|nfs-?e|nfcom|danfe"),
    Alvo("estoque", "filtro", _CROSS, r"estoque|invent|almoxarif"),
    # RH: categoria própria (379 seções, ~12,5k artigos, só ~6% Protheus). Coleta CIRÚRGICA
    # por subtema de RH-Protheus (GPE/Folha/Férias/Ponto/Cargos → SIGAGPE/SIGAPON/SIGACSA);
    # o filtro de linha descarta as seções de produtos não-Protheus (Pontoweb/Velti/Meu RH).
    Alvo("rh", "filtro", _RH, r"\bGPE\b|folha|f[eé]rias|ponto|cargos|sigagpe|sigapon"),
    # Coleta DIRIGIDA de how-tos operacionais (ADR-021): busca por ROTINA na API de search,
    # filtrada pela linha Protheus. Mira os temas de maior retorno do diagnóstico (COLETA.md).
    Alvo("acesso", "busca", 0, "FATA900 liberação de acesso de usuário"),
    Alvo("cadastro-produto", "busca", 0, "MATA010 como cadastrar produto"),
    # tributação (MATA020 grupo de tributação): a Central NÃO tem o how-to Protheus — a busca
    # devolve ADVPL/SIGAFAT (fora do alvo). Está só no TDN (não coberto por este coletor).
    # Follow-up ADR-021: coletar do TDN. NÃO reativar aqui como 'busca' (traz ruído).
]

# Linhas de boilerplate (menu/rodapé/acessibilidade/avisos) a remover do corpo. Editável.
_BOILERPLATE = (
    "ouvir descrição",
    "tempo aproximado para leitura",
    "este artigo foi útil",
    "voltar ao topo",
    "compartilhe este artigo",
)

# Tags cujo conteúdo NÃO é texto do artigo (script/estilo/navegação/botões...).
_TAGS_LIXO = (
    "script", "style", "head", "nav", "footer", "header",
    "button", "svg", "noscript", "form", "iframe",
)


# --- limpeza do corpo (pura e testável) ------------------------------------


def _limpar_corpo(body_html: str) -> str:
    """Extrai o texto do artigo do HTML do Zendesk: dropa script/estilo/nav, promove
    títulos a `## ...` (o ingest_docs entende) e remove boilerplate. '' se não houver
    conteúdo (ex.: artigos que carregam o corpo por JavaScript)."""
    if not body_html or not body_html.strip():
        return ""
    try:
        doc = lh.fromstring(body_html)
    except Exception:
        return ""
    for tag in _TAGS_LIXO:
        for el in list(doc.iter(tag)):
            el.drop_tree()
    for h in list(doc.iter("h1", "h2", "h3", "h4")):
        titulo = " ".join(h.text_content().split())
        for filho in list(h):
            h.remove(filho)
        h.text = f"\n\n## {titulo}\n" if titulo else ""
    _promover_rotulos_html(doc)
    return _normalizar(_promover_rotulos(doc.text_content()))


# Rótulos de seção como ELEMENTO próprio (`<strong>Solução</strong>`, `<p>Ocorrência</p>`):
# no HTML são inequivocamente cabeçalhos, então promovê-los é confiável mesmo espaçados —
# pega o resíduo que a heurística de texto (rótulo grudado) não alcança (ADR-017).
_ROTULOS_SECAO = {
    "dúvida", "duvida", "ocorrência", "ocorrencia", "problema", "ambiente", "causa",
    "solução", "solucao", "resolução", "resolucao", "procedimento", "passo a passo",
}


def _promover_rotulos_html(doc) -> None:
    for el in list(doc.iter()):
        if not isinstance(el.tag, str):  # pula comentários/PIs (HtmlComment não tem texto)
            continue
        if len(el):  # só elementos-folha (o rótulo é o texto inteiro do elemento)
            continue
        texto = " ".join((el.text_content() or "").split())
        if texto.lower().rstrip(":").strip() in _ROTULOS_SECAO and len(texto) <= 20:
            el.text = f"\n\n## {texto.rstrip(':').strip()}\n"


# Rótulos de seção dos artigos TOTVS. O layout varia (ora "…Solução", ora "DúvidaComo…"),
# então decidimos por rótulo: é cabeçalho quando está GRUDADO — colado a uma minúscula à
# esquerda OU a uma maiúscula à direita (o CMS comeu o espaço do bold). Promovê-lo a `## `
# faz o ingest_docs separar problema (Dúvida/Ocorrência) da solução. Um rótulo cercado de
# espaços é ambíguo (pode ser prosa) — deixamos como está (vira documento corrido, ok).
_ROTULOS_RE = re.compile(
    r"(Dúvida|Ocorrência|Ambiente|Causa|Solução|Resolução|Procedimento)"
)


def _promover_rotulos(texto: str) -> str:
    def promover(m: re.Match) -> str:
        antes = texto[m.start() - 1] if m.start() else "\n"
        depois = texto[m.end()] if m.end() < len(texto) else "\n"
        grudado = antes.islower() or depois.isupper()
        return f"\n\n## {m.group(1)}\n" if grudado else m.group(0)

    return _ROTULOS_RE.sub(promover, texto)


def _normalizar(texto: str) -> str:
    texto = texto.replace(" ", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    linhas: list[str] = []
    for linha in texto.splitlines():
        despojada = linha.strip()
        if not despojada:
            linhas.append("")
        elif not any(b in despojada.lower() for b in _BOILERPLATE):
            linhas.append(despojada)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def _documento(titulo: str, corpo: str) -> str:
    """Formato salvo: título como heading (o ingest_docs usa como `titulo`) + corpo."""
    return f"# {titulo}\n\n{corpo}\n"


def _slug(texto: str, limite: int = 60) -> str:
    ascii_ = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_).strip("-").lower()
    return s[:limite] or "artigo"


def _nome_arquivo(tema: str, artigo_id: int, titulo: str) -> str:
    return f"{tema}__{artigo_id}__{_slug(titulo)}.txt"


def _ja_baixado(pasta: Path, tema: str, artigo_id: int) -> bool:
    """Idempotência: o próprio .txt (por tema+id) é o estado — resume sem recoletar."""
    return any(pasta.glob(f"{tema}__{artigo_id}__*.txt"))


# --- coleta (I/O) ----------------------------------------------------------


@dataclass
class ResumoColeta:
    salvos: int = 0
    ja_baixados: int = 0
    pulados_draft: int = 0
    sem_corpo: int = 0  # corpo abaixo do mínimo (provável shell-JS)
    fora_da_linha: int = 0  # artigo de outra linha de produto (ver LINHA_FILTRO)
    exemplos: list[str] = field(default_factory=list)  # nomes dos arquivos salvos
    exemplos_descartados: list[str] = field(default_factory=list)  # títulos fora da linha


def _cookies_de_sessao() -> str | None:
    """Cookies exportados do navegador (ZENDESK_COOKIE ou zendesk_cookies.txt)."""
    valor = os.environ.get("ZENDESK_COOKIE")
    if valor:
        return valor.strip()
    arquivo = Path("zendesk_cookies.txt")
    if arquivo.exists():
        return arquivo.read_text(encoding="utf-8").strip() or None
    return None


async def carregar_robots(client: httpx.AsyncClient, base: str) -> RobotFileParser:
    parser = RobotFileParser()
    try:
        resp = await client.get(f"{base}/robots.txt")
        parser.parse(resp.text.splitlines())
    except Exception:
        parser.parse([])  # robots inacessível: não bloqueia (mas seguimos comedidos)
    return parser


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    robots: RobotFileParser | None,
    pausa_s: float,
) -> dict | None:
    if robots is not None and not robots.can_fetch(UA, url):
        return None  # respeita o robots.txt
    resp = await client.get(url)
    resp.raise_for_status()
    if pausa_s:
        await asyncio.sleep(pausa_s)  # boas maneiras entre requisições
    return resp.json()


async def _paginar(
    client: httpx.AsyncClient,
    url: str,
    chave: str,
    *,
    robots: RobotFileParser | None,
    pausa_s: float,
) -> list[dict]:
    """Segue `next_page` do Help Center, acumulando `j[chave]`."""
    itens: list[dict] = []
    proxima: str | None = url
    while proxima:
        pagina = await _get_json(client, proxima, robots=robots, pausa_s=pausa_s)
        if not pagina:
            break
        itens.extend(pagina.get(chave, []))
        proxima = pagina.get("next_page")
    return itens


async def _artigos_do_alvo(
    client: httpx.AsyncClient,
    alvo: Alvo,
    *,
    robots: RobotFileParser | None,
    pausa_s: float,
) -> AsyncIterator[dict]:
    api = f"{BASE}/api/v2/help_center/{LOCALE}"
    if alvo.tipo == "busca":
        # API de search do Zendesk: top resultados por relevância (1 página; o filtro de
        # linha + `limite` refinam). Não pagina as ~1000 correspondências fuzzy.
        url = (f"{BASE}/api/v2/help_center/articles/search.json"
               f"?query={quote(alvo.padrao or '')}&per_page=50")
        j = await _get_json(client, url, robots=robots, pausa_s=pausa_s)
        for artigo in (j.get("results", []) if j else []):
            yield artigo
        return
    if alvo.tipo in ("categoria", "filtro"):
        secoes = await _paginar(
            client, f"{api}/categories/{alvo.id}/sections.json", "sections",
            robots=robots, pausa_s=pausa_s,
        )
        if alvo.tipo == "filtro" and alvo.padrao:
            regex = re.compile(alvo.padrao, re.IGNORECASE)
            secoes = [s for s in secoes if regex.search(s.get("name", ""))]
        ids = [s["id"] for s in secoes]
    else:
        ids = [alvo.id]
    for sid in ids:
        artigos = await _paginar(
            client, f"{api}/sections/{sid}/articles.json", "articles",
            robots=robots, pausa_s=pausa_s,
        )
        for artigo in artigos:
            yield artigo


async def coletar(
    client: httpx.AsyncClient,
    alvos: list[Alvo],
    pasta: Path,
    *,
    robots: RobotFileParser | None = None,
    limite: int | None = None,
    pausa_s: float = 0.0,
    linha: str = "",
) -> ResumoColeta:
    """Percorre os alvos e salva um .txt limpo por artigo. `limite`: modo teste (N no total).
    `linha`: REGEX; se preenchido, só salva artigos cujo TÍTULO casa o padrão (sinais de
    Protheus). Vazio = sem filtro."""
    pasta.mkdir(parents=True, exist_ok=True)
    padrao_linha = re.compile(linha, re.IGNORECASE) if linha else None
    resumo = ResumoColeta()
    for alvo in alvos:
        async for artigo in _artigos_do_alvo(client, alvo, robots=robots, pausa_s=pausa_s):
            if limite is not None and resumo.salvos >= limite:
                return resumo
            if artigo.get("draft"):
                resumo.pulados_draft += 1
                continue
            artigo_id, titulo = artigo["id"], artigo.get("title") or "sem título"
            if padrao_linha and not padrao_linha.search(titulo):
                resumo.fora_da_linha += 1
                if len(resumo.exemplos_descartados) < 15:
                    resumo.exemplos_descartados.append(titulo)
                continue
            if _ja_baixado(pasta, alvo.tema, artigo_id):
                resumo.ja_baixados += 1
                continue
            corpo = _limpar_corpo(artigo.get("body") or "")
            if len(corpo) < MIN_CHARS_CORPO:
                resumo.sem_corpo += 1
                continue
            nome = _nome_arquivo(alvo.tema, artigo_id, titulo)
            (pasta / nome).write_text(_documento(titulo, corpo), encoding="utf-8")
            resumo.salvos += 1
            if len(resumo.exemplos) < 10:
                resumo.exemplos.append(nome)
    return resumo


async def main() -> ResumoColeta:
    ap = argparse.ArgumentParser(description="Coletor da Central de Atendimento TOTVS")
    ap.add_argument("--secao", type=int, help="coleta só esta seção (modo teste)")
    ap.add_argument("--categoria", type=int, help="coleta só esta categoria")
    ap.add_argument("--temas", help="coleta só estes temas de ALVOS (separados por vírgula)")
    ap.add_argument("--tema", default="teste", help="tema (prefixo do arquivo salvo)")
    ap.add_argument("--limite", type=int, help="máx. de artigos salvos (modo teste)")
    ap.add_argument("--saida", type=Path, default=PASTA_SAIDA, help="pasta de saída")
    ap.add_argument("--linha", default=LINHA_FILTRO, help="linha de produto (título); '' = todas")
    args = ap.parse_args()

    if args.secao:
        alvos = [Alvo(args.tema, "secao", args.secao)]
    elif args.categoria:
        alvos = [Alvo(args.tema, "categoria", args.categoria)]
    elif args.temas:
        quero = {t.strip() for t in args.temas.split(",")}
        alvos = [a for a in ALVOS if a.tema in quero]
    else:
        alvos = ALVOS

    cabecalhos = {"User-Agent": UA}
    if (cookie := _cookies_de_sessao()) is not None:
        cabecalhos["Cookie"] = cookie
        print("Usando cookies de sessão (ZENDESK_COOKIE/zendesk_cookies.txt).")

    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=cabecalhos, follow_redirects=True
    ) as client:
        robots = await carregar_robots(client, BASE)
        resumo = await coletar(
            client, alvos, args.saida, robots=robots,
            limite=args.limite, pausa_s=PAUSA_S, linha=args.linha,
        )

    print(
        f"Coleta: {resumo.salvos} salvos, {resumo.ja_baixados} já baixados, "
        f"{resumo.pulados_draft} rascunhos, {resumo.sem_corpo} sem corpo (shell-JS), "
        f"{resumo.fora_da_linha} de outra linha."
    )
    for nome in resumo.exemplos:
        print(f"  📄 {nome}")
    if resumo.exemplos_descartados:
        print("  descartados por linha (amostra):")
        for titulo in resumo.exemplos_descartados:
            print(f"    ✂️  {titulo}")
    return resumo


if __name__ == "__main__":
    asyncio.run(main())
