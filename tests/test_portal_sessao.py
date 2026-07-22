"""Testes do SessaoStore (ADR-026) — grava/lê a sessão do Portal (token) em arquivo."""

from app.portal_sessao import SessaoStore
from app.portal_totvs import SessaoPortal


async def test_grava_e_le_roundtrip(tmp_path):
    store = SessaoStore(str(tmp_path / "s.json"))
    store.gravar(SessaoPortal(token="jwt-x", user_id=42, customer_code="99034"))
    s = await store.ler()
    assert s is not None
    assert s.token == "jwt-x"
    assert s.user_id == 42
    assert s.customer_code == "99034"


async def test_le_inexistente_vira_none(tmp_path):
    store = SessaoStore(str(tmp_path / "nao-existe.json"))
    assert await store.ler() is None


async def test_le_corrompido_vira_none(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ isto nao eh json valido", encoding="utf-8")
    assert await SessaoStore(str(p)).ler() is None
