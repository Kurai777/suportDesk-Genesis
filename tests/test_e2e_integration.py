"""Teste ponta a ponta (INTEGRAÇÃO) — custo zero, roda em CI com o Postgres de pé.

`@pytest.mark.integration` (pulado sem banco). Voyage e Claude são FALSOS/injetados;
o Postgres + pgvector são reais. Popula a base (incluindo o caso MV_ATFMOED), dispara
o pipeline com um chamado idêntico (deve recuperar o par certo e RESOLVER) e com um
chamado sem correspondência (deve ESCALAR com encontrou_solucao=false).

O fake de embeddings é determinístico: cada palavra-chave vira uma dimensão, então
textos que compartilham a chave viram vetores idênticos (distância de cosseno 0) e
chaves diferentes ficam ortogonais (distância 1).
"""

import psycopg
import pytest
from pgvector.psycopg import register_vector_async

from app.models import Requester, RespostaIA, TicketFreshdesk
from app.pipeline import IdempotenciaRepository, processar
from app.rag import RagRepository, RagService

pytestmark = pytest.mark.integration

DIM = 1024
_TOPICOS = {"MV_ATFMOED": 0, "SCC19070": 0, "MV_OUTRO": 1, "RELATORIO_B": 2}
_DESCONHECIDO = DIM - 1


def _vetor_para(texto: str) -> list[float]:
    v = [0.0] * DIM
    for chave, idx in _TOPICOS.items():
        if chave in texto:
            v[idx] = 1.0
    if not any(v):
        v[_DESCONHECIDO] = 1.0
    return v


class FakeVoyage:
    async def embed_document(self, textos):
        return [_vetor_para(t) for t in textos]

    async def embed_query(self, texto):
        return _vetor_para(texto)


class FakeClaudeE2E:
    """Simula o julgamento do Claude: RESOLVE se recuperou um par bem próximo."""

    async def gerar_resposta(self, problema, contexto_pares):
        if contexto_pares and contexto_pares[0].distancia < 0.5:
            par = contexto_pares[0]
            return RespostaIA(
                resposta_cliente=par.solucao,
                encontrou_solucao=True,
                confianca="alta",
                resumo_para_responsavel=f"Baseado no caso #{par.ticket_id}.",
                urgencia="alta",
                pedido_operacional=False,
            )
        return RespostaIA(
            resposta_cliente="Encaminhado para análise manual.",
            encontrou_solucao=False,
            confianca="baixa",
            resumo_para_responsavel="Nenhum caso similar na base.",
            urgencia="media",
            pedido_operacional=False,
        )

    async def reformular_query(self, problema):
        # E2E: mede a recuperação real, sem inventar reformulação — busca o texto cru.
        return problema


class FakeFreshdesk:
    def __init__(self, ticket):
        self._ticket = ticket
        self.notas = []
        self.atribuicoes = []

    async def get_ticket(self, ticket_id):
        return self._ticket

    async def criar_nota_interna(self, ticket_id, body):
        self.notas.append(body)

    async def atribuir(self, ticket_id, responder_id):
        self.atribuicoes.append((ticket_id, responder_id))


class FakeWhatsApp:
    def __init__(self):
        self.enviados = []

    async def enviar(self, numero, texto):
        self.enviados.append(texto)
        return True


_BASE = [
    (
        10,
        "Empresa X",
        "Erro no lançamento da NF. Log SCC19070 / MV_ATFMOED.",
        "Atualize a taxa da moeda 3 do Ativo Fixo e reprocesse a NF.",
    ),
    (20, "Empresa Y", "Tela trava ao abrir MV_OUTRO.", "Reinicie o serviço."),
    (30, "Empresa Z", "RELATORIO_B não é gerado.", "Recompile o fonte."),
]


@pytest.fixture
async def conn(settings):
    try:
        c = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True, connect_timeout=2
        )
    except Exception:
        pytest.skip("Postgres do docker-compose não está de pé (docker compose up -d db)")
    await register_vector_async(c)
    await c.execute("DELETE FROM conhecimento")
    await c.execute("DELETE FROM chamado_processado")
    repo = RagRepository(c)
    voyage = FakeVoyage()
    for ticket_id, empresa, problema, solucao in _BASE:
        [vetor] = await voyage.embed_document([problema])
        await repo.inserir(
            ticket_id=ticket_id,
            empresa=empresa,
            problema=problema,
            solucao=solucao,
            embedding=vetor,
        )
    try:
        yield c
    finally:
        await c.execute("DELETE FROM conhecimento")
        await c.execute("DELETE FROM chamado_processado")
        await c.close()


def _ticket(ticket_id, subject, description):
    return TicketFreshdesk(
        id=ticket_id,
        subject=subject,
        description_text=description,
        priority="alta",
        status=2,
        requester=Requester(name="Cliente"),
        empresa="Cliente Novo",
        responder_id=55,
    )


async def test_caso_conhecido_recupera_par_certo_e_resolve(conn, settings):
    rag = RagService(FakeVoyage(), RagRepository(conn))
    ticket = _ticket(101, "Problema na NF", "Log SCC19070: verifique o parâmetro MV_ATFMOED.")
    fd = FakeFreshdesk(ticket)
    wa = FakeWhatsApp()

    # O par correto (MV_ATFMOED, ticket 10) é recuperado como vizinho mais próximo.
    pares = await rag.buscar(f"{ticket.subject}\n\n{ticket.description_text}")
    assert pares[0].ticket_id == 10

    await processar(
        101,
        settings=settings,
        idempotencia=IdempotenciaRepository(conn),
        freshdesk=fd,
        rag_service=rag,
        claude=FakeClaudeE2E(),
        whatsapp=wa,
    )

    assert "🤖 Rascunho gerado por IA" in fd.notas[0]
    assert "Atualize a taxa da moeda 3" in fd.notas[0]  # solução do par certo
    assert fd.atribuicoes == [(101, 55)]
    assert any("✅ Chamado #101" in t for t in wa.enviados)


async def test_chamado_sem_correspondencia_escala(conn, settings):
    rag = RagService(FakeVoyage(), RagRepository(conn))
    ticket = _ticket(202, "Dúvida", "Como faço backup do ambiente XPTO?")
    fd = FakeFreshdesk(ticket)
    wa = FakeWhatsApp()

    await processar(
        202,
        settings=settings,
        idempotencia=IdempotenciaRepository(conn),
        freshdesk=fd,
        rag_service=rag,
        claude=FakeClaudeE2E(),
        whatsapp=wa,
    )

    assert "⚠️ IA não encontrou solução na base" in fd.notas[0]
    assert any("🔴 Chamado #202" in t for t in wa.enviados)
