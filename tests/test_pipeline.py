"""Testes do Módulo 7 — pipeline.py.

Todos os clientes são falsos/injetados — nenhuma chamada real. Cobre: a função de
decisão (todos os caminhos de confiança), idempotência (reentrega ignorada), o fluxo
resolvido, o fluxo escalar e o fallback quando o miolo falha.
"""

import pytest

from app.claude_client import RESPOSTA_ESCALAR_PADRAO
from app.models import Anexo, Requester, RespostaIA, ResultadoChamado, TicketFreshdesk
from app.pipeline import (
    Decisao,
    Inspecao,
    _incorporar_imagens,
    decidir,
    inspecionar,
    processar,
)
from app.rag import Similar

# --- fakes -----------------------------------------------------------------


class FakeIdempotencia:
    def __init__(self, ja_processados=None):
        self.ja = set(ja_processados or [])

    async def marcar_em_processamento(self, ticket_id):
        if ticket_id in self.ja:
            return False
        self.ja.add(ticket_id)
        return True


class FakeFreshdesk:
    def __init__(self, ticket=None, erro_get=None, anexo_bytes=b"IMG", erro_anexo=None):
        self._ticket = ticket
        self._erro_get = erro_get
        self._anexo_bytes = anexo_bytes
        self._erro_anexo = erro_anexo
        self.notas = []
        self.atribuicoes = []
        self.baixados = []

    async def get_ticket(self, ticket_id):
        if self._erro_get is not None:
            raise self._erro_get
        return self._ticket

    async def criar_nota_interna(self, ticket_id, body):
        self.notas.append((ticket_id, body))

    async def atribuir(self, ticket_id, responder_id):
        self.atribuicoes.append((ticket_id, responder_id))

    async def baixar_anexo(self, url):
        self.baixados.append(url)
        if self._erro_anexo is not None:
            raise self._erro_anexo
        return self._anexo_bytes

    async def baixar_imagem(self, url):
        self.baixados.append(url)
        return b"INLINE-IMG", "image/png"


class FakeVisao:
    def __init__(self, texto="", erro=None):
        self._texto = texto
        self._erro = erro
        self.chamadas = []

    async def transcrever(self, imagem, content_type):
        self.chamadas.append((imagem, content_type))
        if self._erro is not None:
            raise self._erro
        return self._texto


class FakeRag:
    def __init__(self, pares=None):
        self._pares = pares or []
        self.ultima_query = None
        self.queries: list[str] = []  # queries da última busca (união, ADR-024)

    async def buscar(self, problema, k=5):
        self.ultima_query = problema
        self.queries = [problema]
        return self._pares

    async def buscar_uniao(self, queries, k=5):
        self.queries = list(dict.fromkeys(q for q in queries if q.strip()))
        self.ultima_query = self.queries[-1] if self.queries else None
        return self._pares


class FakeClaude:
    """`query`/`erro_query` controlam a reformulação (ADR-024); None = devolve o problema."""

    def __init__(self, resposta=None, erro=None, query=None, erro_query=None):
        self._resposta = resposta
        self._erro = erro
        self._query = query
        self._erro_query = erro_query
        self.problemas_recebidos: list[str] = []
        self.reformulacoes: list[str] = []

    async def gerar_resposta(self, problema, pares):
        self.problemas_recebidos.append(problema)
        if self._erro is not None:
            raise self._erro
        return self._resposta

    async def reformular_query(self, problema):
        self.reformulacoes.append(problema)
        if self._erro_query is not None:
            raise self._erro_query
        return self._query if self._query is not None else problema


class FakeWhatsApp:
    def __init__(self):
        self.enviados = []

    async def enviar(self, numero, texto):
        self.enviados.append((numero, texto))
        return True


class FakeBuscaWeb:
    """Substitui o BuscaWebClient: registra chamadas e devolve trechos canned."""

    def __init__(self, trechos=None):
        self._trechos = trechos or []
        self.chamadas = []

    async def buscar(self, problema):
        self.chamadas.append(problema)
        return self._trechos


class FakeClaudeRoteia:
    """Devolve resposta diferente conforme a origem dos pares (local × web)."""

    def __init__(self, local, web):
        self._local = local
        self._web = web
        self.chamadas = []

    async def gerar_resposta(self, problema, pares):
        self.chamadas.append(list(pares))
        veio_web = any(getattr(p, "fonte", None) == "web_totvs" for p in pares)
        return self._web if veio_web else self._local

    async def reformular_query(self, problema):
        return problema


def _ticket(responder_id=55, empresa="Empresa A"):
    return TicketFreshdesk(
        id=101,
        subject="Erro no lançamento da NF",
        description_text="Log SCC19070 / MV_ATFMOED.",
        priority="alta",
        status=2,
        requester=Requester(name="Cliente"),
        empresa=empresa,
        responder_id=responder_id,
    )


def _ticket_com_imagem(
    url="https://s3/att/1.png", content_type="image/png", descricao="Segue print."
):
    return TicketFreshdesk(
        id=101,
        subject="Erro na NF",
        description_text=descricao,
        priority="alta",
        status=2,
        requester=Requester(name="Cliente"),
        empresa="Empresa A",
        responder_id=55,
        attachments=[
            Anexo(id=1, name="print.png", content_type=content_type, attachment_url=url)
        ],
    )


def _resposta(
    encontrou=True,
    confianca="alta",
    pedido_operacional=False,
    cliente=None,
    alcada_admin=False,
    tipo_alcada="",
):
    return RespostaIA(
        resposta_cliente=cliente or "Atualize a taxa da moeda 3 e reprocesse a NF.",
        encontrou_solucao=encontrou,
        confianca=confianca,
        resumo_para_responsavel="Moeda 3 do Ativo Fixo sem taxa do dia.",
        urgencia="alta",
        pedido_operacional=pedido_operacional,
        alcada_admin=alcada_admin,
        tipo_alcada=tipo_alcada,
    )


def _resultado(encontrou, confianca, alcada_admin=False):
    return ResultadoChamado(
        ticket_id=1,
        empresa="X",
        resposta=_resposta(encontrou, confianca, alcada_admin=alcada_admin),
    )


# --- decisão (função pura) -------------------------------------------------


@pytest.mark.parametrize(
    "encontrou,confianca,minimo,esperado",
    [
        (True, "alta", "alta", Decisao.RESOLVIDO),
        (True, "media", "alta", Decisao.ESCALAR),  # confiança abaixo do mínimo
        (True, "baixa", "alta", Decisao.ESCALAR),
        (True, "media", "media", Decisao.RESOLVIDO),
        (True, "alta", "media", Decisao.RESOLVIDO),
        (True, "baixa", "baixa", Decisao.RESOLVIDO),
        (False, "alta", "baixa", Decisao.ESCALAR),  # não encontrou -> escalar sempre
    ],
)
def test_decidir(encontrou, confianca, minimo, esperado):
    # Distância boa (0,1 <= 0,40): isola a dimensão de CONFIANÇA. O guardrail é testado à parte.
    assert (
        decidir(
            _resultado(encontrou, confianca),
            minimo,
            melhor_distancia=0.1,
            distancia_maxima=0.40,
        )
        is esperado
    )


@pytest.mark.parametrize(
    "distancia,esperado",
    [
        (0.31, Decisao.RESOLVIDO),  # match bom, bem abaixo do limiar
        (0.40, Decisao.RESOLVIDO),  # exatamente no limiar (<=)
        (0.4017, Decisao.ESCALAR),  # logo acima do limiar
        (0.4617, Decisao.ESCALAR),  # #4446: doc de NFSE p/ NF de ENTRADA — match distante
        (None, Decisao.ESCALAR),  # nenhum par recuperado -> sem confirmação objetiva
    ],
)
def test_decidir_guardrail_distancia_supera_autoavaliacao(distancia, esperado):
    # O Claude disse encontrou=True / confiança "alta", mas a DISTÂNCIA objetiva decide (ADR-030).
    resultado = _resultado(encontrou=True, confianca="alta")
    assert (
        decidir(resultado, "alta", melhor_distancia=distancia, distancia_maxima=0.40)
        is esperado
    )


def test_decidir_alcada_admin_com_solucao_vira_terceira_via():
    # Solução CONFIÁVEL, mas de alçada admin -> nem RESOLVIDO nem ESCALAR: ALCADA_ADMIN (ADR-031).
    resultado = _resultado(encontrou=True, confianca="alta", alcada_admin=True)
    d = decidir(resultado, "alta", melhor_distancia=0.30, distancia_maxima=0.40)
    assert d is Decisao.ALCADA_ADMIN


def test_decidir_alcada_admin_pedido_operacional_vai_a_equipe():
    # Tarefa operacional de admin (ex.: criar usuário): encontrou=false, mas há brief a entregar
    # -> ALCADA_ADMIN (o grupo recebe a direção), não ESCALAR comum. ADR-031.
    resultado = ResultadoChamado(
        ticket_id=1,
        empresa="X",
        resposta=_resposta(encontrou=False, confianca="media", pedido_operacional=True,
                           alcada_admin=True, tipo_alcada="usuário"),
    )
    d = decidir(resultado, "alta", melhor_distancia=0.26, distancia_maxima=0.40)
    assert d is Decisao.ALCADA_ADMIN


def test_decidir_admin_mesmo_com_match_distante_vai_a_equipe():
    # ADR-033: admin vai à EQUIPE com a direção que a IA tiver, MESMO com match distante — o
    # guardrail protege o cliente, não a equipe (que verifica). Não escala terse.
    resultado = _resultado(encontrou=True, confianca="alta", alcada_admin=True)
    d = decidir(resultado, "alta", melhor_distancia=0.47, distancia_maxima=0.40)
    assert d is Decisao.ALCADA_ADMIN


def test_decidir_sem_admin_sem_tarefa_e_match_distante_escala():
    # Sem alçada admin, sem tarefa e match distante -> ESCALAR comum (guardrail, ADR-030).
    resultado = _resultado(encontrou=True, confianca="alta", alcada_admin=False)
    d = decidir(resultado, "alta", melhor_distancia=0.47, distancia_maxima=0.40)
    assert d is Decisao.ESCALAR


# --- fluxo resolvido -------------------------------------------------------


async def test_fluxo_resolvido(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
        whatsapp=wa,
    )

    assert len(fd.notas) == 1
    assert "🤖 Rascunho gerado por IA" in fd.notas[0][1]
    assert "Confiança: alta" in fd.notas[0][1]
    assert fd.atribuicoes == [(101, 55)]
    numero, texto = wa.enviados[0]
    assert numero == settings.whatsapp_responsavel_default  # sem mapeamento -> default
    assert "✅ Chamado #101 da Empresa A" in texto


async def test_fluxo_com_grupo_configurado_notifica_o_grupo(settings):
    # Com WHATSAPP_GRUPO_DESTINO setado, o feedback vai ao GRUPO, não ao responsável (ADR-029).
    grupo = "120363018941234567@g.us"
    cfg = settings.model_copy(update={"whatsapp_grupo_destino": grupo})
    fd = FakeFreshdesk(ticket=_ticket(responder_id=55))
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=cfg,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
        whatsapp=wa,
    )

    assert fd.atribuicoes == [(101, 55)]  # atribuição ao responsável segue igual
    numero, _ = wa.enviados[0]
    assert numero == grupo  # mas a notificação vai ao grupo


# --- fluxo escalar ---------------------------------------------------------


async def test_fluxo_escalar(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([]),
        claude=FakeClaude(resposta=_resposta(encontrou=False, confianca="baixa")),
        whatsapp=wa,
    )

    assert "⚠️ IA não encontrou solução na base" in fd.notas[0][1]
    assert "Resumo: Moeda 3 do Ativo Fixo sem taxa do dia." in fd.notas[0][1]
    assert fd.atribuicoes == [(101, 55)]
    assert "🔴 Chamado #101 da Empresa A" in wa.enviados[0][1]


# --- idempotência ----------------------------------------------------------


async def test_segunda_entrega_do_mesmo_ticket_e_ignorada(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()
    idem = FakeIdempotencia()
    comuns = dict(
        settings=settings,
        idempotencia=idem,
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
        whatsapp=wa,
    )

    await processar(101, **comuns)  # 1ª entrega: processa
    await processar(101, **comuns)  # 2ª entrega: ignorada

    assert len(fd.notas) == 1
    assert len(fd.atribuicoes) == 1
    assert len(wa.enviados) == 1


# --- fallback --------------------------------------------------------------


async def test_fallback_quando_claude_falha(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(erro=RuntimeError("claude fora do ar")),
        whatsapp=wa,
    )

    assert len(fd.notas) == 1
    assert "IA indisponível no momento" in fd.notas[0][1]
    assert fd.atribuicoes == [(101, 55)]
    assert "🔴 Chamado #101 da Empresa A" in wa.enviados[0][1]


# --- busca web como último recurso (ADR-015) -------------------------------


def _settings_web_on(settings):
    return settings.model_copy(update={"busca_web_ativa": True})


async def test_base_local_resolve_nao_chama_web(settings):
    # Base local resolve (encontrou + confiança alta) -> web NÃO deve ser consultada,
    # mesmo com a flag ligada.
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()
    web = FakeBuscaWeb(trechos=["não deveria ser usado"])

    await processar(
        101,
        settings=_settings_web_on(settings),
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
        whatsapp=wa,
        busca_web=web,
    )

    assert web.chamadas == []  # nunca chamada
    assert "🤖 Rascunho gerado por IA" in fd.notas[0][1]


async def test_escala_por_falta_de_contexto_web_traz_solucao(settings):
    # Local escala (não encontrou) -> web traz conteúdo -> Claude reconsultado -> resolve
    # ANCORADO nos trechos web, com nota marcada como fonte menos verificada.
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()
    web = FakeBuscaWeb(trechos=["[https://tdn.totvs.com/x]\nAtualize a taxa da moeda 3."])
    claude = FakeClaudeRoteia(
        local=_resposta(encontrou=False, confianca="baixa"),
        web=_resposta(encontrou=True, confianca="media"),
    )

    await processar(
        101,
        settings=_settings_web_on(settings),
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([]),
        claude=claude,
        whatsapp=wa,
        busca_web=web,
    )

    assert web.chamadas  # a web foi consultada
    assert len(claude.chamadas) == 2  # local + reconsulta com a web
    assert any(getattr(p, "fonte", None) == "web_totvs" for p in claude.chamadas[-1])
    assert "🌐 Rascunho gerado a partir de BUSCA WEB" in fd.notas[0][1]
    assert "via BUSCA WEB" in wa.enviados[0][1]


async def test_web_vazia_escala_mesmo_assim(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()
    web = FakeBuscaWeb(trechos=[])  # web não trouxe nada
    claude = FakeClaudeRoteia(
        local=_resposta(encontrou=False, confianca="baixa"),
        web=_resposta(encontrou=True, confianca="alta"),
    )

    await processar(
        101,
        settings=_settings_web_on(settings),
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([]),
        claude=claude,
        whatsapp=wa,
        busca_web=web,
    )

    assert web.chamadas  # tentou a web
    assert len(claude.chamadas) == 1  # não houve reconsulta (web vazia)
    assert "⚠️ IA não encontrou solução na base" in fd.notas[0][1]
    assert "🔴 Chamado #101 da Empresa A" in wa.enviados[0][1]


async def test_web_tenta_quando_guardrail_rejeita_encontrou_true(settings):
    # ADR-034: o Claude achou algo LOCAL (encontrou=true) mas o guardrail escalou (par distante,
    # 0,46). Antes o gate `not encontrou` pulava a web; agora ela TENTA e pode resolver (#4446).
    web = FakeBuscaWeb(trechos=["[https://tdn.totvs.com/x]\nSolução correta da web."])
    claude = FakeClaudeRoteia(
        local=_resposta(encontrou=True, confianca="alta"),  # achou local, mas...
        web=_resposta(encontrou=True, confianca="media"),
    )
    par_distante = Similar(None, "p", "s", None, 0.46, fonte="documentacao", titulo="x")

    insp = await inspecionar(
        _ticket(),
        settings=_settings_web_on(settings),
        rag_service=FakeRag([par_distante]),
        claude=claude,
        busca_web=web,
    )

    assert web.chamadas  # a web foi tentada APESAR de encontrou=true no local
    assert insp.via_web is True
    assert insp.decisao is Decisao.RESOLVIDO  # a web trouxe a solução


async def test_flag_desligada_nunca_chama_web(settings):
    fd = FakeFreshdesk(ticket=_ticket())
    wa = FakeWhatsApp()
    web = FakeBuscaWeb(trechos=["não deveria ser usado"])

    await processar(
        101,
        settings=settings,  # flag padrão = False
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([]),
        claude=FakeClaude(resposta=_resposta(encontrou=False, confianca="baixa")),
        whatsapp=wa,
        busca_web=web,
    )

    assert web.chamadas == []  # flag off -> web nunca chamada
    assert "⚠️ IA não encontrou solução na base" in fd.notas[0][1]


# --- inspecionar: miolo SEM efeitos colaterais (ADR-019) -------------------
# `inspecionar` NÃO recebe Freshdesk nem WhatsApp — por construção não há como escrever
# nota ou enviar mensagem. Estes testes exercem o mesmo miolo que a interface de teste usa.


async def test_inspecionar_resolvido(settings):
    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
    )

    assert isinstance(insp, Inspecao)
    assert insp.decisao is Decisao.RESOLVIDO
    assert "🤖 Rascunho gerado por IA" in insp.nota
    assert "✅ Chamado #101 da Empresa A" in insp.whatsapp
    assert len(insp.pares) == 1 and insp.pares[0].fonte == "ticket"
    assert insp.via_web is False and insp.pares_web == []


async def test_inspecionar_escalar(settings):
    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([]),
        claude=FakeClaude(resposta=_resposta(encontrou=False, confianca="baixa")),
    )

    assert insp.decisao is Decisao.ESCALAR
    assert "⚠️ IA não encontrou solução na base" in insp.nota
    assert "🔴 Chamado #101" in insp.whatsapp
    # ADR-036: mesmo escalando, a equipe recebe o CONTEXTO (o resumo do que a IA entendeu).
    assert "Contexto: Moeda 3 do Ativo Fixo sem taxa do dia." in insp.whatsapp
    assert insp.query_web == ""  # web desligada -> nada foi pesquisado na web


async def test_inspecionar_guardrail_distancia_escala_apesar_de_alta(settings):
    # Caso #4446: o Claude diz encontrou=True / "alta", mas o melhor par local está a 0,46
    # (doc de NFSE para uma NF de entrada). O guardrail de distância força ESCALAR (ADR-030).
    par_distante = Similar(None, "p", "s", None, 0.4617, fonte="documentacao", titulo="NFSE")

    insp = await inspecionar(
        _ticket(),
        settings=settings,  # busca_web desligada na fixture -> não mascara com a web
        rag_service=FakeRag([par_distante]),
        claude=FakeClaude(
            resposta=_resposta(encontrou=True, confianca="alta", cliente="Altere o MV_ATFMOED.")
        ),
    )

    assert insp.decisao is Decisao.ESCALAR
    assert "⚠️ IA não encontrou solução na base" in insp.nota
    # ADR-032: guardrail escalou um "encontrou=true" -> o cliente NÃO recebe a resposta técnica.
    assert insp.resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO
    assert "MV_ATFMOED" not in insp.resposta.resposta_cliente  # o texto do modelo não vaza


async def test_inspecionar_resolve_quando_par_esta_dentro_do_limiar(settings):
    # Contraprova: mesma confiança "alta", mas par a 0,39 (<= 0,40) -> RESOLVIDO.
    par_perto = Similar(None, "p", "s", None, 0.39, fonte="documentacao", titulo="doc")

    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([par_perto]),
        claude=FakeClaude(resposta=_resposta(encontrou=True, confianca="alta")),
    )

    assert insp.decisao is Decisao.RESOLVIDO


# --- alçada administrativa: solução vai à EQUIPE, não ao cliente (ADR-031) --


async def test_inspecionar_alcada_admin_encaminha_solucao_a_equipe(settings):
    # Solução confiável (par 0,30) MAS de alçada admin -> ALCADA_ADMIN: o grupo recebe a
    # direção (resumo), a nota marca alçada, e o cliente não recebe os passos.
    par_perto = Similar(None, "p", "s", None, 0.30, fonte="ticket")
    resposta = _resposta(
        encontrou=True, confianca="alta", alcada_admin=True, tipo_alcada="usuário"
    )
    resposta = resposta.model_copy(
        update={"resumo_para_responsavel": "Criar usuário X copiando o perfil de Y (SIGACFG)."}
    )

    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([par_perto]),
        claude=FakeClaude(resposta=resposta),
    )

    assert insp.decisao is Decisao.ALCADA_ADMIN
    # Grupo/WhatsApp recebe a direção completa (o resumo), rotulada como alçada admin.
    assert "ALÇADA ADMINISTRATIVA (usuário)" in insp.whatsapp
    assert "Criar usuário X copiando o perfil de Y" in insp.whatsapp
    # Nota interna marca a alçada e traz a direção; o cliente não recebe os passos.
    assert "ALÇADA ADMINISTRATIVA (usuário)" in insp.nota
    assert "NÃO instruir o cliente" in insp.nota


async def test_inspecionar_alcada_admin_pedido_operacional_manda_brief_ao_grupo(settings):
    # Tarefa operacional de admin (criar usuário, #4450): encontrou=false, mas o grupo recebe
    # o brief completo (resumo), não a mensagem curta de escalar (ADR-031).
    brief = "Criar usuário para Lauriany, copiando o perfil da Jessyca (SIGACFG)."
    resposta = _resposta(
        encontrou=False, confianca="media", pedido_operacional=True,
        alcada_admin=True, tipo_alcada="usuário",
    ).model_copy(update={"resumo_para_responsavel": brief})

    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([Similar(2, "p", "s", "E", 0.26, fonte="ticket")]),
        claude=FakeClaude(resposta=resposta),
    )

    assert insp.decisao is Decisao.ALCADA_ADMIN
    assert brief in insp.whatsapp  # o grupo recebe a direção completa
    assert "ALÇADA ADMINISTRATIVA (usuário)" in insp.whatsapp


async def test_inspecionar_admin_distante_ainda_encaminha_a_equipe(settings):
    # ADR-033: admin com match distante (0,47) NÃO resolve ao cliente, mas a equipe recebe a
    # direção que a IA tem (não escala terse). O cliente recebe só o acolhimento.
    par_distante = Similar(None, "p", "s", None, 0.47, fonte="documentacao", titulo="doc")
    resposta = _resposta(
        encontrou=True, confianca="alta", alcada_admin=True, tipo_alcada="parâmetro"
    ).model_copy(update={"resumo_para_responsavel": "Ajustar MV_ATFMOED — verificar release."})

    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([par_distante]),
        claude=FakeClaude(resposta=resposta),
    )

    assert insp.decisao is Decisao.ALCADA_ADMIN
    assert "ALÇADA ADMINISTRATIVA (parâmetro)" in insp.whatsapp
    assert "Ajustar MV_ATFMOED" in insp.whatsapp  # a equipe recebe a direção
    assert insp.resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO  # cliente só o acolhimento


async def test_inspecionar_aciona_web_e_expoe_pares_web(settings):
    web = FakeBuscaWeb(trechos=["[https://tdn.totvs.com/x]\nFaça Y para resolver."])
    claude = FakeClaudeRoteia(
        local=_resposta(encontrou=False, confianca="baixa"),
        web=_resposta(encontrou=True, confianca="media"),
    )

    insp = await inspecionar(
        _ticket(),
        settings=_settings_web_on(settings),
        rag_service=FakeRag([]),
        claude=claude,
        busca_web=web,
    )

    assert insp.via_web is True
    assert insp.pares_web and insp.pares_web[0].fonte == "web_totvs"
    # ADR-027: a interface expõe a query REAL enviada aos domínios oficiais.
    assert insp.query_web
    assert "site:centraldeatendimento.totvs.com" in insp.query_web
    assert insp.decisao is Decisao.RESOLVIDO
    assert "🌐 Rascunho gerado a partir de BUSCA WEB" in insp.nota


async def test_inspecionar_web_vazia_expoe_query_mas_mantem_escala(settings):
    # A busca web disparou mas não achou nada nos domínios: a query pesquisada ainda deve
    # aparecer (para o revisor ver o que foi buscado), sem virar resolução (ADR-027).
    web = FakeBuscaWeb(trechos=[])
    claude = FakeClaudeRoteia(
        local=_resposta(encontrou=False, confianca="baixa"),
        web=_resposta(encontrou=True, confianca="media"),
    )

    insp = await inspecionar(
        _ticket(),
        settings=_settings_web_on(settings),
        rag_service=FakeRag([]),
        claude=claude,
        busca_web=web,
    )

    assert insp.via_web is False and insp.pares_web == []
    assert insp.query_web and "site:tdn.totvs.com" in insp.query_web  # buscou, e mostra o quê
    assert insp.decisao is Decisao.ESCALAR


# --- tom ao cliente × verdade técnica ao time (ADR-020) --------------------


async def test_resolver_entrega_solucao_direta_ao_cliente(settings):
    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta", cliente="Atualize a taxa da moeda 3.")),
    )

    assert insp.decisao is Decisao.RESOLVIDO
    assert "🤖 Rascunho gerado por IA" in insp.nota  # cabeçalho para o TIME
    assert "Atualize a taxa da moeda 3." in insp.nota  # solução direta ao cliente


async def test_escalar_acolhe_cliente_mas_nota_mantem_verdade_tecnica(settings):
    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([]),
        claude=FakeClaude(resposta=_resposta(encontrou=False, confianca="baixa")),
    )

    assert insp.decisao is Decisao.ESCALAR
    # Nota (TIME): verdade técnica crua...
    assert "⚠️ IA não encontrou solução na base" in insp.nota
    assert "Requer análise manual" in insp.nota
    # ...e o rascunho de ACOLHIMENTO (a saudação-padrão) para o agente enviar ao cliente.
    assert RESPOSTA_ESCALAR_PADRAO in insp.nota
    assert insp.resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO


async def test_pedido_operacional_vai_a_equipe_e_nao_busca_web(settings):
    # Pedido operacional / coordenação (ex.: agendar rodada) -> EQUIPE (ADR-033), não web.
    web = FakeBuscaWeb(trechos=["não deveria ser usado"])
    insp = await inspecionar(
        _ticket(),
        settings=_settings_web_on(settings),  # web LIGADA, mas execução da equipe não usa
        rag_service=FakeRag([]),
        claude=FakeClaude(
            resposta=_resposta(encontrou=False, confianca="baixa", pedido_operacional=True)
        ),
        busca_web=web,
    )

    assert insp.decisao is Decisao.ALCADA_ADMIN  # execução/coordenação -> equipe
    assert web.chamadas == []  # execução da equipe não é dúvida pesquisável → não busca web
    assert insp.via_web is False
    assert "TAREFA / EXECUÇÃO DA EQUIPE" in insp.whatsapp  # rótulo de tarefa (não admin)
    assert insp.resposta.resposta_cliente == RESPOSTA_ESCALAR_PADRAO  # cliente só o acolhimento


async def test_fallback_quando_get_ticket_falha_usa_defaults(settings):
    fd = FakeFreshdesk(erro_get=RuntimeError("freshdesk 500 no get"))
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([]),
        claude=FakeClaude(resposta=None),
        whatsapp=wa,
    )

    assert "IA indisponível no momento" in fd.notas[0][1]
    assert fd.atribuicoes == []  # sem responder_id conhecido -> não atribui
    numero, texto = wa.enviados[0]
    assert numero == settings.whatsapp_responsavel_default
    assert "Empresa não identificada" in texto


# --- leitura de imagens (ADR-023) ------------------------------------------


async def test_incorporar_imagens_concatena_transcricao(settings):
    fd = FakeFreshdesk(anexo_bytes=b"\x89PNG-bytes")
    visao = FakeVisao(texto="SCC19070 no MV_ATFMOED")

    novo = await _incorporar_imagens(
        _ticket_com_imagem(descricao="Segue print."),
        freshdesk=fd,
        visao=visao,
        settings=settings,
    )

    assert fd.baixados == ["https://s3/att/1.png"]  # baixou o anexo
    assert visao.chamadas[0][0] == b"\x89PNG-bytes"  # os bytes foram transcritos
    assert "Segue print." in novo.description_text  # descrição original preservada
    assert "SCC19070 no MV_ATFMOED" in novo.description_text  # transcrição concatenada


async def test_incorporar_imagens_le_anexo_txt(settings):
    # ADR-039 (caso #4427): cliente anexa Erro.txt com o log; a IA lê o texto direto (sem Claude)
    # e concatena à busca. Log grande é truncado no teto.
    log = "THREAD ERROR SCC19070 na rotina CTBA100. " + ("x" * 20000)
    fd = FakeFreshdesk(anexo_bytes=log.encode("utf-8"))
    visao = FakeVisao(texto="")  # sem imagem/PDF; o .txt não passa pela visão
    ticket = TicketFreshdesk(
        id=101, subject="Erro na contabilização", description_text="Segue o erro em anexo.",
        priority="baixa", status=2, requester=Requester(name="C"), empresa="E",
        responder_id=None,
        attachments=[Anexo(id=7, name="Erro.txt", content_type="text/plain",
                           attachment_url="https://s3/erro.txt")],
    )

    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=settings)

    assert "https://s3/erro.txt" in fd.baixados  # baixou o .txt
    assert visao.chamadas == []  # .txt NÃO vai à visão (é texto puro)
    assert "SCC19070 na rotina CTBA100" in novo.description_text  # o erro entrou na busca
    assert "Erro.txt" in novo.description_text  # rótulo do anexo
    assert len(novo.description_text) < 10000  # log grande foi truncado no teto


async def test_incorporar_imagens_le_pdf_anexo(settings):
    # ADR-037: PDF anexado (log de erro, comprovante de NF) é transcrito e concatenado à busca.
    fd = FakeFreshdesk(anexo_bytes=b"%PDF-1.4 log")
    visao = FakeVisao(texto="SCC19070 no log do PDF, NF 000077191")
    ticket = TicketFreshdesk(
        id=101, subject="Erro", description_text="Segue o log em anexo.",
        priority="baixa", status=2, requester=Requester(name="C"), empresa="E",
        responder_id=None,
        attachments=[Anexo(id=9, name="log.pdf", content_type="application/pdf",
                           attachment_url="https://s3/log.pdf")],
    )

    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=settings)

    assert "https://s3/log.pdf" in fd.baixados  # baixou o PDF
    assert visao.chamadas[0] == (b"%PDF-1.4 log", "application/pdf")  # transcreveu como PDF
    assert "SCC19070 no log do PDF" in novo.description_text
    assert "Segue o log em anexo." in novo.description_text  # descrição original preservada


async def test_incorporar_imagens_inline_do_corpo(settings):
    # ADR-035: print COLADO no e-mail (inline, sem anexo) é lido e concatenado à busca.
    fd = FakeFreshdesk()
    visao = FakeVisao(texto="Invalid object name TMTSS.DBO.SPED050 MATA103")
    ticket = TicketFreshdesk(
        id=101, subject="Exclusão", description_text="Erro ao excluir nota.",
        priority="baixa", status=2, requester=Requester(name="C"), empresa="E",
        responder_id=None,
        imagens_inline=["https://attachment.freshdesk.com/inline/attachment?token=JWT"],
    )

    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=settings)

    assert fd.baixados == ["https://attachment.freshdesk.com/inline/attachment?token=JWT"]
    assert visao.chamadas[0] == (b"INLINE-IMG", "image/png")  # baixou inline + content_type
    assert "SPED050" in novo.description_text and "MATA103" in novo.description_text
    assert "Erro ao excluir nota." in novo.description_text  # descrição original preservada


async def test_incorporar_imagens_ilegivel_mantem_ticket(settings):
    fd = FakeFreshdesk()
    visao = FakeVisao(texto="")  # imagem sem texto útil -> vazio

    ticket = _ticket_com_imagem(descricao="Segue print.")
    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=settings)

    assert visao.chamadas  # tentou transcrever
    assert novo.description_text == "Segue print."  # inalterado (nada a concatenar)


async def test_incorporar_imagens_falha_download_best_effort(settings):
    fd = FakeFreshdesk(erro_anexo=RuntimeError("S3 timeout"))
    visao = FakeVisao(texto="não deveria chegar aqui")

    ticket = _ticket_com_imagem(descricao="Segue print.")
    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=settings)

    assert fd.baixados  # tentou baixar
    assert visao.chamadas == []  # falhou antes de transcrever
    assert novo.description_text == "Segue print."  # best-effort: não derruba, mantém ticket


async def test_incorporar_imagens_sem_anexo_inalterado(settings):
    fd = FakeFreshdesk()
    visao = FakeVisao(texto="x")

    novo = await _incorporar_imagens(_ticket(), freshdesk=fd, visao=visao, settings=settings)

    assert fd.baixados == [] and visao.chamadas == []  # nada a fazer
    assert novo.description_text == _ticket().description_text


async def test_incorporar_imagens_flag_desligada_nao_le(settings):
    fd = FakeFreshdesk()
    visao = FakeVisao(texto="SCC19070")
    off = settings.model_copy(update={"leitura_imagens_ativa": False})

    ticket = _ticket_com_imagem()
    novo = await _incorporar_imagens(ticket, freshdesk=fd, visao=visao, settings=off)

    assert fd.baixados == [] and visao.chamadas == []
    assert novo.description_text == ticket.description_text


async def test_processar_com_imagem_texto_entra_na_query_do_rag(settings):
    # Prova de ponta: o texto transcrito da imagem vira parte da query do RAG.
    fd = FakeFreshdesk(ticket=_ticket_com_imagem(descricao="Segue print."))
    wa = FakeWhatsApp()
    rag = FakeRag([])

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=rag,
        claude=FakeClaude(resposta=_resposta(encontrou=False, confianca="baixa")),
        whatsapp=wa,
        visao=FakeVisao(texto="SCC19070 ao gravar a NF"),
    )

    assert rag.ultima_query is not None
    assert "SCC19070 ao gravar a NF" in rag.ultima_query  # transcrição na busca


async def test_processar_sem_visao_fluxo_inalterado(settings):
    # visao=None (ou anexo) → pipeline segue como antes, sem baixar nada.
    fd = FakeFreshdesk(ticket=_ticket_com_imagem())
    wa = FakeWhatsApp()

    await processar(
        101,
        settings=settings,
        idempotencia=FakeIdempotencia(),
        freshdesk=fd,
        rag_service=FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)]),
        claude=FakeClaude(resposta=_resposta(True, "alta")),
        whatsapp=wa,
        # sem visao
    )

    assert fd.baixados == []  # sem VisaoClient, não baixa anexos
    assert "🤖 Rascunho gerado por IA" in fd.notas[0][1]


# --- reformulação de query antes do RAG (ADR-024) --------------------------
# A busca é a UNIÃO do texto limpo com a intenção reformulada (a documentação responde
# melhor à intenção; o chamado anterior, ao texto cru). A query reformulada alimenta SÓ o
# embedding — o `gerar_resposta` recebe o problema ÍNTEGRO. Best-effort: falhar volta ao
# texto limpo, e a união colapsa numa busca só.


async def test_uniao_busca_texto_limpo_e_intencao_mas_responde_com_o_problema(settings):
    rag = FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)])
    claude = FakeClaude(resposta=_resposta(True, "alta"), query="Erro SCC19070 ao lançar NF")

    insp = await inspecionar(_ticket(), settings=settings, rag_service=rag, claude=claude)

    # buscou com AS DUAS: o texto limpo (traz chamados) e a intenção (traz docs)
    assert rag.queries == [insp.problema, "Erro SCC19070 ao lançar NF"]
    assert insp.query == "Erro SCC19070 ao lançar NF"  # a interface mostra a intenção
    # o Claude gerou a resposta a partir do problema íntegro, não da query
    assert claude.problemas_recebidos == [insp.problema]
    assert insp.problema != insp.query
    assert "MV_ATFMOED" in insp.problema


async def test_reformulacao_desligada_busca_so_o_texto_limpo(settings):
    off = settings.model_copy(update={"reformular_query_ativa": False})
    rag = FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)])
    claude = FakeClaude(resposta=_resposta(True, "alta"), query="NÃO DEVERIA SER USADA")

    insp = await inspecionar(_ticket(), settings=off, rag_service=rag, claude=claude)

    assert claude.reformulacoes == []  # nem chamou o modelo
    assert rag.queries == [insp.problema]  # união colapsa: só o texto limpo
    assert insp.query == insp.problema


async def test_reformulacao_que_falha_cai_no_texto_limpo_e_nao_derruba_o_chamado(settings):
    rag = FakeRag([Similar(1, "p", "s", "Empresa A", 0.1)])
    claude = FakeClaude(resposta=_resposta(True, "alta"), erro_query=RuntimeError("api caiu"))

    insp = await inspecionar(_ticket(), settings=settings, rag_service=rag, claude=claude)

    assert rag.queries == [insp.problema]  # best-effort: comportamento pré-ADR-024
    assert insp.query == insp.problema
    assert insp.decisao is Decisao.RESOLVIDO  # o chamado seguiu normalmente
