"""AVALIAÇÃO REALISTA (leave-one-out) — ⚠️ CONSOME EMBEDDINGS (Voyage, pago).

Mede a recuperação simulando o cenário REAL de produção: um chamado novo entra e a base
(chamados antigos + documentação oficial) é consultada.

Regra do experimento (para não viciar a métrica):
- As CONSULTAS vêm EXCLUSIVAMENTE de chamados (`fonte='ticket'`). Documento nunca é
  consulta — um artigo grande casar com trechos de si mesmo derrubava a distância à toa.
- Os ALVOS são todos os itens (chamados + docs), exceto o próprio (`id != ...`).

Para cada chamado-consulta, imprime os top-k recuperados com fonte e título/ID (para ver
se os artigos oficiais estão sendo puxados para os problemas que antes falhavam: EDI, IPI,
férias, estorno) e, no fim, dois indicadores:
- em quantas consultas o top-1 é um documento oficial;
- a distribuição de distâncias top-1 SEPARADA por fonte do vizinho (ticket × documentação).

Uso: python -m scripts.avaliar_realista [N=10] [K=3]
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


def _trecho(texto: str, limite: int = 80) -> str:
    """Uma linha, cortada, para caber na tela."""
    achatado = " ".join(texto.split())
    return achatado[:limite] + ("…" if len(achatado) > limite else "")


def _origem(fonte: str, ticket_id: int | None, titulo: str | None) -> str:
    if fonte == "documentacao":
        return f"DOC «{titulo or 'sem título'}»"
    return f"chamado #{ticket_id}"


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    settings = get_settings()

    conn = await psycopg.AsyncConnection.connect(settings.database_url)
    await register_vector_async(conn)
    try:
        voyage = VoyageClient(settings)

        # CONSULTAS: só chamados. Documento é alvo de recuperação, nunca consulta.
        cur = await conn.execute(
            "SELECT id, ticket_id, fonte, titulo, problema "
            "FROM conhecimento WHERE fonte = 'ticket' ORDER BY random() LIMIT %(n)s",
            {"n": n},
        )
        amostra = await cur.fetchall()
        if not amostra:
            print("Nenhum chamado (fonte='ticket') na base — rode a ingestão de chamados antes.")
            return

        top1_docs = 0
        dist_por_fonte: dict[str, list[float]] = {"ticket": [], "documentacao": []}
        for pid, ticket_id, fonte, titulo, problema in amostra:
            vetor = await voyage.embed_query(problema)
            cur = await conn.execute(
                "SELECT ticket_id, fonte, titulo, problema, embedding <=> %(v)s AS d "
                "FROM conhecimento WHERE id <> %(self)s "
                "ORDER BY embedding <=> %(v)s LIMIT %(k)s",
                {"v": Vector(vetor), "self": pid, "k": k},
            )
            vizinhos = await cur.fetchall()

            print(f"\n=== Consulta: {_origem(fonte, ticket_id, titulo)} ===")
            print(f"  problema: {_trecho(problema)}")
            print(f"  top-{k} (excluindo ele mesmo):")
            for pos, (v_ticket, v_fonte, v_titulo, v_problema, dist) in enumerate(
                vizinhos, start=1
            ):
                origem = _origem(v_fonte, v_ticket, v_titulo)
                print(f"    {pos}. [{origem}] (d={dist:.4f}) {_trecho(v_problema)}")
            if vizinhos:
                v_fonte_top1, dist_top1 = vizinhos[0][1], vizinhos[0][4]
                dist_por_fonte.setdefault(v_fonte_top1, []).append(dist_top1)
                if v_fonte_top1 == "documentacao":
                    top1_docs += 1

        total = len(amostra)
        print(f"\n--- Resumo ({total} consultas, todas de chamados) ---")
        print(f"  Top-1 é documentação oficial: {top1_docs}/{total} consultas")
        print("  Distância top-1 por fonte do vizinho mais próximo:")
        for fonte_alvo in ("ticket", "documentacao"):
            ds = dist_por_fonte.get(fonte_alvo, [])
            if ds:
                print(
                    f"    {fonte_alvo:13} n={len(ds):<3} "
                    f"min={min(ds):.4f} média={sum(ds) / len(ds):.4f} máx={max(ds):.4f}"
                )
            else:
                print(f"    {fonte_alvo:13} n=0   (nunca foi o vizinho mais próximo)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
