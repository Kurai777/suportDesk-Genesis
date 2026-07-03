# CLAUDE.md — Suporte TOTVS IA (Genesis Consulting)

> Arquivo de contexto do projeto. O Claude Code deve ler este arquivo antes de
> qualquer tarefa e seguir as "Regras de código (invioláveis)" e os "Padrões de
> Engenharia" à risca.

---

## O QUÊ (What)

Serviço em Python (FastAPI) que:

1. Recebe, via **webhook**, cada chamado novo aberto no Freshdesk (suporte a **TOTVS Protheus**). A maioria dos chamados chega por e-mail e vira ticket no Freshdesk.
2. Busca soluções numa **base vetorial de conhecimento** (chamados já resolvidos + documentação oficial TOTVS).
3. Usa o **Claude Haiku** para gerar um rascunho de resposta ancorado **exclusivamente** no conteúdo recuperado.
4. **Fase atual = Copiloto:** NUNCA responde o cliente automaticamente. Cria uma **nota interna** com o rascunho, **atribui** o chamado ao responsável e o **notifica no WhatsApp**.
5. Se não houver solução conhecida na base, **escala**: nota interna + WhatsApp de alerta ("não encontrei solução, revise pessoalmente").

---

## POR QUÊ (Why)

- Reduzir o tempo de primeira resposta e padronizar o atendimento.
- Avisar o responsável de cada chamado no WhatsApp (resolvido × precisa de atenção humana).
- **Nunca alucinar:** o cliente opera um ERP em produção; uma instrução errada causa dano real. Por isso o modelo só pode responder com conteúdo TOTVS presente na base recuperada.

---

## COMO (How)

### Stack

- Python 3.11+, FastAPI, Uvicorn
- PostgreSQL + extensão **pgvector** (base vetorial)
- Claude API — modelo `claude-haiku-4-5-20251001` (saída em JSON estruturado via tool use)
- Embeddings — **Voyage AI (`voyage-3`, 1024 dimensões)** — decidido
- WhatsApp — Evolution API (piloto) → migrar para Meta Cloud API (produção)
- Deploy — Railway (serviço + Postgres). Desenvolvimento local: `docker-compose` com `pgvector/pgvector`.

### Estrutura de pastas

```
suporte-totvs-ia/
├── app/
│   ├── main.py            # FastAPI: endpoint do webhook, responde 200 na hora
│   ├── config.py          # variáveis de ambiente (Pydantic Settings)
│   ├── models.py          # schemas Pydantic (contrato de I/O do Claude e do webhook)
│   ├── freshdesk.py       # ler chamado, criar nota interna, atribuir responsável
│   ├── rag.py             # gerar embedding + buscar no pgvector
│   ├── claude_client.py   # chamar o Claude e validar a saída JSON
│   ├── whatsapp.py        # enviar notificação (Evolution/Meta)
│   └── pipeline.py        # o "maestro": RAG → Claude → decisão → ação
├── scripts/
│   └── ingest_tickets.py  # puxa chamados resolvidos → chunk → embed → pgvector
├── tests/
├── db/
│   └── init.sql           # extensão vector + tabela de conhecimento (dev local)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── CLAUDE.md
```

### Regras de código (invioláveis)

1. **Fase 1 = Copiloto.** É PROIBIDO enviar resposta pública ao cliente automaticamente. Apenas `freshdesk.criar_nota_interna()` e `freshdesk.atribuir()`.
2. Toda entrada/saída do Claude passa por **modelos Pydantic** (`models.py`). O Claude deve devolver SOMENTE JSON válido no schema `RespostaIA`.
3. **Regra de ouro (anti-alucinação):** se a solução não estiver no contexto recuperado, `encontrou_solucao=false`. Nunca citar parâmetro, tabela ou caminho que não apareça no contexto.
4. O endpoint do webhook responde `200 OK` imediatamente e processa em background (`BackgroundTasks`), para não estourar o timeout do Freshdesk.
5. Nenhum segredo no código. Tudo em variáveis de ambiente (`.env.example`).
6. Type hints em tudo; funções curtas e testáveis.

### Contrato de saída do Claude (`RespostaIA`)

| Campo | Tipo | Descrição |
|---|---|---|
| `resposta_cliente` | str | Rascunho de resposta ao cliente (revisado por humano na Fase 1). |
| `encontrou_solucao` | bool | `true` só se a solução estiver no contexto recuperado. |
| `confianca` | str | `"alta" \| "media" \| "baixa"`. |
| `resumo_para_responsavel` | str | Resumo curto do caso para o WhatsApp/nota. |
| `empresa` | str | Nome do cliente do chamado. |
| `urgencia` | str | Urgência inferida do chamado. |

### Ingestão da base (detalhe crítico)

O `ingest_tickets.py` deve guardar o **par completo** de cada chamado resolvido:
problema do cliente (incluindo logs de erro, ex.: `SCC19070` / `MV_ATFMOED`) **+**
a resposta do agente que resolveu. É esse par que dá qualidade à busca.

### Variáveis de ambiente (`.env.example`)

```
FRESHDESK_DOMAIN=
FRESHDESK_API_KEY=
FRESHDESK_WEBHOOK_SECRET=
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-haiku-4-5-20251001
VOYAGE_API_KEY=
VOYAGE_MODEL=voyage-3
DATABASE_URL=postgresql://totvs:totvs@localhost:5432/suporte_totvs
WHATSAPP_API_URL=
WHATSAPP_API_KEY=
WHATSAPP_RESPONSAVEL_DEFAULT=
CONFIANCA_MINIMA=alta
```

### Fora de escopo agora

- Resposta automática ao cliente (Fase 2, só após medir a taxa de acerto).
- Integração direta com o Protheus (usamos apenas o texto dos chamados).
- Busca web ao vivo (Fase 2 opcional, restrita a domínios oficiais TOTVS).

---

## Padrões de Engenharia

Estas regras valem para **TODOS os módulos**, em conjunto com as "Regras de código (invioláveis)".

- **Bibliotecas (somente estas; qualquer outra, PERGUNTE antes):** `fastapi`, `uvicorn`, `pydantic` v2 + `pydantic-settings`, `httpx`, `tenacity`, `anthropic`, `voyageai`, `psycopg[binary]` v3 + `pgvector`, `pytest` + `pytest-asyncio` + `respx`, `ruff`.
- **Cliente fino por integração externa:** cada serviço externo fica numa classe-cliente fina (`FreshdeskClient`, `ClaudeClient`, `VoyageClient`, `WhatsAppClient`) que recebe a config por injeção, para ser testável.
- **`pipeline.py` só orquestra:** a decisão "resolvido × escalar" fica numa **função pura, sem I/O**.
- **Saída do Claude via tool use / function calling**, validada contra o schema `RespostaIA`. Usar o método atual do SDK `anthropic`; em caso de dúvida sobre a API, **AVISAR antes de assumir**.
- **Retry em toda chamada externa:** toda chamada a API externa é envolvida em retry com `tenacity` (backoff exponencial, poucas tentativas).
- **Endpoint do webhook:** valida um token secreto no header (`FRESHDESK_WEBHOOK_SECRET`), responde `200 OK` imediatamente, processa em `BackgroundTasks` e é **idempotente por `ticket_id`**.
- **Segredos só em variáveis de ambiente.** Nenhum teste pode gastar chamada real paga — usar `respx` para mockar HTTP.
