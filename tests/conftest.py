"""Fixtures compartilhadas dos testes."""

import asyncio
import os
import sys

import pytest

from app.config import Settings

# No Windows, o psycopg async exige SelectorEventLoop (o ProactorEventLoop padrão não é
# suportado). Setar a política aqui (antes do pytest-asyncio criar os loops) evita o
# warning de deprecação de sobrescrever a fixture event_loop_policy. Em Linux/CI é no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Os testes de integração usam este banco; sobrescreva com TEST_DATABASE_URL para
# apontar para outra porta/host (ex.: quando a 5432 já está ocupada).
_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://totvs:totvs@localhost:5432/suporte_totvs"
)

_SETTINGS_KWARGS = {
    "freshdesk_domain": "genesis",
    "freshdesk_api_key": "fd-key",
    "freshdesk_webhook_secret": "whsec",
    "anthropic_api_key": "sk-ant",
    "voyage_api_key": "voy",
    "database_url": _DATABASE_URL,
    "whatsapp_api_url": "http://localhost:8080",
    "whatsapp_api_key": "wa-key",
    "whatsapp_instance": "genesis-instancia",
    "whatsapp_responsavel_default": "5511999999999",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(**_SETTINGS_KWARGS)
