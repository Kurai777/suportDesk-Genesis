"""Integração do IdempotenciaRepository contra o Postgres do docker-compose.

Marcado como `integration` — pulado se o banco não estiver de pé. Valida o
INSERT ... ON CONFLICT DO NOTHING (rowcount 1 na 1ª vez, 0 na reentrega).
"""

import psycopg
import pytest

from app.pipeline import IdempotenciaRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def conn(settings):
    try:
        c = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True, connect_timeout=2
        )
    except Exception:
        pytest.skip("Postgres do docker-compose não está de pé (docker compose up -d db)")
    await c.execute("DELETE FROM chamado_processado")
    try:
        yield c
    finally:
        await c.execute("DELETE FROM chamado_processado")
        await c.close()


async def test_on_conflict_do_nothing(conn):
    repo = IdempotenciaRepository(conn)

    assert await repo.marcar_em_processamento(101) is True  # 1ª vez: insere
    assert await repo.marcar_em_processamento(101) is False  # reentrega: já existe
    assert await repo.marcar_em_processamento(102) is True  # outro ticket
