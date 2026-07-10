# COLETA.md — Coleta da documentação oficial TOTVS

Status e guia da coleta de artigos da **Central de Atendimento** para `docs_totvs/`, que o
`scripts/ingest_docs` depois ingere na base vetorial. Detalhes de decisão: **ADR-017**.

## ✅ Base de conhecimento — CONCLUÍDA (marco)

Os **7 temas** foram coletados, filtrados por linha (Protheus) e ingeridos. A base saiu de
2,8k para **8.274 trechos** de documentação oficial + 25 chamados. A cobertura (top-1 =
documentação, nos 25 chamados) evoluiu de **4/10 → 11/25 (44%)**, com a fragmentação global
caindo de 23% para **10,6%** e os docs sendo recuperados cada vez mais perto (dist. 0,585 →
0,476). A coleta em massa está **encerrada**.

- **Pendência OPCIONAL (não priorizada):** re-coletar o tema **fiscal** (hoje a 23,9% de
  fragmentação, por ter sido coletado antes do fix de rótulos do coletor). O conteúdo já é
  recuperado normalmente; re-coletar só melhoraria os títulos → não vale o esforço agora.
- Próximos passos (fora da coleta) ficam a definir: leitura de imagens dos chamados ou virada
  para produção.

## Coleta dirigida operacional (ADR-021) — acesso + cadastro-produto

Coleta por **rotina** (API de search do Zendesk, tipo `busca` em ALVOS), mirando os temas
operacionais de maior retorno do diagnóstico. Filtro de linha Protheus aplicado.

| Tema | Rotina | Baixados | Descartados (linha) | Trechos | Fragmentação |
|---|---|---|---|---|---|
| acesso | FATA900 | 20 | 30 (Datasul/RM/CRM/Educacional/Fluig) | 21 | 9,5% |
| cadastro-produto | MATA010 | 30 | 20 (CRM/WINT/Varejo/RH) | 33 | 12,1% |

**Ingeridos:** 51 trechos novos (3 deduplicados por título+problema). Base → 8.325 docs.

**tributação (MATA020):** ❌ a Central **não tem** o how-to Protheus de grupo de tributação — a
busca devolve ADVPL/SIGAFAT (fora do alvo). Está só no **TDN**. Follow-up: coletar do TDN
(o coletor da Central não cobre). O `Alvo` foi retirado de ALVOS para não trazer ruído.

### Medição do limiar 0,40 (prova antes/depois) — resultado HONESTO

Medido em **13 chamados operacionais reais** (acesso 9 + cadastro-produto 4, subject-keyword),
comparando o melhor how-to **com** os 51 docs novos (depois) vs **sem** eles (antes, por corte
de `id`):

- **Conteúdo VALIDADO:** em query limpa `"Como cadastrar um produto novo no Protheus"`, o
  MATA010 novo é recuperado em **1º lugar, d=0,3124** (bem abaixo de 0,40). A base **passou a
  saber** ensinar o cadastro — o conteúdo é bom.
- **Flip realizado ≈ 0:** ensináveis (how-to < 0,40) **antes 0/13, depois 0/13** com texto cru;
  **1/13** com a intenção reformulada por Haiku. Nenhum flip atribuível aos docs novos.
- **Por quê:** os "operacionais" reais desta base, sob os temas acesso/cadastro, são em maioria
  **senha/VPN/SEFAZ/Smart View/erro específico** — não o how-to canônico que MATA010/FATA900
  respondem. Ex.: "Marron esqueceu a senha", "Sem acesso a VPN", "RELATÓRIO SMART VIEW",
  "erro de permissão ao alterar PO". O texto cru ainda infla a distância (assinatura, saudação,
  assunto em CAIXA ALTA): a mesma intenção limpa cai de ~0,54 para ~0,31.

**Conclusão:** a estratégia "ensinar em vez de escalar" é válida **no nível do conteúdo** (a
base agora responde o how-to canônico), mas a **frequência-por-tema superestimou o volume
ensinável** — a maioria dos chamados sob esses rótulos não é o how-to genérico. Levers reais
seguintes: (1) **reformulação de query** antes do RAG (fecha o gap de ruído, ganho modesto);
(2) conteúdo mais **específico** (variantes reais) ou reavaliar o ROI do portão neste fluxo.
Portão **não construído** — decisão pendente à luz desta medição.

> **Update (ADR-024): lever (1) construída e medida.** A reformulação foi implementada por
> **união** (busca o texto limpo + a intenção reformulada, une por menor distância). Em 25
> chamados reais: documentação de 0,5046 → **0,4686**, top-1 = doc de 11/25 → **20/25**, com os
> chamados anteriores preservados (0,3493 → 0,3414) e **flips ganhos 2 / perdidos 0**. Ganho
> modesto, como previsto, mas sem custo colateral. Detalhes e a prova antes/depois: **ADR-024**
> + `scripts/avaliar_reformulacao.py`.

## Fonte

- **`centraldeatendimento.totvs.com`** (Central de Atendimento técnica, plataforma **Zendesk**).
- **API pública de Help Center** (JSON paginado, sem raspar HTML):
  `/api/v2/help_center/pt-br/{categories,sections,articles}.json`.
- Taxonomia por **segmento**. A categoria **"Cross Segmentos" (id `360005280714`)** concentra
  os módulos do Backoffice (650 seções, multi-linha); **"TOTVS RH" (`1500000346941`)** é à parte.

## Como rodar

```bash
# modo teste (poucos artigos de uma seção, numa pasta temporária):
python -m scripts.coletar_central --secao <id> --tema <nome> --limite 3 --saida /tmp/x

# coleta de um tema para docs_totvs/ (usa a lista ALVOS + filtro de linha):
python -m scripts.coletar_central --temas fiscal
python -m scripts.coletar_central --temas nf,financeiro   # vários temas

# opções: --linha "" (sem filtro de linha) | --categoria <id> | --secao <id>
```

- **Boas maneiras:** respeita `robots.txt`, User-Agent identificável, pausa de ~1,5 s/req.
- **Idempotente:** pula `{tema}__{id}__*.txt` já baixado; o `ingest_docs` deduplica no banco.
- **Saída:** `docs_totvs/{tema}__{id}__{slug}.txt` = `# título` + corpo limpo (Dúvida/Solução).

## Filtro de linha (Protheus)

"Cross Segmentos" mistura Protheus/RM/Datasul/Logix. `LINHA_FILTRO` é um **regex de sinais de
Protheus no título**; só salva quem casa (`--linha ""` desliga).

- Atual: `protheus|microsiga|siga[a-z]{3}` — pega "Protheus", "Microsiga" e códigos de módulo
  `SIGAxxx` (exclusivos do Protheus). Calibrado após ver que só `"Protheus"` comia artigos bons
  titulados "MP - SIGAFIN…" / "Cross Segmentos – SIGAFIN".
- **Escapam ainda (poucos):** typo "Prothes" e prefixo "MP -" sem código SIGA (ex.: "MP - FIN").
  Ampliação possível: `prothe|microsiga|\bmp\b|siga[a-z]{3}`.

## Status por tema

| Tema | Status | Protheus baixados | Descartados (linha) | Trechos | Fragmentação |
|---|---|---|---|---|---|
| fiscal | ✅ coletado + ingerido | 2.357 | ~2.700 | 2.798 | 23,9% ¹ |
| nf | ✅ coletado + ingerido | 807 | 1.192 (Linha RM) | 824 | 4,1% |
| financeiro | ✅ coletado + ingerido | 1.997 | 72 (Logix/Datasul/CRM) | 2.071 | 5,9% |
| compras | ✅ coletado + ingerido | 1.351 | 509 (Linha RM) | 1.393 | 5,1% |
| relatórios | ✅ coletado + ingerido | 343 | 211 (CRM/SFA) | 359 | 7,0% |
| estoque | ✅ coletado + ingerido | 44 | 442 (Datasul/RM) | 44 | 0,0% |
| RH | ✅ coletado (cirúrgico) + ingerido | 1.391 | 1.443 (Feedz/Velti/Ahgora) | 1.405 | 1,7% |

**Base final (7 temas):** 8.274 trechos de documentação + 25 chamados = 8.299. Fragmentação
global **10,6%**. Avaliação (25 chamados): top-1 = documentação **11/25 (44%)**; distância
média top-1 — ticket 0,31, documentação **0,48**.

Evolução da cobertura (top-1 = documentação oficial): fiscal só **4/10** (amostra de 10) →
+ NF + Financeiro **9/25** (docs mais próximos: 0,585 → 0,521) → + compras/rel/estoque + RH
**11/25 (44%)** (distância dos docs 0,476). *(a 1ª medição usou amostra de 10 chamados; as
seguintes, os 25.)*

**RH — coleta cirúrgica (opção b):** a categoria "TOTVS RH" tem 12.568 artigos em 379 seções,
mas só ~6% Protheus. Em vez de baixar tudo, o `Alvo("rh","filtro",…)` restringe às seções de
RH-Protheus (`\bGPE\b|folha|f[eé]rias|ponto|cargos|sigagpe|sigapon`); o filtro de linha isola
o Protheus. Resultado: **1.391 Protheus** (acima da estimativa de ~750; a amostra de 6% havia
subestimado), descartando os produtos não-Protheus (Pontoweb/Velti/Feedz/Ahgora).

¹ fiscal foi coletado **antes** do fix de promoção de rótulos no coletor (ADR-017); NF e
Financeiro, já com o fix, saíram em 4–6%. Re-coletar o fiscal para baixar a fragmentação é
opcional.

## Ordem acordada

Lotes medidos, com validação a cada passo: **fiscal → (nf + financeiro) → [compras,
relatórios, RH, estoque]**. Só avançar após validar cobertura/qualidade do lote anterior.
