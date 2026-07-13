"""Audita um chamado REAL do Freshdesk pelo pipeline, em modo inspeção (NÃO escreve nada).

Puxa o chamado pela API do Freshdesk e roda o MESMO miolo do webhook (`inspecionar`):
leitura de imagens → reformulação de query → RAG (união) → Claude → decisão + guardrail de
distância (ADR-030) → busca web (se ligada). NÃO cria nota, NÃO atribui, NÃO envia WhatsApp.

Serve para spot-check antes e depois de produção: ver o que o sistema DECIDIRIA e RASCUNHARIA
para um chamado real, e auditar a distância do melhor par (o sinal do guardrail).

⚠️ Consome Voyage (embedding) e Claude (pago). Requer .env com as chaves reais e a base populada.

Uso:
    python -m scripts.inspecionar_chamado 4446
    python -m scripts.inspecionar_chamado 4446 --raw   # imprime o rascunho e a nota por extenso
"""

from __future__ import annotations

import asyncio
import sys

import psycopg
from pgvector.psycopg import register_vector_async

from app.busca_web import BuscaWebClient
from app.claude_client import ClaudeClient
from app.config import get_settings
from app.freshdesk import FreshdeskClient
from app.pipeline import Inspecao, _incorporar_imagens, inspecionar
from app.rag import RagRepository, RagService, VoyageClient
from app.visao import VisaoClient

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _uma_linha(texto: str, limite: int = 200) -> str:
    achatado = " ".join((texto or "").split())
    return achatado[:limite] + ("…" if len(achatado) > limite else "")


def _melhor_distancia(insp: Inspecao) -> float | None:
    return min((p.distancia for p in insp.pares), default=None)


async def inspecionar_ticket(ticket_id: int) -> tuple[object, Inspecao]:
    """Puxa o chamado e roda a inspeção. Retorna (ticket, inspeção). Sem efeitos colaterais."""
    settings = get_settings()
    http_freshdesk = FreshdeskClient(settings)
    visao = VisaoClient(settings)
    try:
        ticket = await http_freshdesk.get_ticket(ticket_id)
        # Imagens (best-effort), como no webhook — entram na busca via reformulação (ADR-023/024).
        ticket = await _incorporar_imagens(
            ticket, freshdesk=http_freshdesk, visao=visao, settings=settings
        )
    finally:
        await http_freshdesk.close()

    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        rag = RagService(VoyageClient(settings), RagRepository(conn))
        insp = await inspecionar(
            ticket,
            settings=settings,
            rag_service=rag,
            claude=ClaudeClient(settings),
            busca_web=BuscaWebClient(),
        )
    finally:
        await conn.close()
    return ticket, insp


def _imprimir(ticket, insp: Inspecao, raw: bool) -> None:
    dist = _melhor_distancia(insp)
    dist_txt = "—" if dist is None else f"{dist:.4f}"
    print(f"=== CHAMADO #{ticket.id} — {ticket.empresa} ===")
    print(f"  assunto      : {ticket.subject}")
    print(f"  solicitante  : {ticket.requester.name}")
    print(f"  prioridade   : {ticket.priority} | imagens: {len(ticket.imagens)}")
    print(f"  descrição    : {_uma_linha(ticket.description_text)}")
    print(f"\n  DECISÃO      : {insp.decisao.value.upper()}")
    print(f"  encontrou    : {insp.resposta.encontrou_solucao} | "
          f"confiança Claude: {insp.resposta.confianca} | via_web: {insp.via_web}")
    print(f"  melhor par   : {dist_txt}  (guardrail ADR-030)")
    print(f"  query        : {_uma_linha(insp.query, 160)}")
    print("\n  PARES RECUPERADOS:")
    for p in insp.pares:
        rot = p.titulo or f"chamado #{p.ticket_id}"
        print(f"    {p.fonte:<13} d={p.distancia:.4f}  {rot[:60]}")
    if insp.pares_web:
        for p in insp.pares_web:
            print(f"    web           {(p.solucao or '').splitlines()[0][:60]}")
    if raw:
        print("\n  RASCUNHO AO CLIENTE:")
        print("    " + insp.resposta.resposta_cliente.replace("\n", "\n    "))
        print("\n  NOTA INTERNA (que SERIA criada):")
        print("    " + insp.nota.replace("\n", "\n    "))
        print("\n  WHATSAPP (que SERIA enviado ao responsável/grupo):")
        print("    " + insp.whatsapp.replace("\n", "\n    "))
    else:
        print(f"\n  rascunho     : {_uma_linha(insp.resposta.resposta_cliente, 240)}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raw = "--raw" in sys.argv
    if not args:
        print(__doc__)
        return 2
    ticket, insp = asyncio.run(inspecionar_ticket(int(args[0])))
    _imprimir(ticket, insp, raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
