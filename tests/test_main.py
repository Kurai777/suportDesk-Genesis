"""Testes do Módulo 7 — main.py (endpoint do webhook).

Sem chamada real: o pipeline é substituído por um fake; o segredo do webhook é
validado. Usa TestClient (roda o lifespan, mas os clientes só são construídos, não
fazem rede).
"""

from fastapi.testclient import TestClient

from app import main
from app.main import _secret_valido


def test_secret_valido():
    assert _secret_valido("abc", "abc") is True
    assert _secret_valido("abc", "xyz") is False
    assert _secret_valido("", "") is False  # segredo vazio nunca é válido
    assert _secret_valido("abc", "") is False


def test_webhook_aceita_com_segredo_valido_e_agenda_pipeline(settings, monkeypatch):
    chamadas = []

    async def _fake_pipeline(app_, ticket_id):
        chamadas.append(ticket_id)

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_rodar_pipeline", _fake_pipeline)

    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/freshdesk",
            json={"ticket_id": 101},
            headers={"X-Webhook-Secret": settings.freshdesk_webhook_secret},
        )

    assert resp.status_code == 200
    assert resp.json()["ticket_id"] == 101
    assert chamadas == [101]  # pipeline foi agendado com o ticket certo


def test_webhook_rejeita_segredo_invalido(settings, monkeypatch):
    chamadas = []

    async def _fake_pipeline(app_, ticket_id):
        chamadas.append(ticket_id)

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_rodar_pipeline", _fake_pipeline)

    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/freshdesk",
            json={"ticket_id": 101},
            headers={"X-Webhook-Secret": "errado"},
        )

    assert resp.status_code == 401
    assert chamadas == []  # pipeline NÃO foi agendado


# --- webhook de ENTRADA do WhatsApp (relay de token, ADR-026) --------------


def _evento_wa(texto="987654", from_me=False):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {
                "remoteJid": "120363018941234567@g.us",
                "fromMe": from_me,
                "id": "M1",
                "participant": "5511988887777@s.whatsapp.net",
            },
            "pushName": "Responsavel",
            "message": {"conversation": texto},
        },
    }


def _settings_wa(settings):
    return settings.model_copy(update={"whatsapp_webhook_secret": "seg-wa"})


def test_whatsapp_webhook_registra_com_segredo_no_header(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings_wa(settings))
    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json=_evento_wa("o token é 987654"),
            headers={"X-Webhook-Secret": "seg-wa"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "recebido"
        recentes = main.app.state.whatsapp_inbox.recentes()
        assert len(recentes) == 1
        assert recentes[0].texto == "o token é 987654"
        assert recentes[0].remetente_nome == "Responsavel"


def test_whatsapp_webhook_aceita_segredo_na_query(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings_wa(settings))
    with TestClient(main.app) as client:
        resp = client.post("/webhook/whatsapp?secret=seg-wa", json=_evento_wa())
        assert resp.status_code == 200
        assert resp.json()["status"] == "recebido"


def test_whatsapp_webhook_rejeita_segredo_invalido(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings_wa(settings))
    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/whatsapp", json=_evento_wa(), headers={"X-Webhook-Secret": "errado"}
        )
        assert resp.status_code == 401
        assert main.app.state.whatsapp_inbox.recentes() == []


def test_whatsapp_webhook_ignora_propria_mensagem(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings_wa(settings))
    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json=_evento_wa(from_me=True),
            headers={"X-Webhook-Secret": "seg-wa"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignorado"
        assert main.app.state.whatsapp_inbox.recentes() == []


def test_whatsapp_webhook_ignora_evento_nao_mensagem(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings_wa(settings))
    with TestClient(main.app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json={"event": "connection.update", "data": {}},
            headers={"X-Webhook-Secret": "seg-wa"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignorado"
