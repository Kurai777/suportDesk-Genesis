"""Testes do Módulo 6 — WhatsAppClient (Evolution API v2).

Envio mockado com respx — NENHUM envio real. Cobre: sucesso, falha de rede (retorna
False sem levantar), normalização do número, dry-run (não chama HTTP) e a resolução
agente->telefone.
"""

import json

import httpx
import pytest
import respx

from app.whatsapp import WhatsAppClient, normalizar_numero, resolver_destino


def _url(settings) -> str:
    return f"{settings.whatsapp_api_url}/message/sendText/{settings.whatsapp_instance}"


# --- normalização do número (função pura) ----------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("11999999999", "5511999999999"),  # 11 dígitos (celular) -> ganha DDI 55
        ("1133334444", "551133334444"),  # 10 dígitos (fixo) -> ganha DDI 55
        ("5511999999999", "5511999999999"),  # já com DDI (13) -> intacto
        ("(11) 99999-9999", "5511999999999"),  # símbolos + 11 dígitos
        ("+55 11 99999-9999", "5511999999999"),  # DDI + símbolos (13 dígitos) -> intacto
    ],
)
def test_normalizar_numero(entrada, esperado):
    assert normalizar_numero(entrada) == esperado


# --- envio -----------------------------------------------------------------


@respx.mock
async def test_enviar_sucesso(settings):
    cfg = settings.model_copy(update={"whatsapp_dry_run": False})
    route = respx.post(_url(cfg)).mock(
        return_value=httpx.Response(201, json={"key": {"id": "abc"}})
    )

    async with WhatsAppClient(cfg) as wa:
        ok = await wa.enviar("(11) 99999-9999", "Chamado atribuído a você.")

    assert ok is True
    assert route.called
    enviado = json.loads(route.calls.last.request.content)
    assert enviado == {"number": "5511999999999", "text": "Chamado atribuído a você."}
    assert route.calls.last.request.headers["apikey"] == cfg.whatsapp_api_key


@respx.mock
async def test_enviar_falha_de_rede_retorna_false_sem_levantar(settings):
    cfg = settings.model_copy(update={"whatsapp_dry_run": False})
    respx.post(_url(cfg)).mock(side_effect=httpx.ConnectError("sem rede"))

    async with WhatsAppClient(cfg) as wa:
        ok = await wa.enviar("11999999999", "oi")

    assert ok is False  # melhor esforço: não propaga a exceção


@respx.mock
async def test_dry_run_nao_chama_http(settings):
    # settings.whatsapp_dry_run é True (padrão) na fixture.
    route = respx.post(_url(settings)).mock(return_value=httpx.Response(201))

    async with WhatsAppClient(settings) as wa:
        ok = await wa.enviar("11999999999", "mensagem de teste")

    assert ok is True
    assert not route.called  # dry-run só loga, não chama a Evolution


# --- resolução agente -> telefone (config, usada no pipeline) ---------------


def test_telefone_responsavel_usa_mapa_e_fallback(settings):
    cfg = settings.model_copy(
        update={
            "responsaveis": {"67": "5511777777777"},
            "whatsapp_responsavel_default": "5511000000000",
        }
    )

    assert cfg.telefone_responsavel(67) == "5511777777777"  # do mapa
    assert cfg.telefone_responsavel(99) == "5511000000000"  # sem mapeamento -> fallback
    assert cfg.telefone_responsavel(None) == "5511000000000"  # sem agente -> fallback


# --- destino: grupo × telefone (ADR-029) -----------------------------------


_GRUPO = "120363018941234567@g.us"


def test_resolver_destino_preserva_jid_de_grupo():
    # JID de grupo NÃO pode ser normalizado (perderia o @g.us).
    assert resolver_destino(_GRUPO) == _GRUPO
    assert resolver_destino("  " + _GRUPO + "  ") == _GRUPO


def test_resolver_destino_normaliza_telefone():
    assert resolver_destino("(11) 99999-9999") == "5511999999999"


def test_destino_notificacao_prefere_o_grupo(settings):
    cfg = settings.model_copy(
        update={
            "whatsapp_grupo_destino": _GRUPO,
            "responsaveis": {"67": "5511777777777"},
            "whatsapp_responsavel_default": "5511000000000",
        }
    )
    # Com grupo configurado, TODO chamado vai ao grupo — ignora o agente.
    assert cfg.destino_notificacao(67) == _GRUPO
    assert cfg.destino_notificacao(None) == _GRUPO


def test_destino_notificacao_sem_grupo_cai_no_telefone(settings):
    cfg = settings.model_copy(
        update={"whatsapp_grupo_destino": "", "responsaveis": {"67": "5511777777777"}}
    )
    assert cfg.destino_notificacao(67) == "5511777777777"  # modelo antigo preservado


@respx.mock
async def test_enviar_para_grupo_usa_o_jid_intacto(settings):
    cfg = settings.model_copy(update={"whatsapp_dry_run": False})
    route = respx.post(_url(cfg)).mock(return_value=httpx.Response(201, json={}))

    async with WhatsAppClient(cfg) as wa:
        ok = await wa.enviar(_GRUPO, "Feedback do chamado #123.")

    assert ok is True
    enviado = json.loads(route.calls.last.request.content)
    assert enviado["number"] == _GRUPO  # o JID foi enviado sem normalização


@respx.mock
async def test_listar_grupos_parseia_jid_e_nome(settings):
    url = f"{settings.whatsapp_api_url}/group/fetchAllGroups/{settings.whatsapp_instance}"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": _GRUPO, "subject": "Suporte Genesis"},
                {"id": "999@g.us", "subject": ""},  # sem nome -> rótulo padrão
                {"subject": "sem id — descartado"},  # sem id -> ignorado
            ],
        )
    )

    async with WhatsAppClient(settings) as wa:
        grupos = await wa.listar_grupos()

    assert grupos == [
        {"jid": _GRUPO, "nome": "Suporte Genesis"},
        {"jid": "999@g.us", "nome": "(sem nome)"},
    ]
