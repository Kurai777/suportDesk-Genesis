"""RAG: embeddings (Voyage) + base vetorial (pgvector). Assíncrono (ADR-005).

Retrieval assimétrico (ADR-006): SÓ o problema entra no vetor.
- Ingestão:  `input_type="document"` — embeda o problema; solução é carga associada.
- Busca:     `input_type="query"`    — embeda o problema do chamado novo e recupera
             os top-k pares mais similares (retornando a `solucao` como contexto).

Cada registro em `conhecimento`: vetor(1024) do problema + {ticket_id, problema,
solucao, empresa}.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
import voyageai
from pgvector.utils import Vector
from psycopg.rows import dict_row
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings

_TENTATIVAS = 4
_ESPERA_MAX = 8.0

# voyageai espelha o módulo de erros no estilo OpenAI. Casamos por NOME da classe
# para não acoplar a nomes de submódulos entre versões do SDK: re-tentamos apenas
# falhas transitórias (rate limit, indisponibilidade, rede/timeout).
_VOYAGE_RETENTAVEL = {
    "RateLimitError",
    "ServiceUnavailableError",
    "APIConnectionError",
    "APITimeoutError",
    "Timeout",
}


def _voyage_e_retentavel(exc: BaseException) -> bool:
    return type(exc).__name__ in _VOYAGE_RETENTAVEL


_retry_voyage = retry(
    reraise=True,
    stop=stop_after_attempt(_TENTATIVAS),
    wait=wait_exponential(multiplier=0.5, max=_ESPERA_MAX),
    retry=retry_if_exception(_voyage_e_retentavel),
)


@dataclass(slots=True)
class Similar:
    """Um trecho recuperado da base (chamado ou documentação), com a distância."""

    ticket_id: int | None
    problema: str
    solucao: str
    empresa: str | None
    distancia: float
    fonte: str = "ticket"
    titulo: str | None = None


class VoyageClient:
    """Cliente fino de embeddings (Voyage). Recebe `Settings` por injeção."""

    def __init__(
        self, settings: Settings, client: voyageai.AsyncClient | None = None
    ) -> None:
        self._model = settings.voyage_model
        self._client = client or voyageai.AsyncClient(api_key=settings.voyage_api_key)

    @_retry_voyage
    async def embed_document(self, textos: list[str]) -> list[list[float]]:
        """Embeda documentos (ingestão) — `input_type="document"`."""
        resultado = await self._client.embed(
            textos, model=self._model, input_type="document"
        )
        return resultado.embeddings

    @_retry_voyage
    async def embed_query(self, texto: str) -> list[float]:
        """Embeda a consulta (busca) — `input_type="query"`."""
        resultado = await self._client.embed(
            [texto], model=self._model, input_type="query"
        )
        return resultado.embeddings[0]


class RagRepository:
    """Acesso à tabela `conhecimento` no pgvector. Recebe a conexão por injeção.

    A conexão deve ter o adaptador do pgvector registrado
    (`pgvector.psycopg.register_vector_async`) antes do uso.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def inserir(
        self,
        *,
        ticket_id: int | None,
        empresa: str | None,
        problema: str,
        solucao: str,
        embedding: list[float],
        fonte: str = "ticket",
        titulo: str | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO conhecimento
                (ticket_id, empresa, problema, solucao, embedding, fonte, titulo)
            VALUES
                (%(ticket_id)s, %(empresa)s, %(problema)s, %(solucao)s,
                 %(embedding)s, %(fonte)s, %(titulo)s)
            """,
            {
                "ticket_id": ticket_id,
                "empresa": empresa,
                "problema": problema,
                "solucao": solucao,
                "embedding": Vector(embedding),
                "fonte": fonte,
                "titulo": titulo,
            },
        )

    async def ticket_ids_ingeridos(self) -> set[int]:
        """Todos os ticket_id já presentes como chamado (idempotência, ADR-016).

        A tabela é a ÚNICA fonte da verdade — à prova de recriação do banco (sem
        depender de checkpoints em arquivo).
        """
        cur = await self._conn.execute(
            "SELECT ticket_id FROM conhecimento "
            "WHERE fonte = 'ticket' AND ticket_id IS NOT NULL"
        )
        return {linha[0] for linha in await cur.fetchall()}

    async def doc_ja_ingerido(self, titulo: str | None, problema: str) -> bool:
        """Se um trecho de documentação com este (titulo, problema) já existe (ADR-016).

        (titulo, problema) é a chave estável do trecho — ambos ficam gravados na linha,
        então a verificação sobrevive à recriação do banco.
        """
        cur = await self._conn.execute(
            "SELECT 1 FROM conhecimento WHERE fonte = 'documentacao' "
            "AND titulo IS NOT DISTINCT FROM %(t)s AND problema = %(p)s LIMIT 1",
            {"t": titulo, "p": problema},
        )
        return await cur.fetchone() is not None

    async def buscar_similares(self, embedding: list[float], k: int) -> list[Similar]:
        """Top-k por distância de cosseno (`<=>`, casa com o índice HNSW)."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT ticket_id, problema, solucao, empresa, fonte, titulo,
                       embedding <=> %(vec)s AS distancia
                FROM conhecimento
                ORDER BY embedding <=> %(vec)s
                LIMIT %(k)s
                """,
                {"vec": Vector(embedding), "k": k},
            )
            linhas = await cur.fetchall()
        return [Similar(**linha) for linha in linhas]


def _identidade(par: Similar) -> tuple:
    """Chave para deduplicar o MESMO trecho recuperado por queries diferentes (ADR-024)."""
    if par.fonte == "ticket":
        return ("ticket", par.ticket_id)
    return (par.fonte, par.titulo, par.problema)


def _unir(listas: list[list[Similar]], k: int) -> list[Similar]:
    """Une recuperações de várias queries: por trecho, mantém a MENOR distância; top-k.

    A distância é sempre à query que recuperou o trecho, então o mesmo alvo aparece com
    distâncias diferentes em cada lista — ficamos com a melhor (o alvo está perto de ao
    menos uma das intenções de busca). Ordena por distância e corta em k.
    """
    melhor: dict[tuple, Similar] = {}
    for par in (p for lista in listas for p in lista):
        chave = _identidade(par)
        atual = melhor.get(chave)
        if atual is None or par.distancia < atual.distancia:
            melhor[chave] = par
    return sorted(melhor.values(), key=lambda p: p.distancia)[:k]


class RagService:
    """Orquestra embedding da(s) query(s) + busca no pgvector."""

    def __init__(self, voyage: VoyageClient, repo: RagRepository) -> None:
        self._voyage = voyage
        self._repo = repo

    async def buscar(self, problema: str, k: int = 5) -> list[Similar]:
        vetor = await self._voyage.embed_query(problema)
        return await self._repo.buscar_similares(vetor, k)

    async def buscar_uniao(self, queries: Sequence[str], k: int = 5) -> list[Similar]:
        """Busca com VÁRIAS queries e une (ADR-024): a documentação responde melhor à
        intenção reformulada; o chamado anterior, ao texto cru. Buscar com as duas e ficar
        com a menor distância por trecho pega o melhor dos dois sem escolher.

        Queries repetidas ou vazias são ignoradas; sem nenhuma útil, retorna [].
        """
        unicas = list(dict.fromkeys(q for q in queries if q.strip()))
        if not unicas:
            return []
        if len(unicas) == 1:
            return await self.buscar(unicas[0], k)
        listas = [await self.buscar(q, k) for q in unicas]
        return _unir(listas, k)
