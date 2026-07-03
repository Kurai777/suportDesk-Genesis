"""Testes do Módulo 7 — pipeline.py.

Todos os clientes são falsos/injetados — nenhuma chamada real. Cobre: a função de
decisão (todos os caminhos de confiança), idempotência (reentrega ignorada), o fluxo
resolvido, o fluxo escalar e o fallback quando o miolo falha.
"""

import pytest

from app.models import Requester, RespostaIA, ResultadoChamado, TicketFreshdesk
from app.pipeline import Decisao, decidir, processar
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
    def __init__(self, ticket=None, erro_get=None):
        self._ticket = ticket
        self._erro_get = erro_get
        self.notas = []
        self.atribuicoes = []

    async def get_ticket(self, ticket_id):
        if self._erro_get is not None:
            raise self._erro_get
        return self._ticket

    async def criar_nota_interna(self, ticket_id, body):
        self.notas.append((ticket_id, body))

    async def atribuir(self, ticket_id, responder_id):
        self.atribuicoes.append((ticket_id, responder_id))


class FakeRag:
    def __init__(self, pares=None):
        self._pares = pares or []

    async def buscar(self, problema, k=5):
        return self._pares


class FakeClaude:
    def __init__(self, resposta=None, erro=None):
        self._resposta = resposta
        self._erro = erro

    async def gerar_resposta(self, problema, pares):
        if self._erro is not None:
            raise self._erro
        return self._resposta


class FakeWhatsApp:
    def __init__(self):
        self.enviados = []

    async def enviar(self, numero, texto):
        self.enviados.append((numero, texto))
        return True


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


def _resposta(encontrou=True, confianca="alta"):
    return RespostaIA(
        resposta_cliente="Atualize a taxa da moeda 3 e reprocesse a NF.",
        encontrou_solucao=encontrou,
        confianca=confianca,
        resumo_para_responsavel="Moeda 3 do Ativo Fixo sem taxa do dia.",
        urgencia="alta",
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
