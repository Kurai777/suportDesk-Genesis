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


# --- busca_uniao (ADR-024): merge por menor distância, dedup por trecho ----


class _VoyagePorQuery:
    """Embeda cada query num vetor distinto (a query vira o próprio 'vetor')."""

    async def embed_query(self, texto: str) -> list[float]:
        return [float(len(texto))]


class _RepoPorQuery:
    """Devolve resultados diferentes conforme a query (o vetor identifica a query)."""

    def __init__(self, por_vetor: dict[float, list[Similar]]) -> None:
        self._por_vetor = por_vetor
        self.chamadas: list[float] = []

    async def buscar_similares(self, embedding: list[float], k: int) -> list[Similar]:
        chave = embedding[0]
        self.chamadas.append(chave)
        return self._por_vetor.get(chave, [])[:k]


def _doc(titulo: str, dist: float) -> Similar:
    return Similar(None, "prob", "sol", None, dist, fonte="documentacao", titulo=titulo)


def _tkt(tid: int, dist: float) -> Similar:
    return Similar(tid, "prob", "sol", "emp", dist, fonte="ticket")


async def test_uniao_mantem_menor_distancia_por_trecho():
    # "aa"=2.0 acha o DOC longe (0.50) e o CHAMADO perto (0.18);
    # "bbbb"=4.0 acha o MESMO doc perto (0.30). União: doc a 0.30 + chamado a 0.18.
    doc_longe, doc_perto = _doc("MATA010", 0.50), _doc("MATA010", 0.30)
    chamado = _tkt(7, 0.18)
    repo = _RepoPorQuery({2.0: [chamado, doc_longe], 4.0: [doc_perto]})
    service = RagService(_VoyagePorQuery(), repo)

    resultado = await service.buscar_uniao(["aa", "bbbb"], k=5)

    assert [(p.fonte, round(p.distancia, 2)) for p in resultado] == [
        ("ticket", 0.18),  # ordenado por distância
        ("documentacao", 0.30),  # o doc PERTO venceu o mesmo doc longe (dedup por título)
    ]


async def test_uniao_ignora_queries_repetidas_e_vazias():
    repo = _RepoPorQuery({2.0: [_tkt(1, 0.1)]})
    service = RagService(_VoyagePorQuery(), repo)

    await service.buscar_uniao(["aa", "aa", "  "], k=3)

    assert repo.chamadas == [2.0]  # buscou UMA vez só (dedup de query, vazia descartada)


async def test_uniao_de_uma_query_so_e_a_busca_simples():
    repo = _RepoPorQuery({2.0: [_tkt(1, 0.1)]})
    service = RagService(_VoyagePorQuery(), repo)

    resultado = await service.buscar_uniao(["aa"], k=3)

    assert [p.ticket_id for p in resultado] == [1]
    assert repo.chamadas == [2.0]
