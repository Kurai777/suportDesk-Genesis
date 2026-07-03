"""Teste de INTEGRAÇÃO do RagRepository contra o Postgres do docker-compose.

Marcado como `integration` — pulado automaticamente se o banco não estiver de pé
(Postgres local é grátis; não viola a regra de "nenhuma chamada real paga").

Suba o banco antes:  docker compose up -d db
Rode só a integração:  pytest -m integration
"""

import psycopg
import pytest
from pgvector import Vector
from pgvector.psycopg import register_vector_async

from app.rag import RagRepository

pytestmark = pytest.mark.integration

DIM = 1024


def _vetor(indice: int, valor: float = 1.0) -> list[float]:
    """Vetor unitário 1024-d com `valor` na posição `indice` (direções ortogonais)."""
    v = [0.0] * DIM
    v[indice] = valor
    return v


@pytest.fixture
async def repo(settings):
    try:
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True, connect_timeout=2
        )
    except Exception:
        pytest.skip("Postgres do docker-compose não está de pé (docker compose up -d db)")
    await register_vector_async(conn)
    await conn.execute("DELETE FROM conhecimento")
    try:
        yield RagRepository(conn)
    finally:
        await conn.execute("DELETE FROM conhecimento")
        await conn.close()


_SEMENTES = [
    (1, "A", "problema A", "solução A", 0),
    (2, "B", "problema B", "solução B", 1),
    (3, "C", "problema C", "solução C", 2),
]


async def _semear(repo: RagRepository) -> None:
    for ticket_id, empresa, problema, solucao, idx in _SEMENTES:
        await repo.inserir(
            ticket_id=ticket_id,
            empresa=empresa,
            problema=problema,
            solucao=solucao,
            embedding=_vetor(idx),
        )


async def test_busca_cosseno_retorna_vizinho_correto(repo):
    await _semear(repo)

    resultados = await repo.buscar_similares(_vetor(0), k=3)

    assert len(resultados) == 3
    # O mais próximo do vetor de consulta (mesma direção) é o registro 1.
    assert resultados[0].ticket_id == 1
    assert resultados[0].solucao == "solução A"
    # Distâncias em ordem crescente.
    assert resultados[0].distancia <= resultados[1].distancia <= resultados[2].distancia


async def test_busca_usa_indice_hnsw(repo):
    await _semear(repo)

    # Com seqscan desligado, o planejador precisa usar o índice HNSW para o ORDER BY <=>.
    async with repo._conn.transaction():
        await repo._conn.execute("SET LOCAL enable_seqscan = off")
        async with repo._conn.cursor() as cur:
            await cur.execute(
                "EXPLAIN SELECT ticket_id FROM conhecimento "
                "ORDER BY embedding <=> %(vec)s LIMIT 3",
                {"vec": Vector(_vetor(0))},
            )
            plano = "\n".join(linha[0] for linha in await cur.fetchall())

    assert "conhecimento_embedding_idx" in plano
