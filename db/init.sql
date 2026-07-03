-- Inicialização do banco vetorial para DESENVOLVIMENTO LOCAL.
-- Executado automaticamente pelo container pgvector/pgvector na primeira subida
-- (montado em /docker-entrypoint-initdb.d/). Em produção (Railway) rode o
-- equivalente manualmente ou via migração.

-- Extensão de vetores (pgvector).
CREATE EXTENSION IF NOT EXISTS vector;

-- Base de conhecimento: um registro por chamado resolvido, guardando o PAR
-- completo (problema do cliente + solução do agente) mais o embedding usado na
-- busca por similaridade. A definição de QUAL texto é embedado (problema, ou
-- problema+solução) fica no Módulo 4 (rag.py + ingest_tickets.py); aqui só
-- garantimos a coluna vector(1024) — dimensão do Voyage `voyage-3`.
CREATE TABLE IF NOT EXISTS conhecimento (
    id         BIGSERIAL PRIMARY KEY,
    ticket_id  BIGINT,                       -- id do chamado de origem no Freshdesk
    empresa    TEXT,
    problema   TEXT        NOT NULL,          -- relato do cliente (inclui logs, ex.: SCC19070)
    solucao    TEXT        NOT NULL,          -- resposta do agente que resolveu
    embedding  VECTOR(1024) NOT NULL,         -- Voyage voyage-3
    criado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índice ANN para busca por similaridade de cosseno (embeddings Voyage).
CREATE INDEX IF NOT EXISTS conhecimento_embedding_idx
    ON conhecimento
    USING hnsw (embedding vector_cosine_ops);

-- Idempotência do webhook: cada chamado é processado uma única vez. A entrada do
-- pipeline faz INSERT ... ON CONFLICT DO NOTHING; se não inserir, é reentrega.
CREATE TABLE IF NOT EXISTS chamado_processado (
    ticket_id  BIGINT PRIMARY KEY,
    criado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);
