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

## ADR-011 — Qualidade da base: limpeza de texto + filtro de solução

- **Contexto:** o teste real mostrou recuperação fraca — muitas soluções ingeridas eram de
  baixo valor ("foi feita a correção", "ajustado") e os textos vinham com ruído de e-mail
  (saudações, assinaturas, blocos citados, `[cid:...]`).
- **Limpeza (`app/texto.py::limpar_texto`):** função PURA e testável que remove saudações,
  despedidas + assinatura (corta da despedida em diante), blocos citados (`>`), cabeçalhos
  de encaminhamento (`De:`/`Para:`/`Assunto:`…), marcadores `[cid:...]` e normaliza espaços,
  **preservando o corpo técnico** (guardas de comprimento evitam cortar linhas técnicas que
  começam com uma palavra de despedida). Aplicada ao problema e à solução na ingestão **e**
  ao problema da consulta no pipeline (`_montar_problema`), para a query casar com o indexado.
- **Problema indexado = assunto + `description_text`** (antes era só a descrição) — igual ao
  que o pipeline usa na consulta, garantindo simetria de retrieval.
- **Filtro de qualidade (`ingest_tickets.py`):** após a limpeza, descarta soluções com < ~40
  caracteres úteis OU que sejam encerramentos sem conteúdo ("foi feita a correção",
  "ajustado", "conforme solicitado", "conforme conversamos", "chamado fechado por falta de
  interação", "segue anexo/em anexo"). O contador `descartados_filtro` é registrado no
  resumo, **separado** de `sem_solucao`. Soluções longas que apenas começam com uma dessas
  frases mas têm conteúdo real são mantidas.
- **Termômetro (`scripts/avaliar_recuperacao.py`, ⚠️ consome embeddings):** para uma amostra
  de N pares, usa o próprio problema como consulta e mede a taxa de acerto (par no top-k) e a
  distância média do auto-match — métrica objetiva para comparar antes/depois das regras.

## ADR-012 — Avaliação honesta (leave-one-out) e filtro de qualidade reforçado

- **Contexto:** o auto-match deu 100%, mas só prova que o embedding é determinístico — não
  reflete o uso real (chamado novo, palavras diferentes). E o filtro da ADR-011 descartou só
  ~2 de ~114 soluções, ou seja, estava frouxo demais.
- **Avaliação realista (`scripts/avaliar_realista.py`, ⚠️ consome embeddings):** leave-one-out
  — para cada chamado da amostra, EXCLUI ele mesmo dos candidatos (`WHERE id <> ...`) e busca
  os top-k *outros* pares. Como não há rótulo de "par certo", imprime os top-k (ticket_id +
  trecho + distância) para avaliação MANUAL da relevância, e resume a distribuição das
  distâncias top-1 (mín/média/máx). É a métrica honesta para comparar iterações.
- **Filtro reforçado (`ingest_tickets.py`):** além de curtas (< 40) e encerramentos genéricos
  (lista ampliada: "realizado o ajuste", "corrigido", "resolvido", "segue", "ok", "feito",
  "conforme combinado"…), descarta soluções **sem indício técnico** — sem parâmetro (`MV_*`),
  código/erro/rotina (`SCC19070`, `MATA010`), tabela/campo Protheus (`SA1`, `A1_COD`), sigla
  do domínio (CFOP/NCM/SPED/TES/SIGA…) nem termo técnico em prosa (parâmetro, rotina, tabela,
  campo, ponto de entrada, gatilho…). O resumo registra `descartados_filtro` **e imprime
  exemplos do que foi descartado (com o motivo)** para calibração humana.
- **Calibração é iterativa:** o filtro é propositalmente agressivo; os exemplos descartados +
  a avaliação realista guiam o ajuste fino (relaxar/apertar padrões e termos técnicos).

## ADR-013 — Recalibração: filtro por conteúdo (não por código) + limpeza inline

- **Contexto:** o filtro da ADR-012 ficou RÍGIDO DEMAIS — descartou 85 de ~114, incluindo o
  caso `MV_ATFMOED` (solução técnica real, mas escrita em português, sem código de parâmetro).
  O critério "exige indício técnico (código/parâmetro/tabela)" estava punindo boas soluções
  em prosa. Além disso, muitos e-mails vêm em UMA LINHA SÓ ("Hi Fulano, Bom dia, tudo bem?
  <conteúdo> Att,"), que a limpeza antiga (por linha) não tratava.
- **Limpeza reforçada (`app/texto.py`):** além do tratamento por linha, remove saudações e
  cordialidades INLINE ("Hi Fulano,", "Rafael, boa tarde, tudo bem?"), assinatura curta no
  fim ("… Att,") e caracteres invisíveis (zero-width). A remoção de "Nome," só ocorre quando
  seguida de cordialidade (evita cortar cláusula técnica que começa com vírgula).
- **Filtro por CONTEÚDO, não por código (`ingest_tickets.py`):** o critério de "indício
  técnico" foi REMOVIDO. Descarta-se por ausência de explicação: (a) **pedido** ao cliente
  ("favor validar/verificar/confirmar", "aguardo retorno") — nunca é solução; (b) **poucas
  palavras** (< ~8 de conteúdo, após limpeza); (c) **encerramento genérico** ("foi feita a
  correção", "conforme solicitado"…) em texto curto (< ~15 palavras, sem descrição). Prosa
  técnica longa passa mesmo sem código. Limiares são constantes ajustáveis.
- **Amostra para calibração:** o resumo imprime a contagem e uma amostra de **10 mantidos e
  10 descartados** (com o motivo), para leitura e ajuste fino humano.
- **Métrica honesta = leave-one-out (`avaliar_realista.py`):** o auto-match (100%) só prova
  que o embedding é determinístico; a avaliação realista (par excluído de si mesmo) é a que
  reflete um chamado novo e guia a calibração.

## ADR-014 — Documentação oficial TOTVS como segunda fonte de conhecimento

- **Contexto:** a avaliação realista mostrou que a base de chamados sozinha não cobre
  problemas ÚNICOS (sem par similar). Adicionamos a documentação oficial TOTVS como segunda
  fonte, começando enxuto: ingestão de arquivos curados à mão em `docs_totvs/`.
- **Schema:** `conhecimento` ganhou `fonte TEXT NOT NULL DEFAULT 'ticket'` e `titulo TEXT`
  (init.sql + `ALTER TABLE ADD COLUMN IF NOT EXISTS` para o banco existente). Chamados
  entram com `fonte='ticket'` (default); documentação com `fonte='documentacao'`.
- **`scripts/ingest_docs.py` (async, idempotente por hash de arquivo+trecho):** lê `.md`/`.txt`/
  `.docx` de `docs_totvs/`. O `.docx` é lido pela **stdlib** (`zipfile` + `xml.etree`) — sem
  dependência nova (respeita os Padrões); headings do Word viram `#` e o texto segue o mesmo
  parser. Se o artigo tem estrutura da Central de Atendimento (blocos "Dúvida"/
  "Solução"), mapeia `problema=Dúvida`, `solucao=Solução`; se for documento corrido (TDN),
  fatia por seção em trechos de ~500–800 tokens e guarda o trecho na busca E no contexto.
  `titulo` = título do artigo/seção. Embede com `input_type="document"` (reaproveita o
  `VoyageClient`). Idempotência via checkpoint `docs_state.txt` (hash sha256 de arquivo+trecho).
- **Recuperação e contexto:** `RagRepository.buscar_similares` retorna `fonte`/`titulo`; o
  `claude_client` rotula cada item do `<contexto>` com a origem ("Fonte: Documentação oficial
  TOTVS — {titulo}" ou "Fonte: Chamado anterior #{ticket_id}"). O prompt de sistema ganhou a
  regra: em conflito, **priorizar a documentação oficial** sobre chamado antigo — mantendo a
  regra de ouro (só responder pelo `<contexto>`).
- **Avaliação:** `avaliar_realista.py` imprime a fonte de cada item recuperado, para ver se os
  artigos oficiais são puxados nos problemas únicos.
- **Não aplicamos o filtro de baixo valor da ADR-013 à documentação:** os artigos são curados
  à mão; o único descarte é trecho vazio.
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

## ADR-015 — Busca web como último recurso, restrita aos domínios oficiais TOTVS

- **Motivação:** a avaliação realista foi primeiro **corrigida** (consultas passam a vir só
  de `fonte='ticket'`; documento é alvo de recuperação, nunca consulta — um artigo grande
  casava com trechos de si mesmo e derrubava a distância à toa). Com a métrica honesta
  (métricas novas: quantas consultas têm top-1 = documento; distância top-1 por fonte),
  confirmou-se que mesmo com chamados + docs há problemas SEM cobertura (top-1 a distância
  ~0.6). Para esses órfãos, adicionamos uma TERCEIRA fonte, acionada só quando a base falha.
- **Gatilho (nunca antes):** a busca web só dispara quando o pipeline ESCALA por FALTA DE
  CONTEXTO (`decisao == ESCALAR and not resposta.encontrou_solucao`) E a flag
  `BUSCA_WEB_ATIVA` (default `false`) está ligada. Se a base local resolveu — ou escalou por
  confiança baixa (achou algo, mas incerto) — a web NÃO é chamada.
- **`app/busca_web.py` — `BuscaWebClient` (async, best-effort):** consulta o DuckDuckGo via
  `ddgs` restringindo aos dois domínios oficiais com o operador `site:`
  (`site:centraldeatendimento.totvs.com OR site:tdn.totvs.com`); abre os 2–3 primeiros
  resultados via `httpx` (timeout curto + delay entre requisições) e extrai o texto principal
  com `trafilatura`. Qualquer falha (rate limit, bloqueio de IP, página fora do ar, HTML sem
  conteúdo) devolve **lista vazia sem levantar** — a web nunca derruba o processamento.
- **`ddgs` verificado por introspecção (não assumir):** é o sucessor não-oficial do
  `duckduckgo-search`; a assinatura foi confirmada na versão instalada —
  `DDGS().text(query, region, safesearch, max_results) -> list[dict]`, com chaves opcionais
  `{title, href, body}` lidas com `.get()`.
- **Cache em memória** por hash sha256 do problema normalizado (minúsculo, espaços
  colapsados): chamados idênticos não repetem a consulta (economia/velocidade). O
  `BuscaWebClient` é criado uma vez no `lifespan` e reaproveitado entre chamados.
- **Regra de ouro mantida:** os trechos web entram no MESMO `<contexto>`, marcados como
  `fonte='web_totvs'`. O `claude_client` rotula a origem como "Busca web em site oficial
  TOTVS (MENOS verificada — exige revisão humana redobrada)"; o prompt de sistema prioriza,
  em conflito, Documentação oficial > Chamado anterior > Busca web, e pede confiança conservadora.
- **Decisão do caminho web ≠ base local:** como o rascunho web já sai marcado como menos
  verificado (revisão humana obrigatória na Fase 1), o caminho web NÃO aplica o
  `confianca_minima` estrito — basta o Claude ter encontrado solução nos trechos web para
  virar rascunho. Sem isso, a instrução de confiança "media" para a web colidiria com
  `CONFIANCA_MINIMA=alta` e a feature nunca produziria rascunho (bug pego no teste).
- **Notificações:** nota interna e WhatsApp deixam explícito quando o rascunho veio de busca
  web ("🌐 … fonte MENOS verificada — revise com atenção redobrada"), para o responsável
  saber que precisa de um olhar mais atento na revisão.
- **Novas dependências (aprovadas pelo usuário):** `ddgs==9.14.4`, `trafilatura==2.1.0`.
- **Testes (busca e fetch MOCKADOS, zero rede):** unidade do `BuscaWebClient` (extrai texto,
  restringe aos domínios, ignora páginas curtas/fora do ar, best-effort vazio em falha, cache
  não repete, roteamento Zendesk→API) + pipeline (base local resolve → web não chamada;
  escala por falta de contexto → web traz conteúdo → responde ancorado; web vazia → escala;
  flag off → nunca chama).
- **Correções encontradas ao rodar a busca de verdade (2 achados reais):**
  1. **Central de Atendimento (Zendesk) devolve 403 ao HTML** (proteção anti-bot por
     fingerprint TLS — nenhum header resolve). O TDN responde HTML normal. Solução: para
     artigos Zendesk (`/hc/.../articles/{id}`), buscar o corpo pela **API pública de Help
     Center** (`/api/v2/help_center/articles/{id}.json`) — mesmo domínio oficial, sem bloqueio;
     o `article.body` (HTML) é reembrulhado e passa pelo trafilatura. Confirmado ponta-a-ponta.
  2. **TDN derruba conexões em rajada** (falhas transitórias de transporte em requisições
     sequenciais). Solução: `tenacity` no fetch (3 tentativas, backoff curto) SÓ para falhas
     transitórias (erro de transporte/timeout ou HTTP 5xx); 4xx é determinístico e não
     re-tenta. Alinha com o Padrão "retry em toda chamada externa" sem quebrar o best-effort.

## ADR-016 — Achados operacionais do 1º fluxo web ao vivo (max_tokens + banco de testes)

- **`max_tokens` 1024 → 2048 (`claude_client`):** rodando o fluxo web de verdade, respostas
  ancoradas que resumem procedimentos de configuração dos artigos oficiais **truncavam** em
  1024 (`stop_reason=max_tokens`), devolvendo `tool_use` com `input` incompleto →
  `RespostaIA` inválida. No pipeline isso degrada para escala/fallback (a resposta se perde).
  A resposta real usou ~1005 tokens; com `temperature=0` o modelo encerra ao terminar, então
  o teto maior não é gasto à toa. É um desvio consciente do 1024 do Módulo 5, com evidência.
- **Isolamento do banco de testes (footgun corrigido):** os testes de integração fazem
  `DELETE FROM conhecimento/chamado_processado`. Rodá-los no MESMO banco da aplicação apagava
  a base ingerida (paga em Voyage) a cada `pytest` — foi o que aconteceu. Passamos a usar um
  banco **dedicado** `suporte_totvs_test` (default do `conftest` e do `.env.example`; criado
  com o `db/init.sql`).
- **Dívida quitada — idempotência da ingestão pelo BANCO (antes: checkpoints `*_state.txt`):**
  os arquivos de estado local dessincronizavam quando o banco era recriado (o script "pulava"
  itens que não estavam mais no banco — foi o que obrigou a limpar os `*_state.txt` à mão nas
  re-ingestões). Agora a **única fonte da verdade é a tabela `conhecimento`**:
  - `ingest_tickets` carrega os `ticket_id` já presentes (`RagRepository.ticket_ids_ingeridos`)
    e pula os que já estão; chamados sem solução/descartados NÃO ficam na base e são
    reavaliados na próxima rodada (podem ter ganho uma resposta pública depois).
  - `ingest_docs` consulta, por trecho, se já existe um `(titulo, problema)` igual
    (`RagRepository.doc_ja_ingerido`) — a chave estável do trecho, gravada na própria linha.
  - Removidos: `ingest_state.txt`/`docs_state.txt` (e do `.gitignore`), `hash_trecho`,
    `carregar_processados`/`marcar_processado`, `carregar_hashes`/`marcar_hash`. Efeito: à
    prova de recriação do banco — zerou o banco, a próxima ingestão repopula sozinha.

## ADR-017 — Coletor da Central de Atendimento TOTVS (`scripts/coletar_central.py`)

- **Objetivo:** baixar em volume os artigos oficiais e salvá-los LIMPOS em `docs_totvs/`
  (título + corpo), prontos para o `ingest_docs` consumir. Conteúdo legítimo (acesso de
  parceiro TOTVS).
- **API do Zendesk, não raspagem:** a API pública do Help Center responde —
  `/api/v2/help_center/pt-br/{categories,sections,articles}.json`, JSON paginado (segue
  `next_page`). Muito mais estável/limpo que raspar HTML página a página.
- **Pivô de fonte (verificado antes de coletar):** começamos apontando para
  `totvscst.zendesk.com`, mas ele só tem 3 categorias administrativas (Adm. e Financeiro,
  Documentações, Notícias) — portal de CONTA/faturamento, não de módulos do ERP. Redirecionamos
  para **`centraldeatendimento.totvs.com`** (a Central de Atendimento TÉCNICA): confirmado que
  é Zendesk (headers `x-zendesk-origin-server`, atrás de Cloudflare), a API responde e o
  `robots.txt` permite os endpoints. Tem 27 categorias por SEGMENTO; a categoria **"Cross
  Segmentos" (id 360005280714) concentra 650 seções** de módulos do Backoffice, e **"TOTVS RH"**
  tem categoria própria. `BASE`/`ALVOS`/`LINHA_FILTRO` são constantes fáceis de editar.
- **Temas → seções (por padrão no NOME da seção; `Alvo` tipo `filtro`):** financeiro (62
  seções), fiscal (75), relatórios (25), nf (19), estoque (16), compras (10) em Cross Segmentos;
  RH pela categoria própria. Os padrões (regex) são aproximados e ficam fáceis de calibrar.
- **Multi-linha → filtro de linha:** "Cross Segmentos" mistura Protheus/RM/Datasul/Logix.
  Como o foco é Protheus, `LINHA_FILTRO="Protheus"` mantém só artigos cujo TÍTULO contenha a
  linha (`--linha ""` desliga). Ex. de artigo Protheus coletado: SIGAATF (Ativo Fixo) —
  parâmetros `MV_VLATFCT`/`MV_VLRATF`, tabelas `SF4`/`SD1`, campos `D1_IDTRIB`, caminho de
  rotina e exemplo de cálculo. Técnico de verdade, não administrativo.
- **Extração robusta:** o `body` do artigo é HTML customizado (CSS/JS embutidos; às vezes um
  "shell" que carrega o conteúdo por JavaScript). `lxml` dropa `script/style/nav/button/...`;
  promovemos headings e RÓTULOS grudados ("DúvidaComo…", "…Solução") a `## ` para o
  `ingest_docs` separar problema (Dúvida/Ocorrência) da solução; corpos < 200 chars (shells)
  são pulados. Também pulamos `draft=true`.
- **Boas maneiras (obrigatório):** respeita `robots.txt` (`urllib.robotparser` — verificado
  que os endpoints da API são permitidos), User-Agent identificável e pausa de ~1,5 s entre
  requisições. Coleta comedida, uma vez.
- **Saída idempotente:** `{tema}__{id}__{slug}.txt` = `# {título}` + corpo limpo. Resume
  pulando o que já existe (`{tema}__{id}__*.txt`) — o próprio `.txt` é o estado (sem
  checkpoint à parte; consistente com ADR-016). O `ingest_docs` depois deduplica no banco.
- **Autenticação:** prioriza conteúdo público; para seções logadas, reaproveita cookies de
  sessão do navegador via `ZENDESK_COOKIE` (env) ou `zendesk_cookies.txt` — sem automatizar
  senha/captcha.
- **Modo teste:** `--secao/--categoria` + `--limite N` + `--saida` + `--linha` (validado na
  seção fiscal Protheus "Ativo fixo - Reforma Tributária": 2 artigos → texto limpo, split
  Dúvida/Solução correto, conteúdo técnico Protheus). NÃO rodamos a coleta em massa — só o
  teste, para o usuário conferir antes de liberar para `docs_totvs/`.
- **Dependência:** `lxml==6.1.1` (já vinha via `trafilatura`; fixada por ser importada
  diretamente). Testes com a API MOCKADA (respx) — zero rede.
- **Coleta fiscal (1º tema, autorizada):** 2.357 artigos Protheus salvos em `docs_totvs/`
  (2.700 de outras linhas descartados pelo filtro); ingeridos como 3.352 trechos. Base:
  3.375 documentação + 25 chamados.
- **Achado na avaliação — 40,6% dos trechos fragmentados (corrigido):** o `avaliar_realista`
  mostrou ganho de cobertura (top-1 = doc em 4/10 consultas; artigos fiscais relevantes
  puxados para os órfãos), MAS 1.370/3.375 trechos tinham `titulo` genérico ("Ambiente",
  "Ocorrência", "Solução"…). Causa: o `ingest_docs` só reconhecia **Dúvida→Solução**, e
  muitos artigos TOTVS usam **Ocorrência→Solução** → caíam no fatiador e viravam vários
  trechos rotulados, com título inútil e par problema/solução quebrado. Correção: os
  marcadores viraram `_MARCADOR_PROBLEMA` (Dúvida|Ocorrência|Problema) e `_MARCADOR_SOLUCAO`
  (Solução|Resolução|Procedimento) — vale para todos os temas. **Consertar os dados já
  ingeridos exige re-ingerir** (apagar `fonte='documentacao'` + `ingest_docs`), que re-embeda.
- **Re-ingestão fiscal com o parser corrigido:** fragmentação caiu **40,6% → 23,4%** (3.352 →
  2.808 trechos; títulos reais na maioria; distância do #4220 "Saldo divergente" 0,60 → 0,45;
  cobertura top-1=doc mantida em 4/10).
- **Resíduo (23%) e fix no coletor:** o diagnóstico mostrou que 361 artigos ainda fatiavam por
  "faltar `## Solução`" — o rótulo vinha **espaçado** (o coletor só promovia rótulos GRUDADOS).
  Fix: `_promover_rotulos_html` no coletor promove rótulos que são um ELEMENTO próprio
  (`<strong>Solução</strong>`, `<p>Ocorrência</p>`) a `## `, confiável mesmo espaçado.
  Verificado num artigo real (DIME SC): virou 1 trecho com título real. **Aplica-se aos
  próximos temas**; o fiscal já ingerido ficou como está (funcional; re-coletar é opcional).
- **Lote NF + Financeiro (2º lote) — 2 correções ao rodar de verdade:**
  1. **Bug do comentário HTML:** `_promover_rotulos_html` iterava `doc.iter()` e chamava
     `.text_content()` num nó `HtmlComment` → `ValueError` que derrubava a coleta inteira do
     NF. Fix: `if not isinstance(el.tag, str): continue` (pula comentários/PIs).
  2. **Filtro de linha apertado demais (o usuário previu):** `LINHA_FILTRO="Protheus"` comia
     Protheus real titulado "MP - SIGAFIN…" (MP = Microsiga Protheus) ou "Cross Segmentos –
     SIGAFIN". Virou um **regex** `protheus|microsiga|siga[a-z]{3}` — `SIGAxxx` é código de
     módulo exclusivo do Protheus. Recuperou 116 artigos de Financeiro; os descartes passaram a
     ser corretos (NF: 1.192 "Linha RM"; Financeiro: 72 Logix/Datasul/CRM). O coletor agora
     registra amostra de títulos descartados (`exemplos_descartados`) para auditoria.
  - **Volume (com filtro Protheus):** NF 807 artigos → 824 trechos (4,1% frag); Financeiro
    1.997 → 2.071 (5,9%). Fragmentação baixa confirma o fix do coletor. Ver COLETA.md.
- **Lote Compras + Relatórios + Estoque (3º lote):** Compras 1.351 art (509 "Linha RM"
  descartados) → 1.393 trechos; Relatórios 343 (211 CRM/SFA) → 359; Estoque 44 (442
  Datasul/RM) → 44. Fragmentação 0–7%. Descartes conferidos: corretos. Estoque rende pouco
  Protheus (44) porque a base de estoque é majoritariamente RM/Datasul — não é o filtro.
- **RH — coleta CIRÚRGICA por subtema (decisão do usuário, opção b):** "TOTVS RH" tem 12.568
  artigos / 379 seções, só ~6% Protheus — baixar tudo é desperdício. Novo `Alvo` tipo `filtro`
  restrito às seções de RH-Protheus (`\bGPE\b|folha|f[eé]rias|ponto|cargos|sigagpe|sigapon`);
  o filtro de linha isola o Protheus. Rendeu **1.391** Protheus (acima da estimativa de ~750),
  descartando os produtos cloud (Pontoweb/Velti/Feedz/Ahgora). Fragmentação 1,7%.
- **Contador de descartados:** `ResumoColeta.exemplos_descartados` guarda amostra de títulos
  cortados pelo filtro de linha, impressa no fim — para auditar de olho se não come Protheus.
- **Fechamento da fase de coleta (7 temas):** base = **8.274 trechos** de documentação + 25
  chamados = 8.299; fragmentação global **10,6%**. Cobertura (top-1 = documentação nos 25
  chamados) evoluiu **4/10 (fiscal, amostra) → 9/25 (+NF+Fin) → 11/25 = 44% (7 temas)**, com a
  distância média dos docs caindo 0,585 → 0,476. Coleta em massa **encerrada**. Pendência
  OPCIONAL, não priorizada: re-coletar o **fiscal** (23,9% de frag, coletado antes do fix de
  rótulos) — o conteúdo já é recuperado, então fica documentado como melhoria futura. Status e
  volume por tema em **COLETA.md**.

## ADR-019 — Interface de teste local (painel de inspeção do pipeline)

- **Objetivo:** porta de entrada VISUAL para testar o sistema antes de produção — colar o
  texto de um chamado e ver o que o pipeline FARIA, SEM tocar no Freshdesk nem enviar WhatsApp.
- **Regra central (não reimplementar):** a interface chama o MESMO pipeline. Extraímos o MIOLO
  de `processar()` para `pipeline.inspecionar(ticket, *, settings, rag_service, claude,
  busca_web)` (RAG → Claude → decisão → busca web). `inspecionar` **NÃO recebe Freshdesk nem
  WhatsApp** — por construção não há como escrever nota ou enviar mensagem. O `processar()`
  virou `inspecionar` + I/O (nota/atribuição/WhatsApp), então a lógica vive num só lugar (os
  testes do webhook seguem passando idênticos).
- **`Inspecao` (retorno sem efeitos):** traz tudo que a tela mostra — problema limpo, pares
  recuperados (fonte/título/distância, p/ auditar isolamento por empresa), rascunho, decisão +
  confiança, nota que SERIA criada, WhatsApp que SERIA enviado, e se a busca web foi acionada
  (com os trechos `web_totvs`).
- **Rotas (gated):** `GET /teste` serve o painel (HTML em `app/teste.html`, fora do `.py` p/
  não ser lintado) e `POST /teste/processar` (contrato Pydantic `TesteRequest`/`TesteResposta`)
  roda `inspecionar` e devolve JSON. Campo opcional "empresa" (texto colado não vem do
  Freshdesk). Gated por **`INTERFACE_TESTE_ATIVA`** (default false): a tela expõe os pares
  recuperados, então NÃO deve ficar ligada em produção.
- **Windows/uvicorn (achado ao rodar de verdade):** o uvicorn instala o `ProactorEventLoop`,
  incompatível com o psycopg async (sobrescreve o `set_event_loop_policy` do import). Criado
  `run_local.py`, que fixa `WindowsSelectorEventLoopPolicy` ANTES do `uvicorn.run(...,
  loop='asyncio')` (sem subprocess, p/ o uvicorn não sobrescrever). Em Linux é no-op.
- **Testes (zero rede, sem Freshdesk/WhatsApp):** unidade de `inspecionar` (resolvido/escalar/
  web) — sem clientes de saída; helper da rota contra o banco de TESTE com Voyage/Claude fakes
  e `state` SEM freshdesk/whatsapp (se usasse, daria AttributeError); página + gate. Verificado
  AO VIVO: `/teste` 200 e `/teste/processar` retornou RESOLVIDO com 5 docs Protheus ancorando o
  rascunho, sem efeito real.

## ADR-020 — Tom da resposta ao cliente separado do processo interno

- **Problema (visto na interface de teste):** a `resposta_cliente` vazava o processo interno
  ("não encontrei na base de conhecimento"), passando incompetência ao cliente.
- **Decisão — dois públicos, dois tons (no SYSTEM_PROMPT):** `resumo_para_responsavel` é para o
  TIME (verdade técnica crua); `resposta_cliente` é para o CLIENTE e JAMAIS revela COMO foi
  produzida — proibido mencionar "base de conhecimento", "IA", "não encontrei", "os artigos
  disponíveis", "com base na análise". A **regra de ouro anti-alucinação segue intacta**
  (responder só pelo `<contexto>`; `encontrou_solucao=false` se não estiver lá).
- **Tom por cenário:** resolver (`encontrou_solucao=true`) → resposta DIRETA e objetiva, como
  técnico que sabe a resposta; escalar (`false`) → ACOLHIMENTO ("seu chamado está sendo analisado
  pelo nosso time e retornaremos em breve") + recomendações seguras do contexto; nunca dizer que
  não achou nada.
- **Pedido operacional:** novo campo `RespostaIA.pedido_operacional` (obrigatório) — o Claude
  sinaliza quando o chamado é TAREFA a EXECUTAR por uma pessoa (cadastro/liberação/ajuste; ex.:
  incluir cadastro na SX5). Aí a resposta_cliente acolhe e sinaliza execução ("vamos providenciar
  … retornamos assim que concluído"); o sistema escala (nota + WhatsApp) p/ o responsável
  executar; a busca web NÃO dispara (execução humana, não busca).
- **Time continua vendo a verdade:** a nota interna mantém a linha técnica ("⚠️ IA não encontrou
  solução na base. Requer análise manual.") + resumo, e AGORA anexa o "Rascunho de acolhimento ao
  cliente (revisar antes de enviar)" — para o agente enviar o texto acolhedor (Fase 1 copiloto).
  O fallback `_resposta_sem_contexto` também acolhe sem revelar o processo.
- **Interface:** `TesteResposta` expõe `pedido_operacional` (selo na tela) para auditar a
  classificação do Claude.
- **Testes + verificação AO VIVO:** prompt (proíbe vazamento, mantém regra de ouro); fallback
  acolhe sem revelar; parse do pedido_operacional; 3 cenários no pipeline (resolver direto;
  escalar com acolhimento + nota técnica; pedido operacional acolhe/escala/não-web). Na
  interface: pedido operacional e escala saíram acolhedores, SEM vazar processo, nota técnica
  preservada.

## ADR-021 — Coleta dirigida operacional + prova do limiar 0,40 (negativo importante)

- **Hipótese testada:** para "pedidos operacionais" (cadastro/liberação), ensinar o how-to em
  vez de escalar. Requer que o procedimento esteja na base. O diagnóstico apontou os temas de
  maior retorno: **acesso (FATA900)**, **cadastro-produto (MATA010)** e tributação (MATA020).
- **Coleta (novo tipo `busca` em ALVOS):** busca por ROTINA na API de search do Zendesk (a
  rotina aparece no TÍTULO, não no nome da seção), filtrada pela linha Protheus. Coletados e
  ingeridos **51 trechos** (acesso 20 arquivos/21 trechos; cadastro-produto 30/33). Base → 8.325.
- **tributação = gap de FONTE, não de coleta:** a Central **não tem** o MATA020 grupo de
  tributação Protheus (busca devolve ADVPL/SIGAFAT). Está só no **TDN**. `Alvo` retirado de
  ALVOS (traria ruído); follow-up = coletar do TDN (fora do escopo deste coletor).
- **Medição antes/depois (rigorosa, mesmo ticket, corte de `id`):** em 13 operacionais reais do
  Freshdesk. **Conteúdo VALIDADO** — query limpa "Como cadastrar um produto novo no Protheus"
  recupera o MATA010 novo em 1º lugar, **d=0,3124**. **Mas flip realizado ≈ 0**: ensináveis
  (how-to < 0,40) antes 0/13, depois 0/13 (texto cru); 1/13 com intenção reformulada por Haiku.
- **Achado honesto (muda o plano):** a **frequência-por-tema superestimou o volume ensinável**.
  Os chamados reais sob acesso/cadastro são em maioria **senha/VPN/SEFAZ/Smart View/erro
  específico** — não o how-to canônico. Some-se o ruído do texto cru (assinatura/CAIXA ALTA)
  que infla a distância (~0,54 vs ~0,31 na intenção limpa). O gap remanescente é de **query e
  especificidade de intenção**, não de conteúdo faltante.
- **Decisão:** manter os 51 docs (conteúdo bom, custo desprezível). **Não construir o portão
  0,40** com a premissa de alta cobertura operacional — ela não se confirmou neste fluxo. Levers
  candidatos, a decidir: (1) reformulação de query antes do RAG (ganho modesto, ~query hygiene);
  (2) conteúdo mais específico (variantes reais); (3) reavaliar o ROI do portão. Medir antes de
  construir — a prova pedida trouxe um negativo, e ele vale.

## ADR-022 — Tom do ESCALAR apertado: texto-modelo fixo ao cliente (aperto da ADR-020)

- **Problema (teste real):** mesmo após a ADR-020, a `resposta_cliente` no caminho ESCALAR
  ainda "vazava IA" — dizia o que consta/não consta na base e PEDIA que o cliente investigasse
  (versão, mensagem de erro, passos). Isso passa incompetência e quebra a Fase 1 copiloto.
- **Decisão — texto-modelo FIXO, sem exceção:** quando `encontrou_solucao=false` (todo ESCALAR,
  inclusive `pedido_operacional`), a `resposta_cliente` é SUBSTITUÍDA em código pela constante
  `RESPOSTA_ESCALAR_PADRAO` = "Olá! Seu chamado está sendo analisado pelo nosso time e
  retornaremos em breve." — nada além disso. Não se deixa mais o modelo gerar esse texto.
- **Ponto único da garantia:** o override vive em `claude_client._acolhimento_padrao_se_escala`,
  aplicado em `gerar_resposta` (e o fallback `_resposta_sem_contexto` usa a mesma constante).
  Assim TODO consumidor da `RespostaIA` (nota, WhatsApp, interface) recebe o texto já saneado —
  impossível vazar no escalar. No RESOLVER (`encontrou_solucao=true`) o texto do modelo é
  preservado (entrega direta da solução).
- **Análise técnica só ao time:** o SYSTEM_PROMPT agora manda toda a análise (versão a checar,
  hipóteses, o que investigar) EXCLUSIVAMENTE para `resumo_para_responsavel`; proíbe, na
  `resposta_cliente` do escalar, dar solução, pedir verificação de versão/erro ou listar passos.
  A nota interna segue com a verdade técnica crua + esse resumo.
- **Testes:** escalar força a constante e reprova qualquer vazamento (base/versão/verifique/
  erro/passos) mesmo quando o modelo tenta vazar; o resumo técnico é preservado; pedido
  operacional também cai na saudação-padrão; prompt exige a análise só no resumo.

## ADR-023 — Leitura de imagens dos chamados (visão) na entrada do pipeline

- **Contexto:** a maioria dos chamados chega por e-mail com PRINTS de erro (logs, mensagens,
  códigos) em anexo. Sem lê-los, a busca ignora o sinal mais forte do problema.
- **`app/visao.py` (VisaoClient, async):** recebe a imagem e usa o Claude (Haiku, visão) para
  TRANSCREVER só o texto legível (logs/erros/códigos/tabelas). **Regra de ouro:** transcreve
  apenas o legível; se ilegível/sem texto útil/tipo não suportado, devolve string VAZIA — nunca
  interpreta, descreve ou inventa. Retry com tenacity; `temperature=0`.
- **`freshdesk.py`:** `TicketFreshdesk` passa a expor `attachments`/`imagens` (novo modelo
  `Anexo`); `baixar_anexo(url)` baixa a `attachment_url` pré-assinada (S3) SEM a auth Basic do
  Freshdesk (mandar auth pode invalidar a assinatura).
- **`pipeline.py` — `_incorporar_imagens` (BEST-EFFORT):** no `processar` (webhook), após ler o
  chamado, transcreve as imagens (teto de `_MAX_IMAGENS=4`) e CONCATENA à `description_text`
  ANTES da busca — vira parte da query do RAG e do contexto do Claude. Falha ao baixar/transcrever
  uma imagem é ignorada (não derruba o chamado). O miolo `inspecionar` continua SEM Freshdesk
  (ADR-019): o download mora só no `processar`, que já tem o cliente Freshdesk; a interface de
  teste (texto colado) segue inalterada.
- **Config/flag:** `LEITURA_IMAGENS_ATIVA` (default **true**) liga a funcionalidade para chamados
  novos; `false` pula a leitura. `VisaoClient` é criado no `lifespan` e injetado no `processar`.
- **Testes (visão mockada, zero rede):** imagem com texto → concatenado à query do RAG (prova de
  ponta no `processar`); imagem ilegível → vazio, segue sem ela; falha no download → best-effort,
  ticket intacto; sem anexo / sem VisaoClient / flag off → fluxo inalterado; `baixar_anexo` vai à
  URL pré-assinada sem auth; VisaoClient normaliza tipo, ignora tipo não suportado/bytes vazios.

## ADR-024 — Reformulação de query antes do RAG, por UNIÃO com o texto limpo

- **Contexto (lever apontada no COLETA.md):** o texto cru do chamado — mesmo após o `limpar_texto`
  (ADR-011/013) — ainda infla a distância da busca (assunto em CAIXA ALTA, "dá erro" vago, ruído de
  e-mail). A intenção limpa (`"Como cadastrar um produto no Protheus"`) cai a ~0,31; o mesmo caso cru
  fica a ~0,54. A hipótese: reescrever o chamado em INTENÇÃO de busca antes do embedding aproxima a
  documentação.
- **Medição (25 chamados reais, leave-one-out, `scripts/avaliar_reformulacao.py`), 3 braços:**
  baseline = o texto **já limpo** que o pipeline busca hoje (não remede o ganho da limpeza regex,
  que já existe; mede só o ganho INCREMENTAL). Resultado, separado por fonte do vizinho:
  | Braço | d_doc (documentação) | d_ticket (chamado) | ensináveis (d_doc<0,40) | top-1 = doc |
  |---|---|---|---|---|
  | ANTES (texto limpo) | 0,5046 | **0,3493** | 0/25 | 11/25 |
  | DEPOIS (só reformulada) | 0,4755 | 0,4632 ⬇ | 2/25 | 20/25 |
  | **UNIÃO (as duas)** | **0,4686** | **0,3414** | 2/25 | 20/25 |
- **Achado decisivo — reformular sozinho é uma TROCA, não um ganho:** aproxima a documentação
  (−0,029) mas **afasta os chamados anteriores em +0,11**. Chamado anterior carrega a *solução real
  de um agente* — o material mais valioso da base. Ex. real (#4214): o vizinho mais próximo era um
  chamado a **0,1802** (quase o mesmo problema já resolvido); a reformulação o jogou para 0,51.
- **Decisão — UNIÃO, não substituição:** buscar com o texto limpo **E** a intenção reformulada e unir
  por MENOR distância por trecho. A documentação responde melhor à intenção; o chamado, ao texto cru.
  A união domina os dois braços: docs ainda mais perto (0,4686, melhor dos três) e chamados
  preservados (0,3414). Top-1 = doc sobe 11/25 → 20/25, com **flips ganhos 2, perdidos 0**.
- **`RagService.buscar_uniao(queries, k)`:** embeda cada query única/não-vazia, busca top-k de cada e
  une deduplicando por trecho (`_identidade`: chamado por `ticket_id`; doc por `(fonte, titulo,
  problema)`), mantendo a menor distância. Uma query só → colapsa na `buscar` simples. O `buscar`
  antigo fica intacto.
- **A reformulação alimenta SÓ o embedding — NUNCA a resposta:** o `gerar_resposta` continua
  recebendo o `problema` ÍNTEGRO (a query é compressão com perda: boa para buscar, ruim para
  responder). Logo uma reformulação ruim degrada a recuperação, mas é **incapaz de alucinar** — a
  regra de ouro segue ancorada nos pares recuperados.
- **Garantia de código técnico EM CÓDIGO (não no prompt):** `texto.extrair_codigos_tecnicos` acha os
  identificadores TOTVS (MV_*, B1_COD, SIGAFIN, MATA010, SCC19070, SX5); `claude_client._preservar_codigos`
  reinjeta na query qualquer código que o modelo tenha descartado ao reescrever. É o sinal mais
  discriminante da busca — mesmo espírito do `_acolhimento_padrao_se_escala` (ADR-022): a garantia
  não confia no modelo. Prova ao vivo: e-mail ruidoso com SCC19070/MV_ATFMOED → query
  `"Erro SCC19070 ao lançar nota fiscal parâmetro MV_ATFMOED ativo fixo"`, ambos preservados,
  decisão RESOLVIDO.
- **`claude_client.reformular_query`:** tool use forçado (`registrar_query` → schema `QueryReformulada`),
  `temperature=0`, prompt próprio que PROÍBE responder o chamado e manda preservar código e descartar
  nome/empresa/saudação. Texto curto (<10 chars) ou reformulação degenerada → devolve o `problema`
  original (mais seguro). Retry `tenacity`, como as demais chamadas.
- **Best-effort no pipeline:** `_query_de_busca` — falha na reformulação NUNCA derruba o chamado, cai
  no texto limpo (= comportamento pré-ADR-024). Config `REFORMULAR_QUERY_ATIVA` (default **true**);
  `false` pula e a união colapsa numa busca só.
- **Interface de teste:** `Inspecao.query`/`TesteResposta.query` expõem a query reformulada; o `/teste`
  mostra a intenção buscada e o texto íntegro que gera a resposta, lado a lado.
- **Testes (zero rede/paga):** união mantém a menor distância por trecho e deduplica; query
  repetida/vazia não busca duas vezes; extrator cobre parâmetro/campo/módulo/rotina/erro/tabela e
  NÃO casa assunto em CAIXA ALTA sem código; reformulação reinjeta código descartado sem duplicar;
  degenerada/curta cai no original; pipeline busca as duas e responde com o problema; flag off/falha
  colapsa a união. **167 passando, ruff limpo.**
