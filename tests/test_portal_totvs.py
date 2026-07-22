"""Testes do PortalTotvsClient (ADR-026) — API interna do Portal TOTVS.

Tudo mockado com respx — NENHUMA chamada real. O token é fake (nunca sai um segredo real
em teste). Cobre: montagem do corpo (token/identidade/filtros), parse dos tickets, extração
da solução (só comentários do AGENTE), limpeza de HTML e retry em 429/5xx.
"""

import json

import httpx
import respx

from app.portal_totvs import (
    ComentarioPortal,
    PortalTotvsClient,
    SessaoPortal,
    TicketPortal,
    _html_para_texto,
)

BASE = "https://ti-services.totvs.com.br/customer-portal-backend"
GET_TICKETS = f"{BASE}/help-center/tickets/get-tickets"
GET_COMMENTS = f"{BASE}/help-center/tickets/get-comments"

SESSAO = SessaoPortal(token="jwt-de-teste", user_id=374391156891, customer_code="99034")

_TICKET_API = {
    "ticketId": 7821159,
    "subject": "Erro ao transmitir NF",
    "description": "<p>Boa tarde,<br>segue erro de conexão.</p>",
    "status": "closed",
    "catalogV3": {"product": "Protheus", "module": "SIGAFAT", "routineGrouper": "MATA010"},
    "organization": {"id": 9, "name": "COLEGIO ARBOS LTDA"},
    "createdAt": "2016-01-01T10:00:00Z",
    "updatedAt": "2016-01-02T10:00:00Z",
}


# --- limpeza de HTML (pura) ------------------------------------------------


def test_html_para_texto_remove_tags_e_preserva_quebras():
    html = '<div class="zd-comment">Bom dia,<br><br>Atualize os <b>Binários</b>.<br>Fim.</div>'
    txt = _html_para_texto(html)
    assert "zd-comment" not in txt and "<b>" not in txt
    assert "Atualize os Binários." in txt
    assert txt.splitlines()[0] == "Bom dia,"  # <br> virou quebra de linha


def test_html_para_texto_texto_puro_e_vazio():
    assert _html_para_texto("apenas texto") == "apenas texto"
    assert _html_para_texto("") == ""


# --- buscar_tickets --------------------------------------------------------


@respx.mock
async def test_buscar_tickets_monta_corpo_e_parseia(settings):
    route = respx.post(GET_TICKETS).mock(
        return_value=httpx.Response(
            200, json={"tickets": [_TICKET_API], "pagination": {"count": 1, "hasNext": True}}
        )
    )
    async with PortalTotvsClient(settings) as portal:
        tickets, tem_proximo = await portal.buscar_tickets(
            SESSAO, keywords="SPED", produtos=["Protheus"], pagina=2
        )

    assert route.called
    # token vai no CORPO, não em header (regra da API descoberta no spike):
    req = route.calls.last.request
    assert "authorization" not in req.headers
    corpo = json.loads(req.content)
    assert corpo["token"] == "jwt-de-teste"
    assert corpo["userId"] == 374391156891
    assert corpo["customerCode"] == "99034"
    assert corpo["keywords"] == "SPED"
    assert corpo["catalogV3ProductTags"] == ["Protheus"]
    assert corpo["pagination"] == {"page": 2}
    assert str(req.url).endswith("language=pt")

    # parse:
    assert tem_proximo is True
    t = tickets[0]
    assert isinstance(t, TicketPortal)
    assert t.ticket_id == 7821159
    assert t.produto == "Protheus" and t.modulo == "SIGAFAT"
    assert t.organizacao == "COLEGIO ARBOS LTDA"
    assert "segue erro de conexão." in t.description  # HTML limpo
    assert "<p>" not in t.description


@respx.mock
async def test_buscar_tickets_hasnext_false_e_ignora_sem_id(settings):
    respx.post(GET_TICKETS).mock(
        return_value=httpx.Response(
            200,
            json={
                "tickets": [_TICKET_API, {"subject": "sem id — descartado"}],
                "pagination": {"hasNext": False},
            },
        )
    )
    async with PortalTotvsClient(settings) as portal:
        tickets, tem_proximo = await portal.buscar_tickets(SESSAO)

    assert tem_proximo is False
    assert [t.ticket_id for t in tickets] == [7821159]  # o sem ticketId foi ignorado


# --- comentarios / solucao -------------------------------------------------


_COMMENTS_API = {
    "comments": [
        {"author": "Cliente", "body": "<p>Tenho um erro X</p>", "isEndUser": True,
         "isPrivate": False},
        {"author": "Nota interna", "body": "checar release", "isEndUser": False,
         "isPrivate": True},
        {"author": "Agente TOTVS", "body": "Atualize os Binários.<br>Faça o upgrade.",
         "isEndUser": False, "isPrivate": False},
    ]
}


@respx.mock
async def test_comentarios_parseia_flags_e_html(settings):
    respx.post(GET_COMMENTS).mock(return_value=httpx.Response(200, json=_COMMENTS_API))
    async with PortalTotvsClient(settings) as portal:
        coments = await portal.comentarios(SESSAO, 7821159)

    assert [c.is_cliente for c in coments] == [True, False, False]
    assert [c.is_privado for c in coments] == [False, True, False]
    assert isinstance(coments[0], ComentarioPortal)
    assert coments[2].eh_solucao is True  # agente, público
    assert coments[0].eh_solucao is False  # cliente
    assert coments[1].eh_solucao is False  # privado


@respx.mock
async def test_solucao_junta_so_agente_publico(settings):
    route = respx.post(GET_COMMENTS).mock(
        return_value=httpx.Response(200, json=_COMMENTS_API)
    )
    async with PortalTotvsClient(settings) as portal:
        solucao = await portal.solucao(SESSAO, 7821159)

    # corpo do get-comments carrega token + ticketId + customerCode:
    corpo = json.loads(route.calls.last.request.content)
    assert corpo["ticketId"] == 7821159
    assert corpo["token"] == "jwt-de-teste"
    assert corpo["customerCode"] == "99034"

    # só a resposta do AGENTE (pública) entra; cliente e nota privada ficam de fora:
    assert "Atualize os Binários." in solucao
    assert "Faça o upgrade." in solucao
    assert "Tenho um erro X" not in solucao  # comentário do cliente
    assert "checar release" not in solucao  # nota privada


@respx.mock
async def test_solucao_vazia_quando_so_cliente(settings):
    respx.post(GET_COMMENTS).mock(
        return_value=httpx.Response(
            200,
            json={"comments": [{"body": "só o cliente falou", "isEndUser": True,
                                "isPrivate": False}]},
        )
    )
    async with PortalTotvsClient(settings) as portal:
        assert await portal.solucao(SESSAO, 1) == ""


# --- retry -----------------------------------------------------------------


@respx.mock
async def test_retry_em_429(settings):
    route = respx.post(GET_TICKETS).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"tickets": [], "pagination": {"hasNext": False}}),
        ]
    )
    async with PortalTotvsClient(settings) as portal:
        tickets, _ = await portal.buscar_tickets(SESSAO)

    assert route.call_count == 2  # re-tentou após o 429
    assert tickets == []
