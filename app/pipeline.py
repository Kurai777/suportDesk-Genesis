"""Pipeline (maestro) — orquestra o processamento de um chamado. Assíncrono.

Ordem (ADR-009): idempotência → Freshdesk (nota interna + atribuição) → WhatsApp.
A decisão "resolvido × escalar" é uma FUNÇÃO PURA, sem I/O (Padrões de Engenharia).
Se o miolo (ler chamado / buscar / gerar / decidir) falhar, um fallback seguro garante
que o chamado NUNCA fique marcado como processado sem nenhuma ação para um humano.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

import psycopg

from app.busca_web import BuscaWebClient, montar_query_web
from app.claude_client import ClaudeClient
from app.config import Settings
from app.freshdesk import FreshdeskClient
from app.models import EMPRESA_DESCONHECIDA, RespostaIA, ResultadoChamado, TicketFreshdesk
from app.rag import RagService, Similar
from app.texto import limpar_texto
from app.visao import VisaoClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

_NIVEL_CONFIANCA = {"baixa": 0, "media": 1, "alta": 2}

NOTA_FALLBACK = "⚠️ IA indisponível no momento — chamado encaminhado para análise manual."

# Leitura de imagens (ADR-023): cabeçalho do bloco transcrito e teto de imagens por chamado.
_CABECALHO_IMAGENS = "[Texto extraído de imagens anexadas ao chamado]"
_MAX_IMAGENS = 4  # limita custo/latência por chamado (best-effort)


class Decisao(StrEnum):
    RESOLVIDO = "resolvido"
    ESCALAR = "escalar"


# --- decisão (função PURA, sem I/O) ----------------------------------------


def decidir(
    resultado: ResultadoChamado,
    confianca_minima: str,
    *,
    melhor_distancia: float | None,
    distancia_maxima: float,
) -> Decisao:
    """Decide RESOLVER × ESCALAR cruzando o auto-relato do Claude com um sinal OBJETIVO.

    RESOLVE só quando TODAS valem: o Claude achou solução, a confiança dele >= mínima E a
    recuperação é objetivamente próxima — o melhor par local a uma distância <= `distancia_maxima`
    (guardrail da ADR-030). NUNCA confia só na autoavaliação do modelo: um match distante (ex.: o
    #4446, doc de NFSE a ~0,46 para uma NF de entrada) escala mesmo com o Claude dizendo "alta".
    Sem par recuperado (`melhor_distancia is None`), escala — não há como confirmar objetivamente.
    """
    r = resultado.resposta
    if (
        r.encontrou_solucao
        and _atende_minimo(r.confianca, confianca_minima)
        and melhor_distancia is not None
        and melhor_distancia <= distancia_maxima
    ):
        return Decisao.RESOLVIDO
    return Decisao.ESCALAR


def _atende_minimo(confianca: str, minimo: str) -> bool:
    # Ordem: alta > media > baixa. Mínimo desconhecido => nada atende (escala, seguro).
    return _NIVEL_CONFIANCA.get(confianca, -1) >= _NIVEL_CONFIANCA.get(
        minimo, len(_NIVEL_CONFIANCA)
    )


# --- textos das notificações (puros) ---------------------------------------


def _montar_problema(ticket: TicketFreshdesk) -> str:
    # Mesma limpeza da ingestão, para a query casar com os problemas indexados.
    return limpar_texto(f"{ticket.subject}\n\n{ticket.description_text}")


def _nota(decisao: Decisao, r: RespostaIA, *, via_web: bool = False) -> str:
    if decisao is Decisao.RESOLVIDO:
        origem = (
            "🌐 Rascunho gerado a partir de BUSCA WEB em site oficial TOTVS "
            "(fonte MENOS verificada — confira a fonte e revise com atenção redobrada)."
            if via_web
            else "🤖 Rascunho gerado por IA (revisar antes de enviar)."
        )
        return f"{origem} Confiança: {r.confianca}.\n\n{r.resposta_cliente}"
    # ESCALAR: para o TIME. Linha de status TÉCNICA (verdade) + resumo; e o rascunho de
    # acolhimento ao cliente, para o agente revisar e enviar (só a resposta_cliente é
    # "cliente-friendly"; o status acima segue cru). Para pedido operacional, o resumo do
    # Claude já sinaliza a execução pendente.
    extra = " (a busca web em sites oficiais TOTVS também não trouxe solução)" if via_web else ""
    return (
        f"⚠️ IA não encontrou solução na base{extra}. Requer análise manual.\n\n"
        f"Resumo: {r.resumo_para_responsavel}\n\n"
        f"— Rascunho de acolhimento ao cliente (revisar antes de enviar) —\n"
        f"{r.resposta_cliente}"
    )


def _whatsapp(decisao: Decisao, ticket_id: int, empresa: str, *, via_web: bool = False) -> str:
    if decisao is Decisao.RESOLVIDO:
        aviso = " (via BUSCA WEB — revise a fonte com atenção)" if via_web else ""
        return (
            f"✅ Chamado #{ticket_id} da {empresa} — "
            f"rascunho pronto no Freshdesk para revisão{aviso}."
        )
    return (
        f"🔴 Chamado #{ticket_id} da {empresa} — não encontrei solução na base do "
        f"TOTVS. Recomendo olhar pessoalmente."
    )


def _pares_web(trechos: list[str]) -> list[Similar]:
    """Converte os trechos extraídos da web em pares Similar rotulados 'web_totvs'."""
    return [
        Similar(
            ticket_id=None,
            problema="Trecho recuperado por busca web em site oficial TOTVS",
            solucao=trecho,
            empresa=None,
            distancia=1.0,  # sem vetor: distância máxima (fonte "distante"/menos confiável)
            fonte="web_totvs",
            titulo="Site oficial TOTVS (busca web)",
        )
        for trecho in trechos
    ]


# --- idempotência (I/O fino) -----------------------------------------------


class IdempotenciaRepository:
    """Marca o chamado como processado (INSERT ... ON CONFLICT DO NOTHING)."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def marcar_em_processamento(self, ticket_id: int) -> bool:
        """Retorna True se INSERIU (primeira vez); False se já existia (reentrega)."""
        cur = await self._conn.execute(
            """
            INSERT INTO chamado_processado (ticket_id)
            VALUES (%(ticket_id)s)
            ON CONFLICT (ticket_id) DO NOTHING
            """,
            {"ticket_id": ticket_id},
        )
        return cur.rowcount == 1


# --- miolo SEM efeitos colaterais (compartilhado pelo webhook e pela interface) --------


@dataclass
class Inspecao:
    """O que o pipeline PRODUZ para um chamado, ANTES de qualquer I/O de saída.

    É o que a interface de teste mostra na tela e o que o `processar()` usa para agir.
    Não contém nem toca Freshdesk/WhatsApp.
    """

    problema: str  # texto limpo do chamado (é ele que vai ao Claude gerar a resposta)
    query: str  # o que REALMENTE foi buscado no pgvector (reformulado, ou == problema)
    pares: list[Similar]  # recuperação local (fonte/título/distância p/ auditoria)
    resposta: RespostaIA  # resposta FINAL (a da web, se acionada)
    decisao: Decisao
    via_web: bool
    pares_web: list[Similar] = field(default_factory=list)  # trechos web (se acionada)
    query_web: str = ""  # a query REAL enviada aos domínios TOTVS ("" = web não acionada)
    nota: str = ""  # nota interna que SERIA criada no Freshdesk
    whatsapp: str = ""  # mensagem que SERIA enviada no WhatsApp


async def _query_de_busca(
    problema: str, *, claude: ClaudeClient, settings: Settings
) -> str:
    """Query do RAG: a intenção reformulada (ADR-024) ou o texto limpo. BEST-EFFORT.

    Falha na reformulação NUNCA derruba o chamado — cai no `problema`, que é exatamente o
    comportamento anterior à ADR-024. Reformular só afeta O QUE É BUSCADO; o `problema`
    íntegro é que segue para o `gerar_resposta`.
    """
    if not settings.reformular_query_ativa:
        return problema
    try:
        return await claude.reformular_query(problema)
    except Exception:
        logger.exception("Reformulação de query falhou — usando o texto limpo do chamado.")
        return problema


async def inspecionar(
    ticket: TicketFreshdesk,
    *,
    settings: Settings,
    rag_service: RagService,
    claude: ClaudeClient,
    busca_web: BuscaWebClient | None = None,
) -> Inspecao:
    """MIOLO do pipeline: reformular query → RAG → Claude → decisão → (busca web).

    SEM efeitos colaterais. NÃO recebe Freshdesk nem WhatsApp — por construção, não há como
    escrever nota nem enviar mensagem por este caminho. É a MESMA lógica que o `processar()`
    usa (a interface de teste chama exatamente isto), então o que se vê na tela é o que
    aconteceria.
    """
    problema = _montar_problema(ticket)
    query = await _query_de_busca(problema, claude=claude, settings=settings)
    # UNIÃO (ADR-024): busca com o texto limpo E a intenção reformulada, unindo por menor
    # distância. A documentação responde melhor à intenção; o chamado anterior, ao texto
    # cru. Se não houve reformulação (flag off/falha/degenerada), query==problema e a união
    # colapsa numa busca só. O Claude gera a resposta a partir do PROBLEMA íntegro, não da
    # query — a query é uma compressão com perda, boa para buscar e ruim para responder.
    pares = await rag_service.buscar_uniao([problema, query])
    resposta = await claude.gerar_resposta(problema, pares)
    resultado = ResultadoChamado(
        ticket_id=ticket.id, empresa=ticket.empresa, resposta=resposta
    )
    # Guardrail de distância (ADR-030): o melhor (menor) par recuperado é o sinal objetivo.
    melhor_distancia = min((p.distancia for p in pares), default=None)
    decisao = decidir(
        resultado,
        settings.confianca_minima,
        melhor_distancia=melhor_distancia,
        distancia_maxima=settings.distancia_maxima_confiavel,
    )

    # ÚLTIMO RECURSO: só se escalou por FALTA DE CONTEXTO e a flag está ligada.
    # Pedido operacional NÃO vai à web — é execução humana, não uma dúvida pesquisável.
    via_web = False
    pares_web: list[Similar] = []
    query_web = ""
    if (
        settings.busca_web_ativa
        and busca_web is not None
        and decisao is Decisao.ESCALAR
        and not resposta.encontrou_solucao
        and not resposta.pedido_operacional
    ):
        # Registra a query REAL enviada aos domínios TOTVS (mesmo que a web volte vazia),
        # para a interface mostrar exatamente o que foi pesquisado (ADR-027).
        query_web = montar_query_web(problema)
        resposta, decisao, via_web, pares_web = await _tentar_busca_web(
            problema, ticket.id, resposta, decisao, busca_web=busca_web, claude=claude
        )

    return Inspecao(
        problema=problema,
        query=query,
        pares=pares,
        resposta=resposta,
        decisao=decisao,
        via_web=via_web,
        pares_web=pares_web,
        query_web=query_web,
        nota=_nota(decisao, resposta, via_web=via_web),
        whatsapp=_whatsapp(decisao, ticket.id, ticket.empresa, via_web=via_web),
    )


async def _tentar_busca_web(
    problema: str,
    ticket_id: int,
    resposta: RespostaIA,
    decisao: Decisao,
    *,
    busca_web: BuscaWebClient,
    claude: ClaudeClient,
) -> tuple[RespostaIA, Decisao, bool, list[Similar]]:
    """Reconsulta o Claude com trechos da web. Best-effort: em falha mantém a escala.

    Retorna (resposta, decisao, via_web, pares_web). via_web=True só quando a web trouxe
    conteúdo e o Claude foi reconsultado (o desfecho passa a ser o dessa 2ª chamada).

    O caminho web NÃO aplica o `confianca_minima` estrito da base local: o rascunho já
    sai marcado como fonte "menos verificada" (revisão humana obrigatória na Fase 1),
    então basta o Claude ter encontrado solução nos trechos web para virar rascunho.
    """
    try:
        trechos = await busca_web.buscar(problema)  # nunca levanta
        if not trechos:
            return resposta, decisao, False, []  # web vazia → mantém a escala local
        pares_web = _pares_web(trechos)
        resposta_web = await claude.gerar_resposta(problema, pares_web)
        decisao_web = (
            Decisao.RESOLVIDO if resposta_web.encontrou_solucao else Decisao.ESCALAR
        )
        return resposta_web, decisao_web, True, pares_web
    except Exception:
        logger.exception(
            "Busca web (último recurso) falhou (ticket %s) — mantém escala.", ticket_id
        )
        return resposta, decisao, False, []


# --- leitura de imagens (I/O; best-effort) ---------------------------------


async def _incorporar_imagens(
    ticket: TicketFreshdesk,
    *,
    freshdesk: FreshdeskClient,
    visao: VisaoClient | None,
    settings: Settings,
) -> TicketFreshdesk:
    """Transcreve o texto legível das imagens do chamado e concatena à descrição (ADR-023).

    BEST-EFFORT: falha ao baixar/transcrever uma imagem é ignorada (não derruba o chamado).
    Se a visão está desligada/ausente ou não há imagens, retorna o ticket INALTERADO. A
    transcrição entra ANTES da busca — vira parte da query do RAG e do contexto do Claude.
    """
    if visao is None or not settings.leitura_imagens_ativa:
        return ticket
    imagens = ticket.imagens[:_MAX_IMAGENS]
    if not imagens:
        return ticket

    trechos: list[str] = []
    for anexo in imagens:
        try:
            dados = await freshdesk.baixar_anexo(anexo.attachment_url)
            texto = await visao.transcrever(dados, anexo.content_type)
        except Exception:
            logger.exception(
                "Falha ao ler imagem %s (ticket %s) — ignorada.", anexo.id, ticket.id
            )
            continue
        if texto.strip():
            trechos.append(texto.strip())

    if not trechos:
        return ticket
    nova_descricao = concatenar_transcricoes(ticket.description_text, trechos)
    return ticket.model_copy(update={"description_text": nova_descricao})


def concatenar_transcricoes(texto: str, trechos: list[str]) -> str:
    """Concatena as transcrições de imagens ao texto, sob um cabeçalho (ADR-023/025).

    Função PURA, reusada pelo webhook (`_incorporar_imagens`, a partir de anexos do Freshdesk)
    e pela interface de teste (a partir de imagens enviadas na hora). Sem trechos, devolve o
    texto inalterado. A transcrição entra ANTES da busca — vira parte da query do RAG.
    """
    if not trechos:
        return texto
    bloco = "\n\n".join(trechos)
    return f"{texto}\n\n{_CABECALHO_IMAGENS}\n{bloco}".strip()


# --- orquestração (webhook: miolo + I/O) -----------------------------------


async def processar(
    ticket_id: int,
    *,
    settings: Settings,
    idempotencia: IdempotenciaRepository,
    freshdesk: FreshdeskClient,
    rag_service: RagService,
    claude: ClaudeClient,
    whatsapp: WhatsAppClient,
    busca_web: BuscaWebClient | None = None,
    visao: VisaoClient | None = None,
) -> None:
    # 1. Idempotência — reentrega do mesmo ticket é ignorada.
    if not await idempotencia.marcar_em_processamento(ticket_id):
        logger.info("Ticket %s já processado — ignorando reentrega.", ticket_id)
        return

    ticket: TicketFreshdesk | None = None
    try:
        # 2. Ler o chamado. 2b. Imagens → texto (best-effort). 3-5b. Miolo SEM I/O de saída.
        ticket = await freshdesk.get_ticket(ticket_id)
        ticket = await _incorporar_imagens(
            ticket, freshdesk=freshdesk, visao=visao, settings=settings
        )
        insp = await inspecionar(
            ticket,
            settings=settings,
            rag_service=rag_service,
            claude=claude,
            busca_web=busca_web,
        )
    except Exception:
        logger.exception("Falha no miolo do pipeline (ticket %s) — fallback.", ticket_id)
        await _fallback_seguro(ticket_id, ticket, freshdesk, whatsapp, settings)
        return

    # 6. Ação no Freshdesk (nunca resposta pública — Fase 1 copiloto).
    await freshdesk.criar_nota_interna(ticket_id, insp.nota)
    if ticket.responder_id is not None:
        await freshdesk.atribuir(ticket_id, ticket.responder_id)

    # 7. WhatsApp por último (melhor esforço). Grupo da equipe, se configurado (ADR-029).
    destino = settings.destino_notificacao(ticket.responder_id)
    await whatsapp.enviar(destino, insp.whatsapp)


async def _fallback_seguro(
    ticket_id: int,
    ticket: TicketFreshdesk | None,
    freshdesk: FreshdeskClient,
    whatsapp: WhatsAppClient,
    settings: Settings,
) -> None:
    """Garante ação humana mesmo se o miolo falhou: nota + atribuição + WhatsApp."""
    responder_id = ticket.responder_id if ticket else None
    empresa = ticket.empresa if ticket else EMPRESA_DESCONHECIDA
    try:
        await freshdesk.criar_nota_interna(ticket_id, NOTA_FALLBACK)
        if responder_id is not None:
            await freshdesk.atribuir(ticket_id, responder_id)
    except Exception:
        logger.exception("Fallback: falha ao registrar no Freshdesk (ticket %s).", ticket_id)
    # WhatsApp sempre (melhor esforço, não levanta) — o humano precisa ser avisado.
    destino = settings.destino_notificacao(responder_id)
    await whatsapp.enviar(destino, _whatsapp(Decisao.ESCALAR, ticket_id, empresa))
