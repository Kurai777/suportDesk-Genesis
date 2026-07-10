"""Testes do Módulo 4 — scripts/ingest_tickets.py (com limpeza + filtro, ADR-011).

Freshdesk (tickets + conversations) mockado com respx; Voyage e o repositório são
fakes — nenhuma chamada real.
"""

import httpx
import pytest
import respx

from app.freshdesk import FreshdeskClient
from app.texto import limpar_texto
from scripts.ingest_tickets import (
    _motivo_baixo_valor,
    extrair_solucao,
    ingerir,
)

BASE = "https://genesis.freshdesk.com/api/v2"

# Solução válida (longa, com conteúdo técnico) usada no chamado ingerido.
SOLUCAO_VALIDA = (
    "Atualize a taxa da moeda 3 do Ativo Fixo (parâmetro MV_ATFMOED) e reprocesse a NF."
)


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


# --- filtro de qualidade da solução (ADR-011) ------------------------------


@pytest.mark.parametrize(
    "solucao,descartar",
    [
        ("Ajustado.", True),  # poucas palavras
        ("ok", True),
        ("Conforme solicitado, foi feita a correção", True),  # 6 palavras
        ("Favor validar e dar um ok se estiver de acordo com o processo", True),  # pedido
        ("Realizado o ajuste conforme combinado ontem com a equipe hoje", True),  # genérica curta
        (SOLUCAO_VALIDA, False),  # solução técnica com descrição
        (
            "Após atualizar a taxa da moeda do dia a classificação do documento de "
            "entrada foi normalizada corretamente no sistema",
            False,  # prosa longa, técnica, sem código — DEVE passar
        ),
    ],
)
def test_motivo_baixo_valor(solucao, descartar):
    assert (_motivo_baixo_valor(solucao) is not None) is descartar


def test_filtro_mantem_solucao_tecnica_em_prosa_mv_atfmoed():
    # Caso-chave: solução técnica REAL, em português, sem código de parâmetro.
    bruta = (
        "​Rafael, boa tarde, tudo bem? Após mapeamento, identificamos que a moeda a ser "
        "considerada para cálculo do Ativo Fixo estava incorreta. Após atualizarmos a "
        "taxa da moeda do dia, a classificação do documento de entrada foi normalizada. "
        "Verifique o lançamento da nota e, se necessário, estorne e preencha os dados."
    )
    assert _motivo_baixo_valor(limpar_texto(bruta)) is None


def test_filtro_descarta_encerramento_generico():
    bruta = (
        "Hi Aldenir Domingos, Bom dia, tudo bem? Conforme solicitado, foi feita a correção. Att,"
    )
    assert _motivo_baixo_valor(limpar_texto(bruta)) is not None


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
                {"id": 1, "status": 4},  # resolvido, solução com conteúdo -> ingere
                {"id": 2, "status": 2},  # aberto -> ignorado
                {"id": 3, "status": 5},  # fechado, sem solução pública -> sem_solucao
                {"id": 4, "status": 4},  # resolvido, solução de baixo valor -> descarta
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
    respx.get(f"{BASE}/tickets/3").mock(
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
    respx.get(f"{BASE}/tickets/4").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 4,
                "status": 4,
                "priority": 2,
                "description_text": "Erro ao gerar o relatório de comissões.",
                "requester": {"name": "Cliente C"},
                "company": {"name": "Empresa C"},
            },
        )
    )
    respx.get(f"{BASE}/tickets/1/conversations").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"private": False, "incoming": True, "body_text": "cliente descreve"},
                {"private": False, "incoming": False, "body_text": SOLUCAO_VALIDA},
            ],
        )
    )
    respx.get(f"{BASE}/tickets/3/conversations").mock(
        return_value=httpx.Response(
            200,
            json=[{"private": True, "incoming": False, "body_text": "só nota interna"}],
        )
    )
    respx.get(f"{BASE}/tickets/4/conversations").mock(
        return_value=httpx.Response(
            200,
            json=[{"private": False, "incoming": False, "body_text": "Ajustado."}],
        )
    )
    return {"t1": t1}


@respx.mock
async def test_ingerir_ingere_valido_descarta_baixo_valor_e_conta(settings):
    _montar_rotas_freshdesk()
    voyage = FakeVoyage()
    repo = FakeRepo()

    async with FreshdeskClient(settings) as fd:
        resumo = await ingerir(fd, voyage, repo, set())  # base vazia = nada ingerido

    assert resumo.ingeridos == 1  # ticket 1
    assert resumo.sem_solucao == 1  # ticket 3 (sem resposta pública)
    assert resumo.descartados_filtro == 1  # ticket 4 ("Ajustado.")
    assert len(repo.inseridos) == 1
    inserido = repo.inseridos[0]
    assert inserido["ticket_id"] == 1
    assert inserido["empresa"] == "Empresa A"
    assert inserido["solucao"] == SOLUCAO_VALIDA
    assert "SCC19070" in inserido["problema"]
    assert len(inserido["embedding"]) == 1024


@respx.mock
async def test_ingerir_pula_ja_ingeridos_sem_refetch(settings):
    rotas = _montar_rotas_freshdesk()
    voyage = FakeVoyage()
    repo = FakeRepo()

    async with FreshdeskClient(settings) as fd:
        # ticket 1 já está na base (idempotência pelo banco, ADR-016)
        resumo = await ingerir(fd, voyage, repo, {1})

    assert resumo.ja_processados == 1
    assert resumo.ingeridos == 0  # ticket 1 pulado; 3 sem solução; 4 baixo valor
    assert resumo.descartados_filtro == 1
    assert not rotas["t1"].called  # idempotente: não re-busca o já ingerido
    assert repo.inseridos == []
