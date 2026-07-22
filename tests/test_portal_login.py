"""Testes do PortalLoginProvider (ADR-026).

O fluxo de browser (Playwright) é validado AO VIVO; aqui cobrimos a parte pura: montar a
`SessaoPortal` a partir do corpo capturado do `get-tickets`.
"""

from app.portal_login import _sessao_de_corpo
from app.portal_totvs import SessaoPortal


def test_sessao_de_corpo_monta_sessao():
    body = {
        "token": "jwt-abc",
        "userId": 374391156891,
        "customerCode": "99034",
        "keywords": "",  # campos a mais são ignorados
    }
    s = _sessao_de_corpo(body)
    assert isinstance(s, SessaoPortal)
    assert s.token == "jwt-abc"
    assert s.user_id == 374391156891
    assert s.customer_code == "99034"


def test_sessao_de_corpo_userid_string_e_convertido():
    s = _sessao_de_corpo({"token": "t", "userId": "123", "customerCode": "99034"})
    assert s is not None and s.user_id == 123


def test_sessao_de_corpo_faltando_campo_vira_none():
    assert _sessao_de_corpo({"userId": 1, "customerCode": "9"}) is None  # sem token
    assert _sessao_de_corpo({"token": "t", "customerCode": "9"}) is None  # sem userId
    assert _sessao_de_corpo({"token": "t", "userId": 1}) is None  # sem customerCode
    assert _sessao_de_corpo({}) is None


def test_sessao_de_corpo_userid_invalido_vira_none():
    assert _sessao_de_corpo({"token": "t", "userId": "abc", "customerCode": "9"}) is None
