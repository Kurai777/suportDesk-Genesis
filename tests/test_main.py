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
