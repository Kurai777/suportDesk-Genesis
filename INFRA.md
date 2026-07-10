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

## ✅ Estado atual — a base vive no Neon (opção A, aplicada em 2026-07-10)

| | |
|---|---|
| Projeto Neon | **Genesis** (`bold-field-49175189`), org `brunoeted@gmail.com` |
| Região / versão | `aws-us-east-1` · **Postgres 18.4** · pgvector **0.8.1** |
| Banco | `neondb` · role `neondb_owner` |
| Conteúdo | **8.350 linhas** (8.325 doc + 25 ticket), 0 sem embedding, 1024 dims, índice HNSW recriado, 120 MB |

Restaurado a partir do `base_conhecimento.dump` (37 MB) — os embeddings vieram prontos,
**nada foi re-embeddado**. Verificado: busca vetorial com parâmetro `Vector` pelo mesmo
adapter `psycopg + pgvector` que o app usa.

O `.env` do desktop já aponta para lá; a `DATABASE_URL` local do docker ficou **comentada
dentro do próprio `.env`** como backup.

O container local `suporte-totvs-db` (host **5433**, db `suporte_totvs`) continua existindo e
serve agora só para (a) o banco de TESTES `suporte_totvs_test` e (b) gerar novos dumps.

### Configurar o notebook (ou qualquer máquina nova)

Não precisa de docker, nem de `docs_totvs/`, nem do dump:

1. `git clone` + `pip install -r requirements.txt`
2. Copie o `.env` (traga os segredos por um canal seguro — **nunca** por git/Drive público).
3. `python run_local.py` — o RAG já lê o Neon.

Para rodar os testes na máquina nova é preciso um Postgres local com o banco dedicado
`suporte_totvs_test` (`docker compose up` + `db/init.sql`), e exportar:

```bash
export TEST_DATABASE_URL=postgresql://totvs:totvs@localhost:5433/suporte_totvs_test
```

---

Situação anterior: container `suporte-totvs-db` (docker-compose), host **5433** → 5432,
db `suporte_totvs`.

---

## Opção A — Postgres gerenciado (FEITA): acaba a transição

Feita **uma vez**. Depois, notebook e desktop só precisam de `DATABASE_URL` no `.env` —
sem Drive, sem docker local, sem `docs_totvs/`.

Provedores com pgvector: **Neon** (escolhido), Railway, Supabase. Os 128 MB cabem folgado
em qualquer free tier.

Como foi feito (reproduzível para outro banco/provedor):

```bash
# O dump já traz `CREATE EXTENSION vector`; --no-comments pula o COMMENT ON EXTENSION,
# que exige superuser e falharia num banco gerenciado.
docker cp base_conhecimento.dump suporte-totvs-db:/tmp/neon.dump
docker exec -e PGURL="$URL_DIRETO" suporte-totvs-db \
  sh -c 'pg_restore -d "$PGURL" --no-owner --no-privileges --no-comments /tmp/neon.dump'

# Conferir
psql "$URL_DIRETO" -c 'SELECT fonte, count(*) FROM conhecimento GROUP BY fonte;'
```

> ⚠️ **Use o endpoint DIRETO, não o `-pooler`.** O Neon devolve por padrão a URI do pooler
> (pgbouncer em modo transação). Isso quebra os *prepared statements* que o **psycopg3**
> cria automaticamente (após ~5 execuções da mesma query) — estoura na ingestão em massa.
> Basta remover o sufixo `-pooler` do host. É a URL que está no `.env`.

> **Cliente antigo, servidor novo:** o dump saiu de um `pg_dump` **16** e foi restaurado num
> Postgres **18**. Restaurar dump antigo em servidor novo é o caminho normal de upgrade —
> funcionou sem erro (exit 0). O inverso (dumpar de um servidor mais novo com cliente antigo)
> é que é proibido.

> **Dev × prod:** não reaproveite o MESMO banco para desenvolvimento e produção — o
> `ingest_docs` e experimentos escrevem na `conhecimento`. Use dois bancos (ou *branches* do
> Neon: `main` = prod, `dev` = trabalho). **Ainda pendente.**
>
> **Latência:** o RAG faz **1 query** por chamado; +50–200 ms de rede é irrelevante. Só a
> ingestão em massa (milhares de INSERTs) fica mais lenta — e é operação única. O compute do
> Neon suspende quando ocioso: a primeira query após a pausa paga um *cold start* (~0,5 s).

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
   **NUNCA** aponte `TEST_DATABASE_URL` para o banco da aplicação — muito menos para o Neon.
   *Verificado:* o `conftest.py` lê **apenas** `TEST_DATABASE_URL` (default `..._test` local) e
   **nunca** a `DATABASE_URL` do `.env`; os dois usos de `get_settings` em `test_main.py` são
   `monkeypatch`. Por construção, `pytest` **não alcança o Neon** — a suíte foi rodada depois
   da migração e as 8.350 linhas seguiram intactas.
2. `DATABASE_URL` é **segredo**: só no `.env` (ignorado). Nunca versionado.
3. `.gitignore` cobre `.env`, **`.env.*`** (pega backups tipo `.env.bak.local`) com exceção de
   `!.env.example`, além de `*.dump` (os 37 MB não vão ao repositório) e `docs_totvs/`.
4. **Dimensão do vetor:** `db/init.sql` fixa `VECTOR(1024)` = `voyage-3`. Trocar de modelo de
   embedding exige recriar a coluna e **re-embeddar tudo**.
5. O índice é **HNSW** (cosseno). O `pg_restore` o recria sozinho.
6. Perdeu o dump? Não é o fim: `coletar_central` + `ingest_docs` refazem a base. Custa tempo
   e as chamadas de embedding do Voyage. O dump existe para você não pagar isso de novo.

---

## Recomendação

**A** (Postgres gerenciado compartilhado) para acabar com a transição, **+ B** (snapshot
periódico em bucket) como backup. **C** é opcional.
