"""Testes do Módulo 4 — scripts/ingest_tickets.py.

Freshdesk (tickets + conversations) é mockado com respx; Voyage e o repositório
são fakes — nenhuma chamada real.
"""

import httpx
import respx

from app.freshdesk import FreshdeskClient
from scripts.ingest_tickets import (
    carregar_processados,
    extrair_solucao,
    ingerir,
    marcar_processado,
)

BASE = "https://genesis.freshdesk.com/api/v2"


# --- heurística da solução (função pura) -----------------------------------


def test_extrair_solucao_pega_ultima_resposta_publica_do_agente():
    conversas = [
        {"private": True, "incoming": False, "body_text": "nota interna"},
        {"private": False, "incoming": True, "body_text": "cliente reclama"},
        {"private": False, "incoming": False, "body_text": "primeira resposta do agente"},
        {"private": False, "incoming": False, "body_text": "resposta final do agente"},
    ]
    assert extrair_solucao(conversas) == "resposta final do agente"


def test_extrair_solucao_sem_resposta_publica_retorna_none():
    conversas = [
        {"private": True, "incoming": False, "body_text": "só nota interna"},
        {"private": False, "incoming": True, "body_text": "só o cliente falou"},
    ]
    assert extrair_solucao(conversas) is None


# --- checkpoint resumível --------------------------------------------------


def test_checkpoint_carrega_e_marca(tmp_path):
    caminho = tmp_path / "state.txt"
    assert carregar_processados(caminho) == set()
    marcar_processado(caminho, 1)
    marcar_processado(caminho, 42)
    assert carregar_processados(caminho) == {1, 42}


# --- núcleo da ingestão ----------------------------------------------------


class FakeVoyage:
    async def embed_document(self, textos: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in textos]


class FakeRepo:
    def __init__(self) -> None:
        self.inseridos: list[dict] = []

    async def inserir(self, *, ticket_id, empresa, problema, solucao, embedding) -> None:
        self.inseridos.append(
            {
                "ticket_id": ticket_id,
                "empresa": empresa,
                "problema": problema,
                "solucao": solucao,
                "embedding": embedding,
            }
        )


def _montar_rotas_freshdesk() -> dict:
    respx.get(f"{BASE}/tickets", params={"page": "1", "per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "status": 4},  # resolvido, com solução
                {"id": 2, "status": 2},  # aberto -> ignorado
                {"id": 3, "status": 5},  # fechado, sem solução pública
            ],
        )
    )
    respx.get(f"{BASE}/tickets", params={"page": "2", "per_page": "100"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    t1 = respx.get(f"{BASE}/tickets/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "status": 4,
                "priority": 3,
                "description_text": "Erro no lançamento da NF. Log SCC19070 / MV_ATFMOED.",
                "requester": {"name": "Cliente A"},
                "company": {"name": "Empresa A"},
            },
        )
    )
    t3 = respx.get(f"{BASE}/tickets/3").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 3,
                "status": 5,
                "priority": 2,
                "description_text": "Outro problema qualquer.",
                "requester": {"name": "Cliente B"},
                "company": None,
            },
        )
    )
    respx.get(f"{BASE}/tickets/1/conversations").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"private": False, "incoming": True, "body_text": "cliente descreve"},
                {"private": False, "incoming": False, "body_text": "Atualize a taxa da moeda 3."},
            ],
        )
    )
    respx.get(f"{BASE}/tickets/3/conversations").mock(
        return_value=httpx.Response(
            200,
            json=[{"private": True, "incoming": False, "body_text": "só nota interna"}],
        )
    )
    return {"t1": t1, "t3": t3}


@respx.mock
async def test_ingerir_ingere_resolvidos_com_solucao(settings):
    _montar_rotas_freshdesk()
    voyage = FakeVoyage()
    repo = FakeRepo()
    processados: set[int] = set()
    marcados: list[int] = []

    async with FreshdeskClient(settings) as fd:
        resumo = await ingerir(fd, voyage, repo, processados, marcados.append)

    assert resumo.ingeridos == 1
    assert resumo.sem_solucao == 1  # ticket 3 sem resposta pública de agente
    assert len(repo.inseridos) == 1
    inserido = repo.inseridos[0]
    assert inserido["ticket_id"] == 1
    assert inserido["empresa"] == "Empresa A"
    assert inserido["solucao"] == "Atualize a taxa da moeda 3."
    assert "SCC19070" in inserido["problema"]
    assert len(inserido["embedding"]) == 1024
    # Só os resolvidos são marcados (o aberto id=2 não entra no checkpoint).
    assert set(marcados) == {1, 3}


@respx.mock
async def test_ingerir_pula_ja_processados_sem_refetch(settings):
    rotas = _montar_rotas_freshdesk()
    voyage = FakeVoyage()
    repo = FakeRepo()
    processados = {1}  # ticket 1 já foi ingerido antes
    marcados: list[int] = []

    async with FreshdeskClient(settings) as fd:
        resumo = await ingerir(fd, voyage, repo, processados, marcados.append)

    assert resumo.ja_processados == 1
    assert resumo.ingeridos == 0  # ticket 1 pulado; ticket 3 sem solução
    assert not rotas["t1"].called  # resumível: não re-busca o já processado
    assert repo.inseridos == []
