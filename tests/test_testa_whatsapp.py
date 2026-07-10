"""Testes do script de teste manual do WhatsApp (scripts/testa_whatsapp.py).

Envio mockado com respx — NENHUM envio real, nenhum custo. Cobre: detecção de config
incompleta, sucesso (captura a resposta da Evolution), falha HTTP (mostra o corpo do erro),
falha de rede (sem resposta) e a normalização do número no envio.
"""

import json

import httpx
import respx

from scripts.testa_whatsapp import config_incompleta, enviar_teste


def _url(settings) -> str:
    return f"{settings.whatsapp_api_url}/message/sendText/{settings.whatsapp_instance}"


def _cfg_real(settings):
    """Settings com envio real ligado (dry_run off) e Evolution preenchida."""
    return settings.model_copy(update={"whatsapp_dry_run": False})


# --- config_incompleta -----------------------------------------------------


def test_config_completa_nao_reporta_faltas(settings):
    assert config_incompleta(_cfg_real(settings)) == []


def test_config_incompleta_lista_variaveis_vazias(settings):
    cfg = settings.model_copy(
        update={"whatsapp_api_url": "", "whatsapp_instance": "  ", "whatsapp_api_key": ""}
    )
    faltando = config_incompleta(cfg)
    assert faltando == ["WHATSAPP_API_URL", "WHATSAPP_INSTANCE", "WHATSAPP_API_KEY"]


# --- enviar_teste (WhatsAppClient real, HTTP mockado) ----------------------


@respx.mock
async def test_enviar_teste_sucesso_captura_resposta(settings):
    cfg = _cfg_real(settings)
    respx.post(_url(cfg)).mock(
        return_value=httpx.Response(201, json={"key": {"id": "MSG123"}})
    )

    sucesso, detalhe = await enviar_teste(cfg, "11999999999", "oi")

    assert sucesso is True
    assert "HTTP 201" in detalhe
    assert "MSG123" in detalhe  # o corpo da Evolution aparece no detalhe


@respx.mock
async def test_enviar_teste_normaliza_numero_no_envio(settings):
    cfg = _cfg_real(settings)
    route = respx.post(_url(cfg)).mock(return_value=httpx.Response(201, json={}))

    await enviar_teste(cfg, "(11) 99999-9999", "oi")

    assert route.called
    enviado = route.calls.last.request
    assert json.loads(enviado.content)["number"] == "5511999999999"  # DDI 55 + só dígitos


@respx.mock
async def test_enviar_teste_falha_http_mostra_corpo_do_erro(settings):
    cfg = _cfg_real(settings)
    respx.post(_url(cfg)).mock(
        return_value=httpx.Response(400, json={"message": "instance not connected"})
    )

    sucesso, detalhe = await enviar_teste(cfg, "11999999999", "oi")

    assert sucesso is False
    assert "HTTP 400" in detalhe
    assert "instance not connected" in detalhe  # diagnóstico visível para o operador


@respx.mock
async def test_enviar_teste_falha_de_rede_sem_resposta(settings):
    cfg = _cfg_real(settings)
    respx.post(_url(cfg)).mock(side_effect=httpx.ConnectError("conexão recusada"))

    sucesso, detalhe = await enviar_teste(cfg, "11999999999", "oi")

    assert sucesso is False
    assert "sem resposta" in detalhe.lower()
    assert "WHATSAPP_API_URL" in detalhe  # aponta o que verificar
