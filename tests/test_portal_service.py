"""Testes do PortalService (ADR-026) — busca no Portal → pares problema→solução.

Cliente do Portal e provedor de sessão são fakes — nenhuma chamada real.
"""

import httpx

from app.portal_service import PortalService
from app.portal_totvs import SessaoPortal, TicketPortal

SESSAO = SessaoPortal(token="jwt-1", user_id=1, customer_code="99034")


def _ticket(tid, subject="Erro X", produto="Protheus", modulo="SIGAFAT"):
    return TicketPortal(
        ticket_id=tid, subject=subject, description="detalhe", status="closed",
        produto=produto, modulo=modulo,
    )


class FakePortal:
    def __init__(self, tickets, solucoes, erro_status=None):
        self._tickets = tickets
        self._solucoes = solucoes
        self._erro_status = erro_status
        self.buscas = 0

    async def buscar_tickets(self, sessao, *, keywords="", **kw):
        self.buscas += 1
        if self._erro_status and self.buscas == 1:
            req = httpx.Request("POST", "http://x/get-tickets")
            raise httpx.HTTPStatusError(
                "erro", request=req, response=httpx.Response(self._erro_status, request=req)
            )
        return self._tickets, False

    async def solucao(self, sessao, ticket_id):
        return self._solucoes.get(ticket_id, "")


def _prov(*sessoes):
    """Provedor que devolve as sessões em ordem (uma por chamada)."""
    fila = list(sessoes)

    async def prov():
        return fila.pop(0) if fila else None

    return prov


async def test_buscar_monta_pares_e_pula_sem_solucao():
    fake = FakePortal(
        [_ticket(10), _ticket(11), _ticket(12)],
        {10: "<p>Atualize os Binários.</p>", 11: "", 12: "Faça Y."},  # 11 sem solução
    )
    svc = PortalService(fake, _prov(SESSAO))

    pares = await svc.buscar("SPED F800")

    assert [p.ticket_id for p in pares] == [10, 12]  # 11 (sem solução) foi pulado
    assert all(p.fonte == "portal_totvs" for p in pares)
    assert "Atualize os Binários." in pares[0].solucao  # HTML limpo pelo client
    assert "#10" in pares[0].titulo and "Protheus/SIGAFAT" in pares[0].titulo
    assert pares[0].empresa is None  # empresa-origem não vai ao texto


async def test_top_k_limita():
    fake = FakePortal([_ticket(i) for i in range(10)], {i: "sol" for i in range(10)})
    svc = PortalService(fake, _prov(SESSAO), top_k=2)
    pares = await svc.buscar("x")
    assert [p.ticket_id for p in pares] == [0, 1]


async def test_sem_sessao_retorna_vazio():
    fake = FakePortal([_ticket(1)], {1: "sol"})
    svc = PortalService(fake, _prov(None))  # provedor não deu sessão
    assert await svc.buscar("x") == []
    assert fake.buscas == 0  # nem tentou buscar


async def test_keywords_vazia_nao_busca():
    fake = FakePortal([_ticket(1)], {1: "sol"})
    svc = PortalService(fake, _prov(SESSAO))
    assert await svc.buscar("   ") == []
    assert fake.buscas == 0


async def test_token_expirado_renova_e_retenta():
    fake = FakePortal([_ticket(5)], {5: "sol"}, erro_status=401)
    sessao2 = SessaoPortal(token="jwt-2", user_id=1, customer_code="99034")
    svc = PortalService(fake, _prov(SESSAO, sessao2))  # 1ª sessão, depois a renovada

    pares = await svc.buscar("x")

    assert [p.ticket_id for p in pares] == [5]  # renovou e conseguiu
    assert fake.buscas == 2  # 1ª deu 401, 2ª (com token novo) funcionou


async def test_erro_nao_401_vira_vazio_best_effort():
    fake = FakePortal([_ticket(1)], {1: "sol"}, erro_status=500)
    svc = PortalService(fake, _prov(SESSAO))
    assert await svc.buscar("x") == []  # 500 não é renovável → best-effort devolve []
