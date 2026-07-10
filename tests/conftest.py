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

# ⚠️ Os testes de integração fazem DELETE FROM conhecimento/chamado_processado.
# Use SEMPRE um banco DEDICADO a testes (nunca o banco da aplicação), senão a base
# ingerida (paga em embeddings Voyage) é apagada a cada rodada de pytest. Por isso o
# default e o exemplo apontam para '..._test'. Sobrescreva com TEST_DATABASE_URL para
# ajustar host/porta (ex.: quando a 5432 já está ocupada).
_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://totvs:totvs@localhost:5432/suporte_totvs_test"
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
    # `_env_file=None`: NÃO lê o .env do desenvolvedor. As flags de feature (busca_web_ativa,
    # reformular_query_ativa, leitura_imagens_ativa, interface_teste_ativa) não estão nos kwargs;
    # sem isto, o pydantic-settings as preencheria a partir do .env local, tornando a suíte
    # dependente do ambiente (ex.: ligar BUSCA_WEB_ATIVA=true no .env quebraria testes de flag
    # desligada). Cada teste que precisa de uma flag ligada usa model_copy explicitamente.
    return Settings(**_SETTINGS_KWARGS, _env_file=None)  # type: ignore[call-arg]
