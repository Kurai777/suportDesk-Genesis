# Decisões técnicas (ADR)

Registro curto e append-only das decisões de arquitetura. Cada entrada:
**contexto → decisão → consequência**. Decisões marcadas **(a confirmar)** foram
assumidas para não travar o build e podem ser revisadas.

---

## Definição de Pronto (DoD)

Um módulo só é considerado PRONTO quando:

1. Código com type hints e funções curtas, seguindo as "Regras de código
   (invioláveis)" e os "Padrões de Engenharia" do CLAUDE.md.
2. Testes cobrindo os caminhos principais, **sem nenhuma chamada real paga**
   (respx/mocks). Suíte inteira verde (`pytest`).
3. `ruff check` sem erros.
4. Nenhum segredo no código — tudo em variáveis de ambiente.
5. Decisões relevantes registradas aqui no `DECISIONS.md`.
6. Explicação curta (2–3 frases) do que o módulo faz.

---

## ADR-001 — Embeddings: Voyage AI `voyage-3` (1024 dimensões)

- **Contexto:** CLAUDE.md deixou "DEFINIR: Voyage ou OpenAI".
- **Decisão:** Voyage `voyage-3`, dimensão 1024.
- **Consequência:** coluna `VECTOR(1024)` no pgvector; `VOYAGE_API_KEY`/`VOYAGE_MODEL`
  no `.env`. Qualidade de recuperação é crítica para a regra anti-alucinação.

## ADR-002 — Webhook do Freshdesk envia só o `ticket_id`

- **Contexto:** o payload do webhook pode ser montado na automação ou buscado via API.
- **Decisão:** a automação envia apenas `{"ticket_id": ...}`; o chamado completo é
  buscado via API em `freshdesk.py`.
- **Consequência:** `WebhookFreshdesk` mínimo; o parsing "de verdade" acontece sobre
  a resposta da API (`TicketFreshdesk`), não sobre o webhook.

## ADR-003 — `urgencia` espelha as prioridades do Freshdesk

- **Contexto:** o CLAUDE.md não enumerava valores de `urgencia`.
- **Decisão:** vocabulário `baixa | media | alta | urgente`, igual às prioridades do
  Freshdesk (Low/Medium/High/Urgent).
- **Consequência:** `urgencia` (em `RespostaIA`) é a **urgência percebida pelo
  conteúdo** do chamado, lida pelo Claude — um sinal **complementar** à prioridade
  oficial do ticket (`TicketFreshdesk.priority`), não um substituto.

## ADR-004 — `FreshdeskClient` (API v2)

- **Auth:** Basic Auth (`FRESHDESK_API_KEY` como usuário, `"X"` como senha) sobre httpx.
- **Endpoints:**
  - `GET /tickets/{id}?include=requester,company,stats` → `TicketFreshdesk`
  - `POST /tickets/{id}/notes` com `{"body": ..., "private": true}`
  - `PUT /tickets/{id}` com `{"responder_id": ...}`
- **Mapa de prioridade:** 1→baixa, 2→media, 3→alta, 4→urgente (desconhecido→media).
- **Empresa:** `company.name`, com fallback `"Empresa não identificada"` quando
  `company` é nulo.
- **Retry:** tenacity re-tenta em erro de rede e HTTP 429, respeitando `Retry-After`.
- **Fase 1:** nenhum método de resposta pública ao cliente.

## ADR-005 — I/O assíncrono ponta a ponta

- **Contexto:** app FastAPI; o webhook responde `200` na hora e processa em background.
- **Decisão:** clientes (`FreshdeskClient`, `VoyageClient`, `ClaudeClient`,
  `WhatsAppClient`) e pipeline **assíncronos** — `httpx.AsyncClient`, psycopg async e
  os SDKs em modo async. O pipeline roda como coroutine em `BackgroundTasks`.
- **Consequência:** melhor uso do event loop do FastAPI para trabalho I/O-bound;
  todos os módulos seguem o padrão async. Testes usam `pytest-asyncio`
  (`asyncio_mode=auto`) + respx. O retry do tenacity funciona sobre corrotinas.

## ADR-006 — Ingestão da solução e estratégia resumível

- **Embeddings:** biblioteca `voyageai` (SDK oficial), `AsyncClient`. Modelo padrão
  `voyage-3` (1024 dims, multilíngue), parametrizável por `VOYAGE_MODEL` — trocar para
  `voyage-3.5` é só mudar o env, sem alterar o schema `VECTOR(1024)`. Evitamos as
  variantes "lite": a qualidade de recuperação É a trava anti-alucinação e o custo de
  embedding é irrelevante no volume do projeto.
- **Retrieval assimétrico:** só o **problema** (assunto + descrição/logs) entra no vetor
  (`input_type="document"` na ingestão, `"query"` na busca). A **solução** é carga
  associada, retornada como contexto. Registro: `{ticket_id, problema, solucao, empresa}`
  + vetor(1024). Busca: top-k por distância de cosseno (`<=>`, casa com o índice HNSW).
- **Heurística da "solução":** é a **última resposta PÚBLICA do agente** na timeline de
  `GET /tickets/{id}/conversations` — item com `private=false` e `incoming=false`. Sem
  nenhuma, o chamado é pulado (sem solução identificável). O "problema" é o
  `description_text` do chamado.
- **Enumeração:** `GET /tickets?page=&per_page=100` (paginação), filtrando
  `status ∈ {4 Resolvido, 5 Fechado}` **no código** — não usa o endpoint de filtro por
  causa do teto de 300 resultados.
- **Resumível + rate limit:** checkpoint em arquivo (`ingest_state.txt`, gitignored) com
  os `ticket_id` já processados — pulados em re-execuções, sem re-fetch. Pausa entre
  chamadas; o `FreshdeskClient` re-tenta 429 respeitando `Retry-After`. Nunca varre todo
  o histórico de uma vez sem controle de taxa.
- **Teste do repositório:** o SQL do `RagRepository` (`inserir`/`buscar_similares`) é
  validado por um teste de INTEGRAÇÃO marcado (`@pytest.mark.integration`) contra o
  Postgres do docker-compose — confere o vizinho por cosseno (`<=>`) e o uso do índice
  HNSW (via `EXPLAIN` com `enable_seqscan=off`). É pulado se o banco não estiver de pé.

## ADR-007 — ClaudeClient e saída estruturada

- **`empresa` fora da saída do Claude:** removido de `RespostaIA`. A empresa é um FATO do
  chamado (`TicketFreshdesk.empresa`), não algo que o modelo deva gerar — evita o modelo
  "pegar" a empresa errada de um vizinho recuperado. O pipeline monta
  `ResultadoChamado` = `RespostaIA` + `empresa` + `ticket_id`.
- **`temperature=0`, `max_tokens=1024`:** respostas determinísticas e conservadoras. O
  Haiku 4.5 aceita `temperature` (não é dos modelos que removeram sampling params).
- **Saída estruturada via TOOL USE FORÇADO:** tool cujo `input_schema` é o schema de
  `RespostaIA`, com `tool_choice={"type":"tool","name":"responder_chamado"}` obrigando a
  chamada; o `input` é validado contra `RespostaIA`. Escolhido em vez do recurso nativo
  (`output_config.format`/`messages.parse`) porque (a) é o padrão do CLAUDE.md e (b) o SDK
  fixado (`anthropic==0.42.0`) antecede a saída estruturada nativa. Migração é fácil se o
  SDK for atualizado.
- **Contexto vazio:** curto-circuito sem chamar o modelo — retorna `encontrou_solucao=false`,
  `confianca="baixa"` (economiza chamada; a regra de ouro já garantiria isso).
- **Prompt caching:** `cache_control: ephemeral` no bloco estático de instruções (system);
  o contexto variável fica no user message, fora do cache. Obs.: em Haiku 4.5 o prefixo
  mínimo cacheável é ~4096 tokens, então o cache só passa a valer quando o bloco estático
  crescer — a plumbing já fica correta.

## ADR-008 — WhatsAppClient: notificação "melhor esforço"

- **Nunca derruba o processamento:** o chamado já foi tratado no Freshdesk (nota +
  atribuição) ANTES da notificação; uma falha de WhatsApp não pode desfazer isso. Por
  isso `enviar` NUNCA propaga exceção — em qualquer falha (rede, número inválido,
  Evolution fora do ar) loga e retorna `False`.
- **Retry só para rede transitória:** tenacity re-tenta apenas `httpx.RequestError`
  (poucas tentativas, backoff exponencial). Erros HTTP (ex.: 400 número inválido) NÃO são
  re-tentados; caem no retorno `False`.
- **Token da INSTÂNCIA:** header `apikey` = `WHATSAPP_API_KEY` (token da instância da
  Evolution, não o global). Endpoint: `POST {WHATSAPP_API_URL}/message/sendText/{WHATSAPP_INSTANCE}`,
  payload `{"number": <normalizado>, "text": <mensagem>}`.
- **Modo dry-run:** `WHATSAPP_DRY_RUN` (padrão `true` em dev) — `enviar` só loga a mensagem
  que seria enviada e retorna `True`, sem chamar a Evolution. Produção: `false`.
- **Cliente burro:** recebe número + texto e envia; NÃO monta os textos (isso é do
  pipeline). `normalizar_numero` é função pura (remove não-dígitos; garante DDI 55 para
  números de 10–11 dígitos).
- **Resolução agente→telefone:** `Settings.telefone_responsavel(responder_id)` consulta
  `RESPONSAVEIS` (mapa id→telefone) com fallback para `WHATSAPP_RESPONSAVEL_DEFAULT`.
  Definida no config, usada pelo pipeline (Módulo 7).

## ADR-009 — Pipeline: ordem das ações, idempotência e fallback seguro

- **Ordem (invariável):** idempotência → Freshdesk (nota interna + atribuição) → WhatsApp.
  O WhatsApp é sempre a ÚLTIMA ação e é melhor esforço — nunca bloqueia nem desfaz o que
  já foi feito no Freshdesk. Nunca há resposta pública ao cliente (Fase 1 copiloto).
- **Decisão resolvido × escalar:** função PURA sem I/O (`decidir`) — resolver quando
  `encontrou_solucao=true` E `confianca >= CONFIANCA_MINIMA` (ordem alta > media > baixa);
  caso contrário, escalar. Testável isoladamente.
- **Idempotência na ENTRADA:** `INSERT INTO chamado_processado (ticket_id) ON CONFLICT DO
  NOTHING`; se `rowcount == 0`, o ticket já está em processamento e a reentrega é ignorada.
  Tabela criada no `init.sql`. Uma conexão psycopg POR TAREFA (uma query por vez — não
  compartilha conexão entre tarefas concorrentes, o que corromperia o protocolo).
- **Fallback seguro (falha no miolo, passos 2–5):** se ler o chamado / buscar / gerar /
  decidir lançar exceção, captura, loga e executa nota de indisponibilidade + atribuição +
  WhatsApp de escalonamento. O chamado NUNCA fica marcado como processado sem nenhuma ação
  para um humano. O WhatsApp do fallback dispara mesmo se o Freshdesk estiver fora, para
  garantir o alerta ao responsável.
- **Webhook:** header `X-Webhook-Secret` comparado em tempo constante (`hmac.compare_digest`)
  com `FRESHDESK_WEBHOOK_SECRET` (segredo vazio nunca é válido). Responde `200 OK` na hora e
  processa em `BackgroundTasks`. Clientes de longa vida (httpx, Voyage, Claude) criados no
  startup e reaproveitados; fechados no shutdown.

## ADR-010 — Testes ponta a ponta (duas formas)

- **(a) Integração automática (custo zero, roda em CI):** `@pytest.mark.integration` contra
  o Postgres do docker-compose, com Voyage e Claude FALSOS injetados. Um fake de embeddings
  determinístico (palavra-chave → dimensão) faz o par MV_ATFMOED ser recuperado como vizinho
  exato; o fake do Claude decide "resolvido" quando o par recuperado é próximo. Verifica o
  retrieval correto + o caminho resolvido, e um chamado sem correspondência →
  `encontrou_solucao=false` → caminho escalar. Pulado se o banco não estiver de pé.
- **(b) Smoke-test manual (`scripts/smoke_test.py`) — ⚠️ CONSOME CHAMADAS PAGAS:** roda o
  fluxo REAL (Voyage embeda de verdade, Claude gera de verdade) contra a base populada e
  imprime o `ResultadoChamado` completo + o contexto recuperado + a decisão + a nota que
  seria criada, para inspeção HUMANA da qualidade da resposta. WhatsApp forçado para dry-run
  (nenhum envio real; nada é escrito no Freshdesk). NÃO entra na suíte automática (regra:
  nenhum teste gasta chamada paga).
- **Correções encontradas ao rodar a integração de verdade (2 bugs reais):**
  1. **pgvector.Vector nos parâmetros:** o adaptador do pgvector não converte `list` →
     `vector` (manda `double precision[]`, sem operador `vector <=> double precision[]`). O
     `RagRepository` agora envolve o embedding em `pgvector.Vector` no insert e na busca. Sem
     isso, a RAG falharia contra qualquer Postgres real.
  2. **Windows + psycopg async:** o `ProactorEventLoop` padrão do Windows não é suportado pelo
     psycopg async — os pontos de entrada (`main.py`, `ingest_tickets.py`, `smoke_test.py`) e
     o `conftest` setam `WindowsSelectorEventLoopPolicy`. No-op em Linux (Railway).
- **`TEST_DATABASE_URL`:** o `conftest` aceita essa env var para apontar a integração a
  outro host/porta (útil quando a 5432 já está ocupada). Padrão: banco local do compose.
