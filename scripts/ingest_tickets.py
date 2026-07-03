"""Ingestão da base de conhecimento (Módulo 4) — assíncrono e RESUMÍVEL.

Popula `conhecimento` com o PAR problema+solução de chamados resolvidos:

1. Lista chamados via paginação (`?page=&per_page=100`), filtrando status 4
   (Resolvido) e 5 (Fechado) NO CÓDIGO — não usa o endpoint de filtro (teto de 300).
2. Para cada chamado novo: `problema` = `description_text`; `solucao` = última
   resposta PÚBLICA do agente em `/conversations` (heurística — ver ADR-006).
3. Só o problema entra no vetor (`input_type="document"`); a solução é carga.

Resumível: mantém um checkpoint com os `ticket_id` já processados e pula-os em
re-execuções. Respeita rate limit — o FreshdeskClient re-tenta 429 respeitando
`Retry-After`, e há uma pausa entre chamadas. Nunca varre todo o histórico de
uma vez sem controle de taxa.

Uso: `python -m scripts.ingest_tickets`
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import Settings, get_settings
from app.freshdesk import FreshdeskClient
from app.rag import RagRepository, VoyageClient

STATUS_RESOLVIDOS = {4, 5}  # 4=Resolvido, 5=Fechado
CHECKPOINT = Path("ingest_state.txt")
PAUSA_S = 0.2  # respiro entre chamadas para não pressionar o rate limit


@dataclass
class ResumoIngestao:
    ingeridos: int = 0
    sem_solucao: int = 0
    ja_processados: int = 0


def extrair_solucao(conversas: list[dict]) -> str | None:
    """Última resposta PÚBLICA do agente (`private=false` e `incoming=false`).

    `incoming=true` = mensagem do cliente; `private=true` = nota interna. Retorna o
    `body_text` da última resposta pública do agente, ou None se não houver.
    """
    respostas_agente = [
        c
        for c in conversas
        if not c.get("private", False) and not c.get("incoming", True)
    ]
    if not respostas_agente:
        return None
    texto = (respostas_agente[-1].get("body_text") or "").strip()
    return texto or None


def carregar_processados(caminho: Path) -> set[int]:
    """Lê o checkpoint (um ticket_id por linha)."""
    if not caminho.exists():
        return set()
    return {
        int(linha)
        for linha in caminho.read_text(encoding="utf-8").splitlines()
        if linha.strip().isdigit()
    }


def marcar_processado(caminho: Path, ticket_id: int) -> None:
    """Anexa um ticket_id ao checkpoint (persistência incremental)."""
    with caminho.open("a", encoding="utf-8") as fh:
        fh.write(f"{ticket_id}\n")


async def ingerir(
    fd: FreshdeskClient,
    voyage: VoyageClient,
    repo: RagRepository,
    processados: set[int],
    marcar: Callable[[int], None],
    *,
    per_page: int = 100,
    pausa_s: float = 0.0,
) -> ResumoIngestao:
    """Percorre os chamados resolvidos e ingere os pares problema+solução novos."""
    resumo = ResumoIngestao()
    page = 1
    while True:
        lote = await fd.listar_tickets(page=page, per_page=per_page)
        if not lote:
            break
        for item in lote:
            if item.get("status") not in STATUS_RESOLVIDOS:
                continue  # não-resolvido: não marca (pode resolver depois)
            ticket_id = item["id"]
            if ticket_id in processados:
                resumo.ja_processados += 1
                continue

            ticket = await fd.get_ticket(ticket_id)
            conversas = await fd.get_conversations(ticket_id)
            solucao = extrair_solucao(conversas)
            problema = ticket.description_text.strip()

            if solucao and problema:
                [vetor] = await voyage.embed_document([problema])
                await repo.inserir(
                    ticket_id=ticket_id,
                    empresa=ticket.empresa,
                    problema=problema,
                    solucao=solucao,
                    embedding=vetor,
                )
                resumo.ingeridos += 1
            else:
                resumo.sem_solucao += 1

            processados.add(ticket_id)
            marcar(ticket_id)
            if pausa_s:
                await asyncio.sleep(pausa_s)
        page += 1
    return resumo


async def main(settings: Settings | None = None) -> ResumoIngestao:
    settings = settings or get_settings()
    processados = carregar_processados(CHECKPOINT)

    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        repo = RagRepository(conn)
        voyage = VoyageClient(settings)
        async with FreshdeskClient(settings) as fd:
            resumo = await ingerir(
                fd,
                voyage,
                repo,
                processados,
                lambda tid: marcar_processado(CHECKPOINT, tid),
                pausa_s=PAUSA_S,
            )
    finally:
        await conn.close()

    print(
        f"Ingestão concluída: {resumo.ingeridos} ingeridos, "
        f"{resumo.sem_solucao} sem solução, {resumo.ja_processados} já processados."
    )
    return resumo


if __name__ == "__main__":
    asyncio.run(main())
