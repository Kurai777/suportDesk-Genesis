"""AVALIAÇÃO DA REFORMULAÇÃO DE QUERY (ADR-024) — ⚠️ CONSOME EMBEDDINGS E CLAUDE (pago).

Prova antes/depois, leave-one-out, do efeito de reformular o chamado em INTENÇÃO de busca
antes do embedding.

Baseline HONESTO: o texto do chamado como o pipeline busca HOJE — já passado pelo
`limpar_texto` (ADR-011/013). Ou seja, isto NÃO remede o ganho da limpeza regex, que já
existe; mede só o ganho INCREMENTAL da reformulação por cima dela.

Regra do experimento (mesma da `avaliar_realista`, para não viciar a métrica):
- As CONSULTAS vêm só de chamados (`fonte='ticket'`); documento nunca é consulta.
- Os ALVOS são todos os itens exceto o próprio (`id <> ...`).

Indicadores, antes × depois:
- `d_top1`: distância do vizinho mais próximo (qualquer fonte).
- `d_doc`: distância do melhor trecho de DOCUMENTAÇÃO — é o que decide se a base sabe
  ENSINAR a resposta.
- `ensinável`: `d_doc < 0,40` (o limiar da ADR-021). É a métrica que move o produto:
  quantos chamados a base passa a saber responder.

Uso: python -m scripts.avaliar_reformulacao [N=25]
Requer a base populada e as chaves reais no .env.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector_async
from pgvector.utils import Vector

from app.claude_client import ClaudeClient
from app.config import get_settings
from app.rag import VoyageClient
from app.texto import limpar_texto

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

LIMIAR_ENSINAVEL = 0.40


def _trecho(texto: str, limite: int = 88) -> str:
    achatado = " ".join(texto.split())
    return achatado[:limite] + ("…" if len(achatado) > limite else "")


@dataclass
class Recuperacao:
    """O que uma query recuperou, separado por fonte (é a separação que decide a ADR)."""

    d_top1: float
    fonte_top1: str
    d_doc: float | None  # melhor trecho de DOCUMENTAÇÃO (a base sabe ENSINAR?)
    d_ticket: float | None  # melhor CHAMADO anterior (traz solução real de um agente)


async def _recuperar(conn, vetor: list[float], pid: int) -> Recuperacao:
    cur = await conn.execute(
        "SELECT fonte, embedding <=> %(v)s AS d FROM conhecimento "
        "WHERE id <> %(self)s ORDER BY embedding <=> %(v)s LIMIT 50",
        {"v": Vector(vetor), "self": pid},
    )
    linhas = await cur.fetchall()
    if not linhas:
        return Recuperacao(1.0, "—", None, None)
    return Recuperacao(
        d_top1=linhas[0][1],
        fonte_top1=linhas[0][0],
        d_doc=next((d for fonte, d in linhas if fonte == "documentacao"), None),
        d_ticket=next((d for fonte, d in linhas if fonte == "ticket"), None),
    )


def _melhor(a: float | None, b: float | None) -> float | None:
    """Menor distância entre dois arms — a UNIÃO recupera com as duas queries."""
    candidatos = [d for d in (a, b) if d is not None]
    return min(candidatos) if candidatos else None


def _ensinavel(d_doc: float | None) -> bool:
    return d_doc is not None and d_doc < LIMIAR_ENSINAVEL


def _media(valores: list[float]) -> float:
    return sum(valores) / len(valores) if valores else float("nan")


def _fmt(distancia: float | None) -> str:
    return "—" if distancia is None else f"{distancia:.4f}"


def _resumo(rotulo: str, docs: list[float], tickets: list[float], ensinaveis: int, n: int) -> None:
    print(
        f"  {rotulo:7} d_doc média={_media(docs):.4f}  d_ticket média={_media(tickets):.4f}  "
        f"ensináveis(d_doc<{LIMIAR_ENSINAVEL:.2f})={ensinaveis}/{n}"
    )


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    settings = get_settings()

    conn = await psycopg.AsyncConnection.connect(settings.database_url)
    await register_vector_async(conn)
    try:
        voyage = VoyageClient(settings)
        claude = ClaudeClient(settings)

        cur = await conn.execute(
            "SELECT id, ticket_id, problema FROM conhecimento "
            "WHERE fonte = 'ticket' ORDER BY id LIMIT %(n)s",
            {"n": n},
        )
        amostra = await cur.fetchall()
        if not amostra:
            print("Nenhum chamado (fonte='ticket') na base — rode a ingestão antes.")
            return

        # Três braços: ANTES (texto limpo), DEPOIS (reformulada), UNIÃO (busca com as DUAS
        # e fica com a menor distância por fonte — 2 embeddings, 1 chamada Haiku).
        docs: dict[str, list[float]] = {"antes": [], "depois": [], "união": []}
        tickets: dict[str, list[float]] = {"antes": [], "depois": [], "união": []}
        ensinaveis = {"antes": 0, "depois": 0, "união": 0}
        top1_doc = {"antes": 0, "depois": 0}
        flips_ganhos: list[int] = []
        flips_perdidos: list[int] = []

        for pid, ticket_id, problema in amostra:
            base = limpar_texto(problema)  # o que o pipeline busca HOJE
            query = await claude.reformular_query(base)

            a = await _recuperar(conn, await voyage.embed_query(base), pid)
            d = await _recuperar(conn, await voyage.embed_query(query), pid)
            u_doc, u_ticket = _melhor(a.d_doc, d.d_doc), _melhor(a.d_ticket, d.d_ticket)

            for rotulo, ddoc, dtk in (
                ("antes", a.d_doc, a.d_ticket),
                ("depois", d.d_doc, d.d_ticket),
                ("união", u_doc, u_ticket),
            ):
                if ddoc is not None:
                    docs[rotulo].append(ddoc)
                if dtk is not None:
                    tickets[rotulo].append(dtk)
                ensinaveis[rotulo] += _ensinavel(ddoc)
            top1_doc["antes"] += a.fonte_top1 == "documentacao"
            top1_doc["depois"] += d.fonte_top1 == "documentacao"

            if _ensinavel(d.d_doc) and not _ensinavel(a.d_doc):
                flips_ganhos.append(ticket_id)
                marca = "  ⬆ FLIP"
            elif _ensinavel(a.d_doc) and not _ensinavel(d.d_doc):
                flips_perdidos.append(ticket_id)
                marca = "  ⬇ PERDA"
            else:
                marca = ""

            print(f"\n#{ticket_id}{marca}")
            print(f"  antes : {_trecho(base)}")
            print(f"          top1={a.fonte_top1}({a.d_top1:.4f})  "
                  f"d_doc={_fmt(a.d_doc)}  d_ticket={_fmt(a.d_ticket)}")
            print(f"  depois: {_trecho(query)}")
            print(f"          top1={d.fonte_top1}({d.d_top1:.4f})  "
                  f"d_doc={_fmt(d.d_doc)}  d_ticket={_fmt(d.d_ticket)}")

        total = len(amostra)
        print(f"\n--- Resumo ({total} chamados, leave-one-out) ---")
        for rotulo in ("antes", "depois", "união"):
            _resumo(rotulo.upper(), docs[rotulo], tickets[rotulo], ensinaveis[rotulo], total)
        print(f"\n  Δ d_doc  (depois−antes): {_media(docs['depois']) - _media(docs['antes']):+.4f}"
              "   (negativo = documentação mais perto)")
        print(f"  Δ d_ticket (depois−antes): "
              f"{_media(tickets['depois']) - _media(tickets['antes']):+.4f}"
              "   (positivo = chamados ficaram MAIS LONGE)")
        print(f"\n  Top-1 é documentação: antes {top1_doc['antes']}/{total}, "
              f"depois {top1_doc['depois']}/{total}")
        print(f"  Flips ganhos  : {len(flips_ganhos)} {flips_ganhos}")
        print(f"  Flips perdidos: {len(flips_perdidos)} {flips_perdidos}")
        print(f"  Ensináveis: antes {ensinaveis['antes']} → depois {ensinaveis['depois']} "
              f"→ união {ensinaveis['união']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
