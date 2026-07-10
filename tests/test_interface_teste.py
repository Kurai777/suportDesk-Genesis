"""Testes da interface de teste local (ADR-019).

Garantem que o caminho de teste NUNCA toca o Freshdesk nem envia WhatsApp: `inspecionar`
(e o helper da rota) não recebem esses clientes. Voyage/Claude são fakes — nenhuma
chamada real. O helper roda contra o banco de TESTE (vazio → recuperação retorna []).
"""

from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException

from app.main import (
    _PAGINA_TESTE,
    _inspecao_do_texto,
    _para_resposta,
    _teste_ativo,
    _ticket_de_teste,
)
from app.models import EMPRESA_DESCONHECIDA, RespostaIA
from app.pipeline import Decisao


@pytest.fixture
async def banco_de_teste(settings):
    """Pula o teste se o Postgres de TESTE não estiver de pé.

    O caminho de teste conecta ao banco por dentro (`_inspecao_do_texto`), então este é um
    teste de INTEGRAÇÃO — mesmo idioma de skip dos demais (`connect_timeout=2` + `pytest.skip`),
    para a suíte não falhar em máquina sem o banco local.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True, connect_timeout=2
        )
    except Exception:
        pytest.skip("Postgres do docker-compose não está de pé (docker compose up -d db)")
    await conn.close()

_CANNED = RespostaIA(
    resposta_cliente="Rascunho de teste.",
    encontrou_solucao=False,
    confianca="baixa",
    resumo_para_responsavel="Resumo de teste.",
    urgencia="media",
    pedido_operacional=False,
)


class FakeVoyageQ:
    async def embed_query(self, texto: str) -> list[float]:
        return [0.1] * 1024


class FakeClaudeCanned:
    def __init__(self, resposta: RespostaIA) -> None:
        self._resposta = resposta

    async def gerar_resposta(self, problema, pares) -> RespostaIA:
        return self._resposta

    async def reformular_query(self, problema) -> str:
        return problema


# --- página e helpers puros ------------------------------------------------


def test_pagina_deixa_claro_que_e_teste():
    assert "Ambiente de teste" in _PAGINA_TESTE
    assert "não escreve no Freshdesk nem envia WhatsApp" in _PAGINA_TESTE
    assert "<textarea" in _PAGINA_TESTE
    assert "Processar" in _PAGINA_TESTE
    assert "/teste/processar" in _PAGINA_TESTE


def test_ticket_de_teste_usa_empresa_ou_default():
    t = _ticket_de_teste("texto do chamado", "ACME")
    assert t.empresa == "ACME"
    assert t.description_text == "texto do chamado"
    assert t.responder_id is None and t.id == 0
    assert _ticket_de_teste("x", None).empresa == EMPRESA_DESCONHECIDA
    assert _ticket_de_teste("x", "   ").empresa == EMPRESA_DESCONHECIDA  # só espaços


def test_teste_ativo_gate():
    def _req(ativo: bool):
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                settings=SimpleNamespace(interface_teste_ativa=ativo)
            ))
        )

    with pytest.raises(HTTPException) as exc:
        _teste_ativo(_req(False))
    assert exc.value.status_code == 404
    assert _teste_ativo(_req(True)).interface_teste_ativa is True


# --- caminho de teste: sem Freshdesk/WhatsApp (integração, banco de teste) --


async def test_inspecao_do_texto_nao_usa_freshdesk_nem_whatsapp(settings, banco_de_teste):
    # `state` NÃO tem freshdesk/whatsapp: se o caminho de teste os usasse, daria
    # AttributeError. Como não usa, roda normalmente. Voyage/Claude fakes = zero rede.
    app = SimpleNamespace(state=SimpleNamespace(
        settings=settings,
        voyage=FakeVoyageQ(),
        claude=FakeClaudeCanned(_CANNED),
        busca_web=None,
    ))

    insp = await _inspecao_do_texto(app, "erro SCC19070 ao lançar a NF", "Cliente ACME")

    assert insp.decisao is Decisao.ESCALAR  # base de teste vazia -> escala
    assert insp.nota and insp.whatsapp
    assert insp.via_web is False
    resp = _para_resposta(insp, "Cliente ACME")
    assert resp.empresa == "Cliente ACME"
    assert resp.decisao == "escalar"
    assert resp.pedido_operacional is False  # exposto na tela (ADR-020)
    assert resp.pares == []  # banco de teste vazio
