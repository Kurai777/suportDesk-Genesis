"""SMOKE-TEST MANUAL — ⚠️ CONSOME CHAMADAS PAGAS (Voyage + Claude/Anthropic).

Roda o fluxo REAL para você inspecionar a QUALIDADE da resposta com os próprios olhos:
o Voyage embeda de verdade, o Claude gera de verdade. NÃO entra na suíte automática
(regra: nenhum teste gasta chamada paga). O WhatsApp é FORÇADO para dry-run — nenhuma
mensagem real é enviada; nada é escrito no Freshdesk.

Pré-requisitos:
  - Postgres de pé e POPULADO:  docker compose up -d db && python -m scripts.ingest_tickets
  - Chaves reais no .env:        ANTHROPIC_API_KEY, VOYAGE_API_KEY, DATABASE_URL

Uso:
  python -m scripts.smoke_test "texto do problema do cliente"
  python -m scripts.smoke_test          # usa o caso MV_ATFMOED de exemplo
"""

from __future__ import annotations

import asyncio
import logging
import sys

import psycopg
from pgvector.psycopg import register_vector_async

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.claude_client import ClaudeClient
from app.config import get_settings
from app.models import ResultadoChamado
from app.pipeline import _nota, _whatsapp, decidir
from app.rag import RagRepository, RagService, VoyageClient
from app.texto import limpar_texto
from app.whatsapp import WhatsAppClient

PROBLEMA_PADRAO = (
    "Estamos com problema no lançamento da NF. Log SCC19070: 'O valor do bem na moeda "
    "legal não foi digitado. Moeda: 3. Informe o valor original na moeda legal ou "
    "verifique a moeda definida no parâmetro MV_ATFMOED.'"
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    problema = limpar_texto(sys.argv[1] if len(sys.argv) > 1 else PROBLEMA_PADRAO)
    # Força dry-run: este script nunca envia WhatsApp real.
    settings = get_settings().model_copy(update={"whatsapp_dry_run": True})

    conn = await psycopg.AsyncConnection.connect(settings.database_url)
    await register_vector_async(conn)
    try:
        rag = RagService(VoyageClient(settings), RagRepository(conn))
        claude = ClaudeClient(settings)

        print(f"\n=== PROBLEMA ===\n{problema}\n")

        pares = await rag.buscar(problema)
        print(f"=== CONTEXTO RECUPERADO ({len(pares)} pares) ===")
        for i, par in enumerate(pares, start=1):
            print(f"[{i}] #{par.ticket_id} ({par.empresa}) — distância={par.distancia:.4f}")
            print(f"    problema: {par.problema[:120]}")
            print(f"    solução:  {par.solucao[:120]}")

        resposta = await claude.gerar_resposta(problema, pares)
        resultado = ResultadoChamado(ticket_id=0, empresa="(smoke-test)", resposta=resposta)
        melhor_distancia = min((p.distancia for p in pares), default=None)
        decisao = decidir(
            resultado,
            settings.confianca_minima,
            melhor_distancia=melhor_distancia,
            distancia_maxima=settings.distancia_maxima_confiavel,
        )

        print(f"\n=== DECISÃO: {decisao.value.upper()} ===")
        print("\n=== ResultadoChamado ===")
        print(resultado.model_dump_json(indent=2))

        print("\n=== Nota interna que seria criada no Freshdesk ===")
        print(_nota(decisao, resposta))

        print("\n=== WhatsApp (dry-run — só loga, não envia) ===")
        whatsapp = WhatsAppClient(settings)
        await whatsapp.enviar(
            settings.telefone_responsavel(None),
            _whatsapp(decisao, resultado.ticket_id, resultado.empresa),
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
