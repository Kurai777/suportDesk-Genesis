"""Busca web como ÚLTIMO RECURSO (ADR-015/038), restrita a domínios TOTVS/Protheus confiáveis.

Só é acionada pelo pipeline quando a base local (chamados + docs) NÃO resolveu — nunca
antes. Consulta o DuckDuckGo (via `ddgs`) limitando a busca aos domínios da allowlist:
os OFICIAIS TOTVS (Central de Atendimento, TDN) + REFERÊNCIAS TÉCNICAS da comunidade Protheus
que o time considera confiáveis (BlackTDN, UserFunction, etc. — ADR-038). Abre os 2–3 primeiros
resultados e extrai o texto principal de cada página (trafilatura). Esse texto vira <contexto>.

BEST-EFFORT: qualquer falha (rate limit, bloqueio de IP, página fora do ar, timeout,
HTML sem conteúdo) devolve lista vazia SEM levantar exceção — a busca web nunca pode
derrubar o processamento de um chamado.

A Central de Atendimento (centraldeatendimento.totvs.com) é um Zendesk que responde 403
ao HTML público (proteção anti-bot). Para esses artigos buscamos o corpo pela API pública
de Help Center (`/api/v2/help_center/articles/{id}.json`) — mesmo domínio oficial, sem
bloqueio. O TDN (tdn.totvs.com) responde o HTML direto normalmente.

Cache em memória por hash do problema normalizado: chamados idênticos não repetem a
mesma consulta web (economia e velocidade).

Assinatura do `ddgs` confirmada por introspecção (ddgs 9.x, não-oficial):
  DDGS().text(query, region=..., safesearch=..., max_results=...) -> list[dict]
  cada dict traz as chaves opcionais {"title", "href", "body"} (lidas com .get()).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable

import httpx
import trafilatura
from ddgs import DDGS
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Allowlist da busca (operador site:): oficiais TOTVS + referências técnicas da comunidade
# Protheus consideradas confiáveis pelo time (ADR-038). A busca é restrita a estes domínios.
_DOMINIOS_OFICIAIS = ("centraldeatendimento.totvs.com", "tdn.totvs.com")
_DOMINIOS_COMUNIDADE = (
    "userfunction.com.br",
    "terminaldeinformacao.com",
    "rfbsistemas.com.br",
    "blacktdn.com.br",
    "udesenv.com.br",
)
_DOMINIOS = " OR ".join(f"site:{d}" for d in _DOMINIOS_OFICIAIS + _DOMINIOS_COMUNIDADE)

_MAX_RESULTADOS = 3  # abre no máx. os 3 primeiros resultados
_MAX_CHARS_QUERY = 240  # a query do buscador não precisa do chamado inteiro
_MIN_CHARS_TEXTO = 200  # descarta extrações curtas demais (nav/erro, não conteúdo)
_MAX_CHARS_TRECHO = 4000  # teto por página, para não estourar o contexto do Claude
_TIMEOUT = httpx.Timeout(6.0)  # timeout curto: é melhor esforço
_DELAY_ENTRE_REQS = 0.4  # gentileza entre requisições (segundos)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Artigo de Help Center Zendesk: .../hc/<locale?>/articles/<id>-<slug>. Captura host e id.
_ZENDESK_ARTIGO = re.compile(r"(https?://[^/]+)/hc/(?:[^/]+/)*articles/(\d+)")

# Tipos dos pontos de injeção (para testar sem rede):
#   buscador: (query) -> lista de dicts do ddgs (síncrono)
#   fetcher:  (url)   -> HTML da página (assíncrono)
Buscador = Callable[[str], list[dict]]
Fetcher = Callable[[str], Awaitable[str]]


def _chave_cache(problema: str) -> str:
    """Hash estável do problema normalizado (minúsculo, espaços colapsados)."""
    normalizado = " ".join(problema.lower().split())
    return hashlib.sha256(normalizado.encode()).hexdigest()


def montar_query_web(problema: str) -> str:
    """Query enxuta + restrição aos domínios oficiais — a string REAL enviada ao buscador.

    Pública para o pipeline/interface poderem exibir EXATAMENTE o que foi pesquisado (ADR-027).
    """
    nucleo = " ".join(problema.split())[:_MAX_CHARS_QUERY]
    return f"{nucleo} {_DOMINIOS}"


def _extrair_texto(html: str) -> str | None:
    """Texto principal da página (trafilatura). None se não houver conteúdo útil."""
    try:
        texto = trafilatura.extract(html, favor_precision=True) or ""
    except Exception:  # extrator é best-effort — HTML podre não derruba o lote
        return None
    texto = texto.strip()
    if len(texto) < _MIN_CHARS_TEXTO:
        return None
    return texto[:_MAX_CHARS_TRECHO]


class BuscaWebClient:
    """Busca nos sites oficiais TOTVS e extrai o texto principal. Best-effort, com cache.

    `buscador` e `fetcher` são injetáveis para testar sem rede. Em produção usam o
    `ddgs` (numa thread, pois é síncrono) e um `httpx.AsyncClient`.
    """

    def __init__(
        self,
        *,
        buscador: Buscador | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self._buscador = buscador or _buscar_ddg
        self._fetcher = fetcher or _fetch_http
        self._cache: dict[str, list[str]] = {}

    async def buscar(self, problema: str) -> list[str]:
        """Trechos de texto úteis dos domínios oficiais TOTVS. Lista vazia se nada útil."""
        chave = _chave_cache(problema)
        if chave in self._cache:
            logger.debug("Busca web: cache hit.")
            return self._cache[chave]

        trechos = await self._buscar_sem_cache(problema)
        self._cache[chave] = trechos  # cacheia até o resultado vazio (evita repetir)
        return trechos

    async def _buscar_sem_cache(self, problema: str) -> list[str]:
        try:
            resultados = await asyncio.to_thread(self._buscador, montar_query_web(problema))
        except Exception as exc:  # rate limit, bloqueio, rede — melhor esforço
            logger.warning("Busca web: consulta ao buscador falhou (%s).", exc)
            return []

        urls = _urls_dos_resultados(resultados)
        trechos: list[str] = []
        for i, url in enumerate(urls):
            if i:
                await asyncio.sleep(_DELAY_ENTRE_REQS)  # gentileza com os servidores
            trecho = await self._abrir_e_extrair(url)
            if trecho:
                trechos.append(trecho)
        return trechos

    async def _abrir_e_extrair(self, url: str) -> str | None:
        try:
            html = await self._fetcher(url)
        except Exception as exc:  # timeout, 4xx/5xx, página fora do ar
            logger.warning(
                "Busca web: falha ao abrir %s (%s: %s)", url, type(exc).__name__, exc
            )
            return None
        texto = _extrair_texto(html)
        if not texto:
            return None
        # Preserva a origem (URL) no próprio trecho, para o revisor rastrear a fonte.
        return f"[{url}]\n{texto}"


def _urls_dos_resultados(resultados: list[dict]) -> list[str]:
    """Extrai as URLs (chave opcional 'href') dos N primeiros resultados, sem duplicar."""
    urls: list[str] = []
    for r in resultados or []:
        url = (r.get("href") or "").strip()
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= _MAX_RESULTADOS:
            break
    return urls


def _buscar_ddg(query: str) -> list[dict]:
    """Consulta real ao DuckDuckGo via ddgs (SÍNCRONO — roda em thread no cliente)."""
    with DDGS() as ddgs:
        return ddgs.text(query, region="br-pt", safesearch="off", max_results=_MAX_RESULTADOS)


def _fetch_e_retentavel(exc: BaseException) -> bool:
    """Re-tenta só falhas TRANSITÓRIAS: erro de transporte/timeout ou HTTP 5xx.

    Um 4xx (ex.: 403/404) é determinístico — não adianta re-tentar; cai no best-effort.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


_retry_fetch = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.3, max=2.0),
    retry=retry_if_exception(_fetch_e_retentavel),
)


def _url_alvo(url: str) -> tuple[str, bool]:
    """Resolve o alvo do fetch. Para artigo Zendesk, aponta para a API JSON (evita o 403).

    Retorna (url_alvo, e_zendesk). Se `e_zendesk`, a resposta é JSON e o HTML vem no
    campo article.body; senão, a resposta já é o HTML da página.
    """
    m = _ZENDESK_ARTIGO.match(url)
    if m:
        host, artigo_id = m.group(1), m.group(2)
        return f"{host}/api/v2/help_center/articles/{artigo_id}.json", True
    return url, False


@_retry_fetch
async def _fetch_http(url: str) -> str:
    """Abre a URL e devolve HTML (httpx async, timeout curto, segue redirects).

    Central de Atendimento (Zendesk) bloqueia o HTML público → busca o corpo pela API.
    Retry curto em falhas transitórias (o TDN derruba conexões em rajada); 4xx não re-tenta.
    """
    alvo, e_zendesk = _url_alvo(url)
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(alvo)
        resp.raise_for_status()
        if not e_zendesk:
            return resp.text
        artigo = resp.json().get("article") or {}
        corpo = artigo.get("body") or ""
        titulo = artigo.get("title") or ""
        # Reembrulha como HTML (título + corpo) para o trafilatura extrair.
        return f"<h1>{titulo}</h1>\n{corpo}" if corpo else ""
