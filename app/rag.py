"""RAG: embeddings (Voyage) + base vetorial (pgvector). Assíncrono (ADR-005).

Retrieval assimétrico (ADR-006): SÓ o problema entra no vetor.
- Ingestão:  `input_type="document"` — embeda o problema; solução é carga associada.
- Busca:     `input_type="query"`    — embeda o problema do chamado novo e recupera
             os top-k pares mais similares (retornando a `solucao` como contexto).

Cada registro em `conhecimento`: vetor(1024) do problema + {ticket_id, problema,
solucao, empresa}.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
import voyageai
from pgvector import Vector
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
    """Um par problema/solução recuperado da base, com a distância de similaridade."""

    ticket_id: int | None
    problema: str
    solucao: str
    empresa: str
    distancia: float


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
        empresa: str,
        problema: str,
        solucao: str,
        embedding: list[float],
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO conhecimento (ticket_id, empresa, problema, solucao, embedding)
            VALUES (%(ticket_id)s, %(empresa)s, %(problema)s, %(solucao)s, %(embedding)s)
            """,
            {
                "ticket_id": ticket_id,
                "empresa": empresa,
                "problema": problema,
                "solucao": solucao,
                "embedding": Vector(embedding),
            },
        )

    async def buscar_similares(self, embedding: list[float], k: int) -> list[Similar]:
        """Top-k por distância de cosseno (`<=>`, casa com o índice HNSW)."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT ticket_id, problema, solucao, empresa,
                       embedding <=> %(vec)s AS distancia
                FROM conhecimento
                ORDER BY embedding <=> %(vec)s
                LIMIT %(k)s
                """,
                {"vec": Vector(embedding), "k": k},
            )
            linhas = await cur.fetchall()
        return [Similar(**linha) for linha in linhas]


class RagService:
    """Orquestra embedding da query + busca no pgvector."""

    def __init__(self, voyage: VoyageClient, repo: RagRepository) -> None:
        self._voyage = voyage
        self._repo = repo

    async def buscar(self, problema: str, k: int = 5) -> list[Similar]:
        vetor = await self._voyage.embed_query(problema)
        return await self._repo.buscar_similares(vetor, k)
