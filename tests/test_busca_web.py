"""Testes do BuscaWebClient (ADR-015) — busca e fetch MOCKADOS (nenhuma chamada real).

O buscador (ddgs) e o fetcher (httpx) são injetados por fakes. A extração de texto
(trafilatura) roda de verdade sobre HTML sintético — é pura e local.
"""

from app.busca_web import _DOMINIOS, BuscaWebClient, _url_alvo

# HTML de artigo TOTVS-ish; trafilatura extrai o <article> e descarta nav/rodapé.
_HTML_289 = """<!DOCTYPE html><html lang="pt-br"><head><title>NF-e rejeição 289</title></head>
<body><nav>menu início contato</nav>
<article><h1>Rejeição 289 - Código Município inexistente</h1>
<p>Ao emitir a NF-e o sistema retorna a rejeição 289 informando que o código do município
do fato gerador do transporte está inexistente. Isso ocorre quando o cadastro do município
no Protheus está com o código IBGE incorreto ou ausente na tabela CC2.</p>
<p>Para resolver, acesse a rotina de cadastro de municípios, localize o município informado
na nota e preencha o código IBGE correto no campo apropriado. Depois reprocesse a NF-e.</p>
</article><footer>rodapé totvs 2025</footer></body></html>"""

_HTML_CURTO = "<html><body><p>erro 500</p></body></html>"  # curto demais -> descartado


class FakeBuscador:
    """Substitui o ddgs: registra as queries e devolve resultados canned."""

    def __init__(self, resultados: list[dict] | None = None, erro: Exception | None = None):
        self._resultados = resultados or []
        self._erro = erro
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[dict]:
        self.queries.append(query)
        if self._erro is not None:
            raise self._erro
        return self._resultados


class FakeFetcher:
    """Substitui o httpx: mapeia url -> HTML; url ausente levanta (página fora do ar)."""

    def __init__(self, paginas: dict[str, str], erro: Exception | None = None):
        self._paginas = paginas
        self._erro = erro
        self.abertas: list[str] = []

    async def __call__(self, url: str) -> str:
        self.abertas.append(url)
        if self._erro is not None:
            raise self._erro
        return self._paginas[url]


async def test_buscar_extrai_texto_das_paginas():
    buscador = FakeBuscador(
        [
            {"title": "Rej 289", "href": "https://centraldeatendimento.totvs.com/artigo-289"},
            {"title": "TDN", "href": "https://tdn.totvs.com/pagina-x"},
        ]
    )
    fetcher = FakeFetcher(
        {
            "https://centraldeatendimento.totvs.com/artigo-289": _HTML_289,
            "https://tdn.totvs.com/pagina-x": _HTML_289,
        }
    )
    client = BuscaWebClient(buscador=buscador, fetcher=fetcher)

    trechos = await client.buscar("NF-e rejeição 289 código município")

    assert len(trechos) == 2
    assert all("código IBGE correto" in t for t in trechos)
    # A origem (URL) é preservada no início do trecho, para o revisor rastrear.
    assert trechos[0].startswith("[https://centraldeatendimento.totvs.com/artigo-289]")


async def test_query_restrita_aos_dominios_oficiais():
    buscador = FakeBuscador([])
    client = BuscaWebClient(buscador=buscador, fetcher=FakeFetcher({}))

    await client.buscar("erro qualquer no Protheus")

    assert len(buscador.queries) == 1
    query = buscador.queries[0]
    assert "site:centraldeatendimento.totvs.com" in query
    assert "site:tdn.totvs.com" in query
    assert query.endswith(_DOMINIOS)


async def test_paginas_curtas_ou_fora_do_ar_sao_ignoradas():
    buscador = FakeBuscador(
        [
            {"href": "https://tdn.totvs.com/boa"},
            {"href": "https://tdn.totvs.com/curta"},
        ]
    )
    fetcher = FakeFetcher(
        {
            "https://tdn.totvs.com/boa": _HTML_289,
            "https://tdn.totvs.com/curta": _HTML_CURTO,  # extração < mínimo -> fora
        }
    )
    client = BuscaWebClient(buscador=buscador, fetcher=fetcher)

    trechos = await client.buscar("problema")

    assert len(trechos) == 1  # só a página com conteúdo suficiente


async def test_buscador_falha_retorna_vazio_sem_levantar():
    buscador = FakeBuscador(erro=RuntimeError("rate limit / IP bloqueado"))
    client = BuscaWebClient(buscador=buscador, fetcher=FakeFetcher({}))

    assert await client.buscar("qualquer") == []


async def test_fetch_falha_retorna_vazio_sem_levantar():
    buscador = FakeBuscador([{"href": "https://tdn.totvs.com/fora"}])
    fetcher = FakeFetcher({}, erro=TimeoutError("página fora do ar"))
    client = BuscaWebClient(buscador=buscador, fetcher=fetcher)

    assert await client.buscar("qualquer") == []


def test_url_zendesk_vira_api_de_help_center():
    # Central de Atendimento bloqueia o HTML (403) -> roteia para a API pública por id.
    api, e_zendesk = _url_alvo(
        "https://centraldeatendimento.totvs.com/hc/pt-br/articles/360034102754-Varejo-Moda"
    )
    assert e_zendesk is True
    assert api == (
        "https://centraldeatendimento.totvs.com/api/v2/help_center/articles/360034102754.json"
    )


def test_url_tdn_vai_direto():
    alvo, e_zendesk = _url_alvo("https://tdn.totvs.com/pages/viewpage.action?pageId=123")
    assert e_zendesk is False
    assert alvo == "https://tdn.totvs.com/pages/viewpage.action?pageId=123"


async def test_cache_evita_repetir_a_mesma_consulta():
    buscador = FakeBuscador([{"href": "https://tdn.totvs.com/boa"}])
    fetcher = FakeFetcher({"https://tdn.totvs.com/boa": _HTML_289})
    client = BuscaWebClient(buscador=buscador, fetcher=fetcher)

    t1 = await client.buscar("Erro EDI reposição QTxITENS")
    t2 = await client.buscar("erro   edi   REPOSIÇÃO   qtxitens")  # mesma coisa normalizada

    assert t1 == t2
    assert len(buscador.queries) == 1  # 2ª veio do cache — não repetiu a busca
    assert len(fetcher.abertas) == 1
