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
from app.claude_client import RESPOSTA_ESCALAR_PADRAO, ClaudeClient
from app.config import Settings
from app.freshdesk import FreshdeskClient
from app.models import EMPRESA_DESCONHECIDA, RespostaIA, ResultadoChamado, TicketFreshdesk
from app.portal_service import PortalService
from app.rag import RagService, Similar
from app.texto import limpar_texto
from app.visao import VisaoClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

_NIVEL_CONFIANCA = {"baixa": 0, "media": 1, "alta": 2}

NOTA_FALLBACK = "⚠️ IA indisponível no momento — chamado encaminhado para análise manual."

# Leitura de anexos (ADR-023/035/037/039): cabeçalho do bloco e tetos por chamado (best-effort).
_CABECALHO_IMAGENS = "[Texto extraído de anexos do chamado (imagens/PDF/logs)]"
_MAX_IMAGENS = 4  # teto de imagens/PDFs transcritos por chamado (custo/latência)
_MAX_ANEXOS_TEXTO = 2  # teto de anexos .txt/.log lidos por chamado
_MAX_CHARS_TXT = 6000  # teto por anexo de texto (log pode ter centenas de KB — pega o começo)


class Decisao(StrEnum):
    RESOLVIDO = "resolvido"  # solução ao cliente
    ALCADA_ADMIN = "alcada_admin"  # achou solução, mas é op. de admin -> vai à equipe (ADR-031)
    ESCALAR = "escalar"  # sem solução confiável -> análise humana


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
    tem_solucao_confiavel = (
        r.encontrou_solucao
        and _atende_minimo(r.confianca, confianca_minima)
        and melhor_distancia is not None
        and melhor_distancia <= distancia_maxima
    )
    # ENCAMINHAR À EQUIPE (ADR-031/033): a resposta NÃO vai ao cliente, vai à equipe no grupo,
    # com a direção. Vale quando a resolução exige AÇÃO DA EQUIPE, não uma dúvida que o cliente
    # resolve sozinho: (a) alçada admin — parâmetro/gatilho/tabela/usuário; ou (b) pedido
    # operacional / coordenação — tarefa a executar ou agendar (criar usuário, rodar MRP...).
    # Nesses casos a equipe recebe o que a IA tiver (solução OU brief), mesmo sem match perfeito:
    # o guardrail de distância protege o CLIENTE; para a equipe (que verifica) a direção ajuda.
    if r.alcada_admin or r.pedido_operacional:
        return Decisao.ALCADA_ADMIN
    if tem_solucao_confiavel:
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


def _rotulo_alcada(r: RespostaIA) -> str:
    """' (parâmetro)' etc., para anexar às mensagens; vazio se sem tipo."""
    return f" ({r.tipo_alcada})" if r.tipo_alcada else ""


def _motivo_equipe(r: RespostaIA) -> str:
    """Por que foi à equipe (ADR-033): alçada admin (com tipo) OU tarefa/coordenação."""
    if r.alcada_admin:
        return f"ALÇADA ADMINISTRATIVA{_rotulo_alcada(r)}"
    return "TAREFA / EXECUÇÃO DA EQUIPE"


def _fontes_tentadas(via_portal: bool, via_web: bool) -> str:
    """Texto ' (o Portal … e a busca web … também não trouxe solução)' para o ESCALAR."""
    tried = []
    if via_portal:
        tried.append("o Portal do Cliente TOTVS")
    if via_web:
        tried.append("a busca web em referências TOTVS/Protheus")
    return f" ({' e '.join(tried)} também não trouxe solução)" if tried else ""


def _nota(
    decisao: Decisao,
    r: RespostaIA,
    *,
    via_web: bool = False,
    via_portal: bool = False,
    auto_elegivel: bool = False,
    modo_sombra: bool = False,
) -> str:
    if decisao is Decisao.RESOLVIDO:
        if via_web:
            origem = (
                "🌐 Rascunho gerado a partir de BUSCA WEB em referência técnica TOTVS/Protheus "
                "(confira a fonte no rodapé do trecho e revise antes de enviar)."
            )
        elif via_portal:
            origem = (
                "📋 Rascunho gerado a partir de CHAMADO RESOLVIDO no Portal do Cliente TOTVS "
                "(histórico do parceiro — revise antes de enviar)."
            )
        else:
            origem = "🤖 Rascunho gerado por IA (revisar antes de enviar)."
        # MODO SOMBRA (ADR-042): sinaliza que ESTE rascunho seria auto-enviado na Fase 2 — mas
        # NÃO foi (copiloto). Se o agente editar/descartar, é evidência de que o auto erraria.
        sombra = (
            "🤖 MODO SOMBRA (Fase 2): este rascunho SERIA ENVIADO AUTOMATICAMENTE ao cliente. "
            "NÃO foi enviado — revise; se você editar ou descartar, registra que o auto-envio "
            "erraria aqui.\n\n"
            if modo_sombra and auto_elegivel
            else ""
        )
        return f"{sombra}{origem} Confiança: {r.confianca}.\n\n{r.resposta_cliente}"
    if decisao is Decisao.ALCADA_ADMIN:
        # Requer AÇÃO DA EQUIPE (ADR-031/033): o cliente recebeu só o acolhimento; a direção
        # (solução do erro OU o brief da tarefa/coordenação) está em `resumo`, para a equipe.
        return (
            f"🔧 {_motivo_equipe(r)} — o cliente recebeu só o acolhimento padrão; NÃO instruir "
            "o cliente a executar.\n\n"
            f"— Direção para a equipe —\n{r.resumo_para_responsavel}"
        )
    # ESCALAR: para o TIME. Linha de status TÉCNICA (verdade) + resumo; e o rascunho de
    # acolhimento ao cliente, para o agente revisar e enviar (só a resposta_cliente é
    # "cliente-friendly"; o status acima segue cru). Alçada admin sem solução é sinalizada.
    extra = _fontes_tentadas(via_portal, via_web)
    admin = f" Envolve ALÇADA ADMIN{_rotulo_alcada(r)}." if r.alcada_admin else ""
    return (
        f"⚠️ IA não encontrou solução na base{extra}. Requer análise manual.{admin}\n\n"
        f"Resumo: {r.resumo_para_responsavel}\n\n"
        f"— Rascunho de acolhimento ao cliente (revisar antes de enviar) —\n"
        f"{r.resposta_cliente}"
    )


def _whatsapp(
    decisao: Decisao,
    ticket_id: int,
    empresa: str,
    resposta: RespostaIA | None = None,
    *,
    via_web: bool = False,
    via_portal: bool = False,
) -> str:
    if decisao is Decisao.RESOLVIDO:
        if via_web:
            aviso = " (via BUSCA WEB — revise a fonte com atenção)"
        elif via_portal:
            aviso = " (via Portal TOTVS — chamado resolvido do parceiro, revise)"
        else:
            aviso = ""
        return (
            f"✅ Chamado #{ticket_id} da {empresa} — "
            f"rascunho pronto no Freshdesk para revisão{aviso}."
        )
    if decisao is Decisao.ALCADA_ADMIN and resposta is not None:
        # A EQUIPE recebe a direção COMPLETA + o que o chamado traz (ADR-031/033). O cliente não.
        return (
            f"🔧 Chamado #{ticket_id} da {empresa} — {_motivo_equipe(resposta)}. "
            "O cliente recebeu só o acolhimento. Direção para resolvermos:"
            f"\n\n{resposta.resumo_para_responsavel}"
        )
    admin = (
        f" (envolve alçada admin{_rotulo_alcada(resposta)})"
        if resposta is not None and resposta.alcada_admin
        else ""
    )
    # Mesmo sem solução, a EQUIPE recebe o CONTEXTO do que a IA entendeu do problema (ADR-036) —
    # não só "olhe pessoalmente". O resumo é o que a IA compreendeu do chamado (e das imagens).
    contexto = (
        f"\n\nContexto: {resposta.resumo_para_responsavel}"
        if resposta is not None and resposta.resumo_para_responsavel.strip()
        else ""
    )
    return (
        f"🔴 Chamado #{ticket_id} da {empresa} — não encontrei solução na base do "
        f"TOTVS{admin}. Recomendo olhar pessoalmente.{contexto}"
    )


def _pares_web(trechos: list[str]) -> list[Similar]:
    """Converte os trechos extraídos da web em pares Similar rotulados 'web_totvs'."""
    return [
        Similar(
            ticket_id=None,
            problema="Trecho recuperado por busca web em referência técnica TOTVS/Protheus",
            solucao=trecho,
            empresa=None,
            distancia=1.0,  # sentinela: sem vetor (o guardrail de distância não se aplica à web)
            fonte="web_totvs",
            titulo="Referência técnica TOTVS/Protheus (busca web)",
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
    via_portal: bool = False  # o desfecho veio da busca no Portal do Cliente TOTVS (ADR-026)
    pares_portal: list[Similar] = field(default_factory=list)  # chamados do Portal (se acionado)
    auto_elegivel: bool = False  # candidato a resposta automática (recorte ADR-041) — só marcador
    nota: str = ""  # nota interna que SERIA criada no Freshdesk
    whatsapp: str = ""  # mensagem que SERIA enviada no WhatsApp


def _elegivel_auto(
    decisao: Decisao,
    resposta: RespostaIA,
    melhor_distancia: float | None,
    via_externa: bool,
    limiar_auto: float,
) -> bool:
    """Núcleo do recorte de auto-resposta (ADR-041), por CAMPOS — antes de montar a Inspecao.

    `via_externa` = a resposta veio de uma fonte SEM vetor (web ou Portal) — o guardrail de
    distância não se aplica, então a proximidade não é exigida.
    """
    if decisao is not Decisao.RESOLVIDO or resposta.confianca != "alta":
        return False
    perto = melhor_distancia is not None and melhor_distancia <= limiar_auto
    return perto or via_externa


def elegivel_auto(insp: Inspecao, limiar_auto: float) -> bool:
    """MARCADOR do recorte de auto-resposta (ADR-041) — NÃO envia nada (Fase 1 é copiloto).

    Diz se um chamado SERIA candidato a resposta automática ao cliente (Fase 2), para MEDIR.
    Critério (definido com o usuário): decisão RESOLVIDO, confiança "alta", e o match é próximo
    (melhor par <= `limiar_auto`) OU a resposta veio da busca web. Só marca; virar a Fase 2
    depende da medição humana provar que os candidatos acertam.
    """
    melhor = min((p.distancia for p in insp.pares), default=None)
    externa = insp.via_web or insp.via_portal
    return _elegivel_auto(insp.decisao, insp.resposta, melhor, externa, limiar_auto)


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
    portal_service: PortalService | None = None,
) -> Inspecao:
    """MIOLO do pipeline: reformular query → RAG → Claude → decisão → (Portal → busca web).

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

    # PORTAL DO CLIENTE TOTVS (ADR-026): fonte do PARCEIRO (chamados resolvidos com a TOTVS),
    # tentada ANTES da web pública — mais autoritativa. Só no ESCALAR, atrás da flag. Busca por
    # palavra-chave usando a `query` (intenção reformulada).
    via_portal = False
    pares_portal: list[Similar] = []
    if (
        settings.portal_totvs_ativo
        and portal_service is not None
        and decisao is Decisao.ESCALAR
    ):
        resposta, decisao, via_portal, pares_portal = await _tentar_portal(
            problema,
            query,
            ticket.id,
            resposta,
            decisao,
            portal_service=portal_service,
            claude=claude,
        )

    # ÚLTIMO RECURSO: só se AINDA escalou (Portal não resolveu) e a flag da web está ligada.
    # Pedido operacional NÃO vai à web — é execução humana, não uma dúvida pesquisável.
    via_web = False
    pares_web: list[Similar] = []
    query_web = ""
    # A web tenta em TODO ESCALAR (ADR-034): "escalou" já significa que NÃO há solução local
    # confiável — seja porque não achou, seja porque o guardrail de distância (ADR-030) rejeitou
    # um `encontrou=true` de match distante (ex.: #4446, docs de NFSE p/ NF de entrada). Antes o
    # gate exigia `not encontrou_solucao` e pulava justamente esse caso. Pedido operacional já vira
    # ALCADA_ADMIN (não ESCALAR), então não chega aqui.
    if (
        settings.busca_web_ativa
        and busca_web is not None
        and decisao is Decisao.ESCALAR
    ):
        # Registra a query REAL enviada aos domínios TOTVS (mesmo que a web volte vazia),
        # para a interface mostrar exatamente o que foi pesquisado (ADR-027).
        query_web = montar_query_web(problema)
        resposta, decisao, via_web, pares_web = await _tentar_busca_web(
            problema, ticket.id, resposta, decisao, busca_web=busca_web, claude=claude
        )

    # GARANTIA FINAL, guiada pela DECISÃO (ADR-032): o cliente só recebe a solução no RESOLVIDO.
    # Em qualquer outra decisão — inclusive quando o guardrail de distância (ADR-030) escala um
    # `encontrou=true` (ex.: #4446/#4438) — a resposta ao cliente é a saudação-padrão. O
    # saneamento do claude_client é por-fonte (encontrou/alcada) e NÃO enxerga a distância;
    # este é o ponto que fecha o furo, porque aqui a decisão final já é conhecida.
    if decisao is not Decisao.RESOLVIDO and resposta.resposta_cliente != RESPOSTA_ESCALAR_PADRAO:
        resposta = resposta.model_copy(update={"resposta_cliente": RESPOSTA_ESCALAR_PADRAO})

    # Recorte de auto-resposta (ADR-041) + modo sombra (ADR-042): calculado após a decisão final
    # (inclui o desfecho da web), para marcar e — se em sombra — sinalizar na nota. NÃO envia nada.
    auto_elegivel = _elegivel_auto(
        decisao, resposta, melhor_distancia, via_web or via_portal, settings.limiar_auto_resposta
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
        via_portal=via_portal,
        pares_portal=pares_portal,
        auto_elegivel=auto_elegivel,
        nota=_nota(
            decisao,
            resposta,
            via_web=via_web,
            via_portal=via_portal,
            auto_elegivel=auto_elegivel,
            modo_sombra=settings.modo_sombra_auto,
        ),
        whatsapp=_whatsapp(
            decisao, ticket.id, ticket.empresa, resposta, via_web=via_web, via_portal=via_portal
        ),
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
        if resposta_web.encontrou_solucao:
            # Solução da web de alçada admin também vai à equipe, não ao cliente (ADR-031).
            decisao_web = (
                Decisao.ALCADA_ADMIN if resposta_web.alcada_admin else Decisao.RESOLVIDO
            )
        else:
            decisao_web = Decisao.ESCALAR
        return resposta_web, decisao_web, True, pares_web
    except Exception:
        logger.exception(
            "Busca web (último recurso) falhou (ticket %s) — mantém escala.", ticket_id
        )
        return resposta, decisao, False, []


async def _tentar_portal(
    problema: str,
    query: str,
    ticket_id: int,
    resposta: RespostaIA,
    decisao: Decisao,
    *,
    portal_service: PortalService,
    claude: ClaudeClient,
) -> tuple[RespostaIA, Decisao, bool, list[Similar]]:
    """Busca no Portal TOTVS e reconsulta o Claude com os chamados resolvidos. Best-effort.

    Retorna (resposta, decisao, via_portal, pares_portal). via_portal=True só quando o Portal
    trouxe pares e o Claude foi reconsultado (o desfecho passa a ser o dessa 2ª chamada). Fonte
    "menos verificada" (histórico do parceiro) — revisão humana obrigatória (Fase 1). Solução do
    Portal de alçada admin também vai à equipe, não ao cliente (ADR-031).
    """
    try:
        pares = await portal_service.buscar(query)  # best-effort: nunca levanta
        if not pares:
            return resposta, decisao, False, []
        resposta_p = await claude.gerar_resposta(problema, pares)
        if resposta_p.encontrou_solucao:
            decisao_p = Decisao.ALCADA_ADMIN if resposta_p.alcada_admin else Decisao.RESOLVIDO
        else:
            decisao_p = Decisao.ESCALAR
        return resposta_p, decisao_p, True, pares
    except Exception:
        logger.exception(
            "Portal TOTVS (último recurso) falhou (ticket %s) — mantém escala.", ticket_id
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
    """Transcreve o texto legível das imagens do chamado e concatena à descrição (ADR-023/035).

    Lê DUAS fontes de imagem, com um teto TOTAL de `_MAX_IMAGENS`: anexos (Anexo) e imagens
    INLINE coladas no corpo do e-mail (ADR-035 — o caso mais comum). BEST-EFFORT: falha ao
    baixar/transcrever uma imagem é ignorada (não derruba o chamado). Sem visão/imagens, retorna
    o ticket INALTERADO. A transcrição entra ANTES da busca — vira parte da query e do contexto.
    """
    if visao is None or not settings.leitura_imagens_ativa:
        return ticket

    # (bytes-loader, rótulo p/ log) por imagem, anexos primeiro, até o teto TOTAL.
    async def _do_anexo(a):
        return await freshdesk.baixar_anexo(a.attachment_url), a.content_type

    async def _do_inline(url):
        return await freshdesk.baixar_imagem(url)

    fontes = [(_do_anexo, a, f"img {a.id}") for a in ticket.imagens]
    fontes += [(_do_anexo, a, f"pdf {a.id}") for a in ticket.pdfs]  # PDFs anexos (ADR-037)
    fontes += [(_do_inline, u, "inline") for u in ticket.imagens_inline]
    fontes = fontes[:_MAX_IMAGENS]
    if not fontes and not ticket.anexos_texto:  # nada para ler (imagem/PDF/inline/texto)
        return ticket

    trechos: list[str] = []
    for carregar, ref, rotulo in fontes:
        try:
            dados, tipo = await carregar(ref)
            texto = await visao.transcrever(dados, tipo)
        except Exception:
            logger.exception("Falha ao ler imagem (%s, ticket %s) — ignorada.", rotulo, ticket.id)
            continue
        if texto.strip():
            trechos.append(texto.strip())

    # Anexos de TEXTO (.txt/.log — ex.: log de erro) — lidos DIRETO, sem Claude (ADR-039).
    # Log pode ter centenas de KB: pega só o começo (onde o erro costuma estar) até o teto.
    for anexo in ticket.anexos_texto[:_MAX_ANEXOS_TEXTO]:
        try:
            dados = await freshdesk.baixar_anexo(anexo.attachment_url)
        except Exception:
            logger.exception("Falha ao baixar anexo texto %s (ticket %s).", anexo.id, ticket.id)
            continue
        conteudo = dados.decode("utf-8", errors="replace").strip()[:_MAX_CHARS_TXT]
        if conteudo:
            trechos.append(f"[Anexo {anexo.name}]\n{conteudo}")

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
    portal_service: PortalService | None = None,
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
            portal_service=portal_service,
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
