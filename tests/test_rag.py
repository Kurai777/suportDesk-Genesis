"""Testes do Módulo 4 — rag.py (VoyageClient e RagService).

Embeddings e banco são injetados como fakes — nenhuma chamada real (Voyage/pgvector).
O SQL do RagRepository é exercido em integração com o Postgres local (docker-compose),
não aqui.
"""

from app.rag import RagService, Similar, VoyageClient


class _FakeEmbedResult:
    def __init__(self, embeddings: list[list[float]]) -> None:
        self.embeddings = embeddings


class FakeVoyageBackend:
    """Imita o voyageai.AsyncClient: registra as chamadas de `embed`."""

    def __init__(self) -> None:
        self.chamadas: list[dict] = []

    async def embed(self, texts, model, input_type):
        self.chamadas.append({"texts": texts, "model": model, "input_type": input_type})
        return _FakeEmbedResult([[0.1, 0.2, 0.3] for _ in texts])


# --- VoyageClient ----------------------------------------------------------


async def test_embed_query_usa_input_type_query(settings):
    backend = FakeVoyageBackend()
    voyage = VoyageClient(settings, client=backend)

    vetor = await voyage.embed_query("problema do chamado novo")

    assert vetor == [0.1, 0.2, 0.3]
    assert backend.chamadas[-1]["input_type"] == "query"
    assert backend.chamadas[-1]["model"] == "voyage-3"
    assert backend.chamadas[-1]["texts"] == ["problema do chamado novo"]


async def test_embed_document_usa_input_type_document(settings):
    backend = FakeVoyageBackend()
    voyage = VoyageClient(settings, client=backend)

    vetores = await voyage.embed_document(["a", "b"])

    assert len(vetores) == 2
    assert backend.chamadas[-1]["input_type"] == "document"


# --- RagService (orquestração) ---------------------------------------------


class FakeVoyageClient:
    def __init__(self) -> None:
        self.ultima_query: str | None = None

    async def embed_query(self, texto: str) -> list[float]:
        self.ultima_query = texto
        return [0.5, 0.5, 0.5]


class FakeRepo:
    def __init__(self, resultado: list[Similar]) -> None:
        self.resultado = resultado
        self.vec: list[float] | None = None
        self.k: int | None = None

    async def buscar_similares(self, embedding: list[float], k: int) -> list[Similar]:
        self.vec = embedding
        self.k = k
        return self.resultado


async def test_rag_service_embeda_query_e_delega_para_o_repo():
    similar = Similar(ticket_id=1, problema="p", solucao="s", empresa="e", distancia=0.1)
    voyage = FakeVoyageClient()
    repo = FakeRepo([similar])
    service = RagService(voyage, repo)

    resultado = await service.buscar("problema novo", k=3)

    assert resultado == [similar]
    assert voyage.ultima_query == "problema novo"
    assert repo.vec == [0.5, 0.5, 0.5]
    assert repo.k == 3
