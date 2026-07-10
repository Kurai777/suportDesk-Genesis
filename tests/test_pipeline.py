"""Testes do Módulo 7 — pipeline.py.

Todos os clientes são falsos/injetados — nenhuma chamada real. Cobre: a função de
decisão (todos os caminhos de confiança), idempotência (reentrega ignorada), o fluxo
resolvido, o fluxo escalar e o fallback quando o miolo falha.
"""

import pytest

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


def _resposta(encontrou=True, confianca="alta", pedido_operacional=False, cliente=None):
    return RespostaIA(
        resposta_cliente=cliente or "Atualize a taxa da moeda 3 e reprocesse a NF.",
        encontrou_solucao=encontrou,
        confianca=confianca,
        resumo_para_responsavel="Moeda 3 do Ativo Fixo sem taxa do dia.",
        urgencia="alta",
        pedido_operacional=pedido_operacional,
    )


def _resultado(encontrou, confianca):
    return ResultadoChamado(ticket_id=1, empresa="X", resposta=_resposta(encontrou, confianca))


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
    assert decidir(_resultado(encontrou, confianca), minimo) is esperado


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
    assert insp.decisao is Decisao.RESOLVIDO
    assert "🌐 Rascunho gerado a partir de BUSCA WEB" in insp.nota


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
    acolhe = "Seu chamado está sendo analisado pelo nosso time e retornaremos em breve."
    insp = await inspecionar(
        _ticket(),
        settings=settings,
        rag_service=FakeRag([]),
        claude=FakeClaude(
            resposta=_resposta(encontrou=False, confianca="baixa", cliente=acolhe)
        ),
    )

    assert insp.decisao is Decisao.ESCALAR
    # Nota (TIME): verdade técnica crua...
    assert "⚠️ IA não encontrou solução na base" in insp.nota
    assert "Requer análise manual" in insp.nota
    # ...e o rascunho de ACOLHIMENTO para o agente enviar ao cliente.
    assert acolhe in insp.nota
    assert insp.resposta.resposta_cliente == acolhe


async def test_pedido_operacional_acolhe_escala_e_nao_vai_a_web(settings):
    web = FakeBuscaWeb(trechos=["não deveria ser usado"])
    acolhe = "Olá! Vamos providenciar o cadastro e retornamos assim que concluído."
    insp = await inspecionar(
        _ticket(),
        settings=_settings_web_on(settings),  # web LIGADA, mas pedido operacional não usa
        rag_service=FakeRag([]),
        claude=FakeClaude(
            resposta=_resposta(
                encontrou=False, confianca="baixa", pedido_operacional=True, cliente=acolhe
            )
        ),
        busca_web=web,
    )

    assert insp.decisao is Decisao.ESCALAR
    assert web.chamadas == []  # pedido operacional é execução humana → não busca web
    assert insp.via_web is False
    assert acolhe in insp.nota  # acolhimento para o agente enviar


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
