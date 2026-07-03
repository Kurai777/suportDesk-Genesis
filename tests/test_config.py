"""Teste simples do Módulo 1: as configurações carregam e aplicam os defaults.

Não toca em nenhum serviço real (regra: nenhum teste gasta chamada paga).
Passamos os campos obrigatórios explicitamente, simulando a injeção de config.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {
    "freshdesk_domain": "genesis",
    "freshdesk_api_key": "fd-key",
    "freshdesk_webhook_secret": "whsec-123",
    "anthropic_api_key": "sk-ant-test",
    "voyage_api_key": "voy-test",
    "database_url": "postgresql://totvs:totvs@localhost:5432/suporte_totvs",
    "whatsapp_api_url": "http://localhost:8080",
    "whatsapp_api_key": "wa-key",
    "whatsapp_instance": "genesis-instancia",
    "whatsapp_responsavel_default": "5511999999999",
}


def test_settings_aplica_defaults():
    s = Settings(**REQUIRED)

    assert s.claude_model == "claude-haiku-4-5-20251001"
    assert s.voyage_model == "voyage-3"
    assert s.voyage_embedding_dim == 1024
    assert s.confianca_minima == "alta"


def test_settings_le_campos_obrigatorios():
    s = Settings(**REQUIRED)

    assert s.freshdesk_domain == "genesis"
    assert s.database_url.endswith("/suporte_totvs")


def test_settings_falha_sem_campo_obrigatorio(monkeypatch):
    # Isola do ambiente/.env para garantir que o campo faltante realmente falte.
    for chave in REQUIRED:
        monkeypatch.delenv(chave.upper(), raising=False)

    incompleto = {k: v for k, v in REQUIRED.items() if k != "database_url"}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **incompleto)
