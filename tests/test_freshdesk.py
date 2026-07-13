"""Testes do Módulo 3: FreshdeskClient (assíncrono).

Tudo mockado com respx — NENHUMA chamada real (regra: testes não gastam cota paga).
asyncio_mode=auto (pyproject) coleta as funções `async def test_*` sem decorator.
"""

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.freshdesk import FreshdeskClient, _normalizar_host
from app.models import EMPRESA_DESCONHECIDA, TicketFreshdesk

BASE = "https://genesis.freshdesk.com/api/v2"

TICKET_BASE = {
    "id": 101,
    "subject": "Erro no lançamento da NF",
    "description_text": "Log SCC19070 ... verifique o parâmetro MV_ATFMOED.",
    "priority": 3,
    "status": 2,
    "responder_id": 55,
    "requester": {"name": "Fulano de Tal", "email": "fulano@cliente.com"},
    "stats": {},
}


def _settings() -> Settings:
    return Settings(
        freshdesk_domain="genesis",
        freshdesk_api_key="fd-key",
        freshdesk_webhook_secret="whsec",
        anthropic_api_key="sk-ant",
        voyage_api_key="voy",
        database_url="postgresql://totvs:totvs@localhost:5432/suporte_totvs",
        whatsapp_api_url="http://localhost:8080",
        whatsapp_api_key="wa-key",
        whatsapp_instance="genesis-instancia",
        whatsapp_responsavel_default="5511999999999",
    )


# --- get_ticket ------------------------------------------------------------


@respx.mock
async def test_get_ticket_com_empresa():
    payload = {**TICKET_BASE, "company": {"id": 9, "name": "Cliente Exemplo Ltda"}}
    route = respx.get(f"{BASE}/tickets/101").mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with FreshdeskClient(_settings()) as fd:
        ticket = await fd.get_ticket(101)

    assert route.called
    # Basic Auth aplicado (chave + senha "X").
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")
    assert ticket.empresa == "Cliente Exemplo Ltda"
    assert ticket.priority == "alta"
    assert ticket.requester.email == "fulano@cliente.com"
    assert ticket.responder_id == 55


@respx.mock
async def test_get_ticket_company_nula_usa_fallback():
    payload = {**TICKET_BASE, "company": None}
    respx.get(f"{BASE}/tickets/101").mock(return_value=httpx.Response(200, json=payload))

    async with FreshdeskClient(_settings()) as fd:
        ticket = await fd.get_ticket(101)

    assert ticket.empresa == EMPRESA_DESCONHECIDA


@respx.mock
async def test_get_ticket_retenta_em_429_respeitando_retry_after():
    payload = {**TICKET_BASE, "company": {"name": "Cliente Exemplo Ltda"}}
    route = respx.get(f"{BASE}/tickets/101").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),  # 1ª tentativa: rate limit
            httpx.Response(200, json=payload),  # 2ª tentativa: sucesso
        ]
    )

    async with FreshdeskClient(_settings()) as fd:
        ticket = await fd.get_ticket(101)

    assert ticket.id == 101
    assert route.call_count == 2


# --- criar_nota_interna ----------------------------------------------------


@respx.mock
async def test_criar_nota_interna_envia_body_privado():
    route = respx.post(f"{BASE}/tickets/101/notes").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    async with FreshdeskClient(_settings()) as fd:
        await fd.criar_nota_interna(101, "Rascunho de resposta gerado pela IA.")

    assert route.called
    enviado = json.loads(route.calls.last.request.content)
    assert enviado == {"body": "Rascunho de resposta gerado pela IA.", "private": True}


# --- atribuir --------------------------------------------------------------


@respx.mock
async def test_atribuir_envia_responder_id():
    route = respx.put(f"{BASE}/tickets/101").mock(
        return_value=httpx.Response(200, json={"id": 101, "responder_id": 77})
    )

    async with FreshdeskClient(_settings()) as fd:
        await fd.atribuir(101, 77)

    assert route.called
    enviado = json.loads(route.calls.last.request.content)
    assert enviado == {"responder_id": 77}


# --- baixar_anexo (leitura de imagens, ADR-023) ----------------------------


@respx.mock
async def test_baixar_anexo_busca_url_pre_assinada_sem_auth():
    url = "https://s3.amazonaws.com/freshdesk/att/1/print.png?sig=abc"
    route = respx.get(url).mock(return_value=httpx.Response(200, content=b"\x89PNG-bytes"))

    async with FreshdeskClient(_settings()) as fd:
        dados = await fd.baixar_anexo(url)

    assert dados == b"\x89PNG-bytes"
    assert route.called
    # URL pré-assinada (S3): buscada SEM a auth Basic do Freshdesk.
    assert "authorization" not in route.calls.last.request.headers


def test_from_freshdesk_parseia_anexos_e_filtra_imagens():
    ticket = TicketFreshdesk.from_freshdesk(
        {
            **TICKET_BASE,
            "company": {"name": "Y"},
            "attachments": [
                {"id": 1, "name": "print.png", "content_type": "image/png",
                 "attachment_url": "https://s3/att/1"},
                {"id": 2, "name": "manual.pdf", "content_type": "application/pdf",
                 "attachment_url": "https://s3/att/2"},
            ],
        }
    )
    assert len(ticket.attachments) == 2
    assert [a.id for a in ticket.imagens] == [1]  # só a imagem


def test_from_freshdesk_extrai_imagens_inline_do_corpo():
    # Prints colados no e-mail viram <img src=attachment.freshdesk.com/inline/...> (ADR-035).
    html = (
        "<div>Segue o erro:<br>"
        '<img src="https://attachment.freshdesk.com/inline/attachment?token=AAA&amp;x=1"><br>'
        '<img src="https://attachment.freshdesk.com/inline/attachment?token=BBB">'
        '<img src="https://logo.externo.com/assinatura.png">'  # externo (assinatura) -> ignorado
        "</div>"
    )
    ticket = TicketFreshdesk.from_freshdesk({**TICKET_BASE, "description": html})

    assert ticket.imagens_inline == [
        "https://attachment.freshdesk.com/inline/attachment?token=AAA&x=1",  # &amp; desescapado
        "https://attachment.freshdesk.com/inline/attachment?token=BBB",
    ]


@respx.mock
async def test_baixar_imagem_inline_sem_auth_com_content_type():
    url = "https://attachment.freshdesk.com/inline/attachment?token=JWT"
    respx.get(url).mock(
        return_value=httpx.Response(
            200, content=b"PNGDATA", headers={"content-type": "image/png; x"}
        )
    )

    async with FreshdeskClient(_settings()) as fd:
        dados, tipo = await fd.baixar_imagem(url)

    assert dados == b"PNGDATA"
    assert tipo == "image/png"  # só o mime, sem o "; x"


# --- mapeamento de prioridade (sem HTTP) -----------------------------------


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("genesis", "genesis.freshdesk.com"),
        ("genesis.freshdesk.com", "genesis.freshdesk.com"),
        ("https://genesis.freshdesk.com", "genesis.freshdesk.com"),
        ("https://genesis-consulting.freshdesk.com/", "genesis-consulting.freshdesk.com"),
        ("  genesis  ", "genesis.freshdesk.com"),
    ],
)
def test_normaliza_host_freshdesk(valor, esperado):
    assert _normalizar_host(valor) == esperado


@pytest.mark.parametrize(
    "priority_int,esperado",
    [(1, "baixa"), (2, "media"), (3, "alta"), (4, "urgente"), (99, "media")],
)
def test_mapeamento_prioridade(priority_int, esperado):
    ticket = TicketFreshdesk.from_freshdesk(
        {
            "id": 1,
            "priority": priority_int,
            "status": 2,
            "requester": {"name": "x"},
            "company": {"name": "Y"},
        }
    )
    assert ticket.priority == esperado
