# INFRA.md — Onde a base de conhecimento vive (e como movê-la)

> Guia para parar de carregar a base entre máquinas (desktop ↔ notebook). Decisão
> registrada na **ADR-024**.

---

## TL;DR — você está sincronizando o arquivo errado

- A base de conhecimento **NÃO é** `docs_totvs/`. É a tabela **`conhecimento`** no
  Postgres + pgvector.
- `docs_totvs/` (8.349 `.txt`, 42 MB) é um **artefato intermediário descartável**: serve só
  de entrada para o `ingest_docs`, e é reprodutível pelo `coletar_central`.
- O que tem valor são os **embeddings já pagos** (8.325 vetores `voyage-3`, 1024 dims). Levar
  os `.txt` obriga a **re-embeddar tudo** no destino; levar o banco, não.
- Um `pg_dump` comprimido tem **37 MB** — *menor* que os `.txt` e já com os vetores dentro.
- Melhor ainda: **pare de transferir**. Hospede um Postgres gerenciado e aponte as duas
  máquinas para ele.

## Fatos medidos (2026-07-10)

| Item | Valor |
|---|---|
| `conhecimento` | **8.350 linhas** (8.325 `documentacao` + 25 `ticket`) |
| `chamado_processado` | 0 linhas |
| Tabela + índice HNSW | 121 MB |
| Banco inteiro | 128 MB |
| **`pg_dump -Fc -Z9`** | **37 MB** |
| Stack | Postgres 16.14 · pgvector 0.8.4 · `VECTOR(1024)` |

Hoje: container `suporte-totvs-db` (docker-compose), host **5433** → 5432, db `suporte_totvs`.

---

## Opção A — Postgres gerenciado (RECOMENDADO): acaba a transição

Faz-se **uma vez**. Depois, notebook e desktop só precisam de `DATABASE_URL` no `.env` —
sem Drive, sem docker local, sem `docs_totvs/`.

Provedores com pgvector: **Neon**, **Railway** (já é o alvo de deploy do CLAUDE.md), Supabase.
Os 128 MB cabem folgado em qualquer free tier.

```bash
# 1. Habilitar a extensão no banco novo
psql "$DATABASE_URL" -c 'CREATE EXTENSION IF NOT EXISTS vector;'

# 2. Restaurar o dump (leva os embeddings prontos — NÃO re-embeda nada)
pg_restore -d "$DATABASE_URL" --no-owner --no-privileges base_conhecimento.dump

# 3. Conferir
psql "$DATABASE_URL" -c 'SELECT fonte, count(*) FROM conhecimento GROUP BY fonte;'
```

Sem `psql`/`pg_restore` instalados, rode pelo container:

```bash
docker run --rm -v "$PWD:/w" -w /w postgres:16 \
  pg_restore -d "$DATABASE_URL" --no-owner --no-privileges base_conhecimento.dump
```

Depois, no `.env` de **cada máquina** (o arquivo é ignorado pelo git):

```
DATABASE_URL=postgresql://<user>:<senha>@<host>/<db>?sslmode=require
```

E `python run_local.py` funciona direto no notebook, com zero dado local.

> **Dev × prod:** não reaproveite o MESMO banco para desenvolvimento e produção — o
> `ingest_docs` e experimentos escrevem na `conhecimento`. Use dois bancos (ou *branches* do
> Neon: `main` = prod, `dev` = trabalho).
>
> **Latência:** o RAG faz **1 query** por chamado; +50–200 ms de rede é irrelevante. Só a
> ingestão em massa (milhares de INSERTs) fica mais lenta — e é operação única.

---

## Opção B — Snapshot em bucket/Drive (offline, e também o backup)

Hoje **não existe backup** da base. Este snapshot resolve as duas coisas.

**Gerar** (o `MSYS_NO_PATHCONV=1` é obrigatório no Git Bash/Windows, senão `/tmp` vira
caminho Windows e o `pg_dump` falha):

```bash
MSYS_NO_PATHCONV=1 docker exec suporte-totvs-db \
  pg_dump -U totvs -d suporte_totvs -Fc -Z9 -f /tmp/base.dump
MSYS_NO_PATHCONV=1 docker cp suporte-totvs-db:/tmp/base.dump ./base_conhecimento.dump
MSYS_NO_PATHCONV=1 docker exec suporte-totvs-db rm -f /tmp/base.dump
```

**Restaurar** numa máquina nova (com o `docker-compose up` já de pé):

```bash
MSYS_NO_PATHCONV=1 docker cp base_conhecimento.dump suporte-totvs-db:/tmp/base.dump
MSYS_NO_PATHCONV=1 docker exec suporte-totvs-db \
  pg_restore -U totvs -d suporte_totvs --clean --if-exists --no-owner /tmp/base.dump
```

Guarde onde quiser: S3 / Cloudflare R2 / GCS / Drive. **37 MB por snapshot.**

> ⚠️ `--clean` **derruba as tabelas** antes de restaurar. Nunca aponte para um banco cujo
> conteúdo você não quer sobrescrever.

---

## Opção C — `docs_totvs/` num bucket (matéria-prima, opcional)

Só se quiser arquivar a fonte bruta. **Não é necessário para rodar o serviço** — o serviço
lê apenas o banco. Já está no `.gitignore` e é regenerável:

```bash
python -m scripts.coletar_central --temas fiscal,nf,financeiro,compras,relatorios,estoque,rh
python -m scripts.ingest_docs
```

---

## Guardrails (leia antes de mexer)

1. ⚠️ **`pytest` APAGA linhas.** Os testes de integração fazem `DELETE FROM conhecimento`.
   **NUNCA** aponte `TEST_DATABASE_URL` para o banco da aplicação — muito menos para o
   gerenciado. O default do `conftest.py` já é um `..._test` local e dedicado.
2. `DATABASE_URL` é **segredo**: só no `.env` (ignorado pelo git). Nunca versionado.
3. `*.dump` está no `.gitignore` — os 37 MB não vão para o repositório.
4. **Dimensão do vetor:** `db/init.sql` fixa `VECTOR(1024)` = `voyage-3`. Trocar de modelo de
   embedding exige recriar a coluna e **re-embeddar tudo**.
5. O índice é **HNSW** (cosseno). O `pg_restore` o recria sozinho.
6. Perdeu o dump? Não é o fim: `coletar_central` + `ingest_docs` refazem a base. Custa tempo
   e as chamadas de embedding do Voyage. O dump existe para você não pagar isso de novo.

---

## Recomendação

**A** (Postgres gerenciado compartilhado) para acabar com a transição, **+ B** (snapshot
periódico em bucket) como backup. **C** é opcional.
