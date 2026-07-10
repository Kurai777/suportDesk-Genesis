"""Ingestão da base de conhecimento (Módulo 4) — assíncrono e RESUMÍVEL.

Popula `conhecimento` com o PAR problema+solução de chamados resolvidos:

1. Lista chamados via paginação (`?page=&per_page=100`), filtrando status 4
   (Resolvido) e 5 (Fechado) NO CÓDIGO — não usa o endpoint de filtro (teto de 300).
2. Para cada chamado novo: `problema` = assunto + description_text; `solucao` = última
   resposta PÚBLICA do agente em `/conversations` (heurística — ver ADR-006).
3. LIMPA problema e solução (ruído de e-mail) e aplica o FILTRO DE QUALIDADE por
   CONTEÚDO (ADR-013): descarta soluções curtas, pedidos ("favor validar") ou frases
   genéricas de encerramento SEM descrição — sem exigir código/parâmetro.
4. Só o problema entra no vetor (`input_type="document"`); a solução é carga.

Idempotente pelo BANCO (ADR-016): consulta os `ticket_id` já presentes em `conhecimento`
— a tabela é a única fonte da verdade (à prova de recriação; sem arquivos de estado).
Uso: `python -m scripts.ingest_tickets`
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

import psycopg
from pgvector.psycopg import register_vector_async

from app.config import Settings, get_settings
from app.freshdesk import FreshdeskClient
from app.rag import RagRepository, VoyageClient
from app.texto import limpar_texto

# psycopg async exige SelectorEventLoop no Windows (dev). Em Linux (Railway) é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

STATUS_RESOLVIDOS = {4, 5}  # 4=Resolvido, 5=Fechado
PAUSA_S = 0.2  # respiro entre chamadas para não pressionar o rate limit
_MAX_EXEMPLOS = 10  # quantos exemplos (mantidos/descartados) guardar para calibração

# --- filtro de qualidade da solução por CONTEÚDO (ADR-013) -----------------
#
# Sem exigir "indício técnico" (código/parâmetro/tabela) — soluções técnicas em prosa
# ("atualizamos a taxa da moeda...") devem passar. Descartamos por AUSÊNCIA DE EXPLICAÇÃO.

_MIN_PALAVRAS = 8  # palavras de conteúdo (após limpeza) — ajustável
_MAX_PALAVRAS_GENERICA = 15  # acima disso, presume-se que há descrição real

# Pedidos ao cliente / não-soluções: descartam em qualquer tamanho.
_FRASES_PEDIDO = (
    "favor validar",
    "favor verificar",
    "favor confirmar",
    "favor dar",
    "aguardo retorno",
    "aguardamos retorno",
    "aguardo seu retorno",
)
# Encerramentos genéricos ("fiz X") — descartam quando o texto é curto (sem descrição).
_FRASES_ENCERRAMENTO = (
    "foi feita a correção",
    "feita a correção",
    "foi feito o cadastro",
    "foi feita a alteração",
    "foi realizado o ajuste",
    "foi feito o ajuste",
    "realizado o ajuste",
    "ajuste realizado",
    "conforme solicitado",
    "conforme conversamos",
    "conforme combinado",
    "conforme alinhado",
    "conforme analisado",
    "conforme informado",
    "problema resolvido",
    "chamado resolvido",
    "segue anexo",
    "segue em anexo",
    "chamado fechado por falta de interação",
)


def _motivo_baixo_valor(solucao: str) -> str | None:
    """Motivo do descarte (pedido | poucas palavras | encerramento genérico), ou None."""
    norm = " ".join(solucao.lower().split())
    n_palavras = len(solucao.split())
    if any(frase in norm for frase in _FRASES_PEDIDO):
        return "pedido sem solução"
    if n_palavras < _MIN_PALAVRAS:
        return "poucas palavras"
    if n_palavras < _MAX_PALAVRAS_GENERICA and any(
        frase in norm for frase in _FRASES_ENCERRAMENTO
    ):
        return "encerramento genérico"
    return None


@dataclass
class ResumoIngestao:
    ingeridos: int = 0
    sem_solucao: int = 0
    descartados_filtro: int = 0  # tinha solução, mas de baixo valor (ADR-013)
    ja_processados: int = 0
    exemplos_mantidos: list[str] = field(default_factory=list)
    exemplos_descartados: list[tuple[str, str]] = field(default_factory=list)


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


async def ingerir(
    fd: FreshdeskClient,
    voyage: VoyageClient,
    repo: RagRepository,
    ja_ingeridos: set[int],
    *,
    per_page: int = 100,
    pausa_s: float = 0.0,
) -> ResumoIngestao:
    """Percorre os chamados resolvidos e ingere os pares problema+solução de valor.

    `ja_ingeridos`: ticket_id já presentes na base (idempotência pelo banco, ADR-016).
    Chamados sem solução/descartados NÃO ficam na base — na próxima rodada são
    reavaliados (podem ter ganhado uma resposta pública depois).
    """
    resumo = ResumoIngestao()
    page = 1
    while True:
        lote = await fd.listar_tickets(page=page, per_page=per_page)
        if not lote:
            break
        for item in lote:
            if item.get("status") not in STATUS_RESOLVIDOS:
                continue  # não-resolvido: pode resolver depois
            ticket_id = item["id"]
            if ticket_id in ja_ingeridos:
                resumo.ja_processados += 1
                continue

            ticket = await fd.get_ticket(ticket_id)
            conversas = await fd.get_conversations(ticket_id)
            solucao_bruta = extrair_solucao(conversas)
            problema = limpar_texto(f"{ticket.subject}\n\n{ticket.description_text}")
            solucao = limpar_texto(solucao_bruta) if solucao_bruta else ""

            if not solucao_bruta or not problema:
                resumo.sem_solucao += 1
            elif (motivo := _motivo_baixo_valor(solucao)) is not None:
                resumo.descartados_filtro += 1
                if len(resumo.exemplos_descartados) < _MAX_EXEMPLOS:
                    resumo.exemplos_descartados.append((motivo, solucao[:120]))
            else:
                [vetor] = await voyage.embed_document([problema])
                await repo.inserir(
                    ticket_id=ticket_id,
                    empresa=ticket.empresa,
                    problema=problema,
                    solucao=solucao,
                    embedding=vetor,
                )
                resumo.ingeridos += 1
                if len(resumo.exemplos_mantidos) < _MAX_EXEMPLOS:
                    resumo.exemplos_mantidos.append(solucao[:120])

            if pausa_s:
                await asyncio.sleep(pausa_s)
        page += 1
    return resumo


async def main(settings: Settings | None = None) -> ResumoIngestao:
    settings = settings or get_settings()

    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    await register_vector_async(conn)
    try:
        repo = RagRepository(conn)
        voyage = VoyageClient(settings)
        ja_ingeridos = await repo.ticket_ids_ingeridos()  # fonte da verdade = banco
        async with FreshdeskClient(settings) as fd:
            resumo = await ingerir(fd, voyage, repo, ja_ingeridos, pausa_s=PAUSA_S)
    finally:
        await conn.close()

    print(
        f"Ingestão concluída: {resumo.ingeridos} MANTIDOS, "
        f"{resumo.descartados_filtro} DESCARTADOS (baixo valor), "
        f"{resumo.sem_solucao} sem solução, {resumo.ja_processados} já processados."
    )
    if resumo.exemplos_mantidos:
        print(f"\nAmostra de {len(resumo.exemplos_mantidos)} MANTIDOS:")
        for trecho in resumo.exemplos_mantidos:
            print(f"  ✅ {trecho!r}")
    if resumo.exemplos_descartados:
        print(f"\nAmostra de {len(resumo.exemplos_descartados)} DESCARTADOS:")
        for motivo, trecho in resumo.exemplos_descartados:
            print(f"  ❌ [{motivo}] {trecho!r}")
    return resumo


if __name__ == "__main__":
    asyncio.run(main())
