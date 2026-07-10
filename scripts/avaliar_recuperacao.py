"""AVALIAÇÃO DE RECUPERAÇÃO — ⚠️ CONSOME EMBEDDINGS (Voyage, pago).

Termômetro objetivo da qualidade da busca (ADR-011): para uma amostra de N pares já
na base, usa o próprio problema como consulta (embedding real) e verifica se o par
retorna entre os top-k. Imprime a taxa de acerto e a distância média do auto-match.

Uso: python -m scripts.avaliar_recuperacao [N=20] [K=5]
Requer a base populada e as chaves reais no .env.
"""

from __future__ import annotations

import asyncio
import sys

import psycopg
from pgvector.psycopg import register_vector_async
from pgvector.utils import Vector

from app.config import get_settings
from app.rag import VoyageClient

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    settings = get_settings()

    conn = await psycopg.AsyncConnection.connect(settings.database_url)
    await register_vector_async(conn)
    try:
        voyage = VoyageClient(settings)

        cur = await conn.execute(
            "SELECT id, problema FROM conhecimento ORDER BY random() LIMIT %(n)s",
            {"n": n},
        )
        amostra = await cur.fetchall()
        if not amostra:
            print("Base vazia — rode a ingestão antes.")
            return

        acertos = 0
        distancias: list[float] = []
        for pid, problema in amostra:
            vetor = await voyage.embed_query(problema)
            cur = await conn.execute(
                "SELECT id, embedding <=> %(v)s AS d FROM conhecimento "
                "ORDER BY embedding <=> %(v)s LIMIT %(k)s",
                {"v": Vector(vetor), "k": k},
            )
            linhas = await cur.fetchall()
            if pid in [linha[0] for linha in linhas]:
                acertos += 1
                distancias.append(next(d for i, d in linhas if i == pid))

        total = len(amostra)
        taxa = acertos / total
        dist_media = sum(distancias) / len(distancias) if distancias else float("nan")
        print(f"Amostra avaliada: {total} pares | k={k}")
        print(f"Taxa de acerto (par no top-{k}): {taxa:.1%} ({acertos}/{total})")
        print(f"Distância média do auto-match (nos acertos): {dist_media:.4f}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
