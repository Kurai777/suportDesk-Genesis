# TESTE_WHATSAPP.md — Teste de envio real via Evolution API

Checklist para testar o **envio real** de WhatsApp (com um número dedicado) antes de
produção, usando `scripts/testa_whatsapp.py` e o `WhatsAppClient` de produção.

> ⚠️ **Envia mensagem DE VERDADE.** Use um número **dedicado de teste** — nunca um cliente
> real. O piloto usa a **Evolution API** (a migração para a Meta Cloud API fica para produção).

---

## 1. Subir a Evolution API

Suba uma instância da Evolution API v2 (Docker é o caminho usual). Você precisa de:

- [ ] Evolution API acessível por HTTP (ex.: `http://localhost:8080` local, ou a URL do seu
      servidor). Essa URL vira o `WHATSAPP_API_URL`.
- [ ] A **API key global** da Evolution (definida na subida do serviço, ex.: variável
      `AUTHENTICATION_API_KEY`). Ela serve para **criar/gerenciar instâncias** — não é a que o
      envio usa (veja o passo 3).

> A Evolution é software de terceiros; siga a documentação oficial da versão que você subir. O
> nosso lado só precisa da URL, da instância e do token da instância.

## 2. Criar a instância

- [ ] Crie uma instância (ex.: nome `genesis-teste`) via API da Evolution, autenticando com a
      **API key global**. O nome escolhido vira o `WHATSAPP_INSTANCE`.
- [ ] Guarde o **token/apikey DA INSTÂNCIA** retornado na criação — é ele que o envio usa
      (`WHATSAPP_API_KEY`), **não** a key global.

## 3. Conectar o número dedicado (QR code)

- [ ] Abra o QR code da instância (endpoint de connect da Evolution) e **escaneie com o
      WhatsApp do número dedicado** (WhatsApp do celular → Aparelhos conectados → Conectar).
- [ ] Confirme que a instância está no estado **conectado/`open`** antes de testar. Instância
      não conectada faz o envio falhar (a API costuma responder erro tipo "instance not
      connected").

## 4. Preencher o `.env` (⚠️ só para o teste)

Preencha estas variáveis no `.env` (o `.gitignore` já protege o `.env` — nunca commite):

- [ ] `WHATSAPP_API_URL=` → URL da Evolution (passo 1). Ex.: `http://localhost:8080`
- [ ] `WHATSAPP_INSTANCE=` → nome da instância (passo 2). Ex.: `genesis-teste`
- [ ] `WHATSAPP_API_KEY=` → token **DA INSTÂNCIA** (passo 2), **não** a key global
- [ ] `WHATSAPP_DRY_RUN=false` → **só para o teste** (habilita o envio real)

## 5. Rodar o teste

```bash
python -m scripts.testa_whatsapp 5511999999999
# ou com mensagem personalizada:
python -m scripts.testa_whatsapp 5511999999999 "Mensagem de teste"
```

O número pode vir com ou sem DDI/símbolos — o script normaliza (DDD+número ganha o DDI 55).
Para testar o envio a um **grupo**, passe o JID no lugar do número (ver seção 7).

- [ ] **Sucesso:** imprime `✅ SUCESSO` com o `HTTP 201` e o corpo da resposta da Evolution; a
      mensagem chega no aparelho conectado.
- [ ] **Falha:** imprime `❌ FALHA` com o motivo — o corpo do erro da Evolution (ex.: `HTTP 400 —
      instance not connected`) ou "sem resposta" (Evolution fora do ar / URL errada).

**Proteções do script (por que é seguro):**
- Se `WHATSAPP_DRY_RUN` **não** for `false`, o script **não envia** — avisa e sai. Esquecer a
  flag ligada nunca dispara mensagem sem querer.
- Se faltar alguma das 3 variáveis da Evolution, ele diz **qual** e aponta este checklist,
  antes de tentar qualquer envio.

## 6. Depois do teste — voltar para dry-run (importante)

- [ ] **Volte `WHATSAPP_DRY_RUN=true` no `.env`** assim que terminar.

**É seguro e imediato.** O `WhatsAppClient` relê a flag **a cada `enviar()`** (não a fixa na
subida): com `true`, toda notificação vira apenas log e retorna sucesso, **sem tocar a
Evolution**. Assim você continua testando o resto do sistema (webhook, interface) sem risco de
disparar WhatsApp real sem querer. Para voltar a enviar de verdade, é só pôr `false` de novo.

---

## 7. Notificar um GRUPO em vez de um número (ADR-029)

Se você quer que a IA mande o feedback dos chamados para um **grupo** da equipe (em vez do
telefone do responsável), o fluxo é:

- [ ] Com o número da IA já conectado à instância (passos 1–3), **adicione esse número ao
      grupo** normalmente (pelo WhatsApp, como qualquer contato).
- [ ] Descubra o **JID do grupo** (um grupo não é um telefone — é um id tipo
      `120363018941234567@g.us`):
      ```bash
      python -m scripts.lista_grupos_whatsapp
      ```
      Ele lista os grupos da instância com nome + JID. (Só leitura, não envia nada; não depende
      de `WHATSAPP_DRY_RUN`.)
- [ ] Copie o JID do grupo certo para o `.env`:
      ```
      WHATSAPP_GRUPO_DESTINO=120363018941234567@g.us
      ```
      Com essa variável **preenchida**, TODO chamado passa a notificar o grupo (o mapa
      `RESPONSAVEIS`/telefone é ignorado). **Vazia** = volta ao modelo antigo (telefone do
      responsável).
- [ ] Teste o envio ao grupo (com `WHATSAPP_DRY_RUN=false`):
      ```bash
      python -m scripts.testa_whatsapp 120363018941234567@g.us "Teste no grupo"
      ```

> O número da IA precisa **ser membro** do grupo para conseguir enviar. Se a Evolution
> responder erro ao enviar, confirme que o número foi adicionado ao grupo.

---

## Referência rápida das variáveis

| Variável | O que é | De onde vem |
|---|---|---|
| `WHATSAPP_API_URL` | URL base da Evolution | Passo 1 |
| `WHATSAPP_INSTANCE` | Nome da instância | Passo 2 |
| `WHATSAPP_API_KEY` | Token **da instância** (não o global) | Passo 2 (criação da instância) |
| `WHATSAPP_DRY_RUN` | `false` p/ enviar de verdade; `true` p/ só logar | Passo 4 (teste) → Passo 6 (voltar) |
| `WHATSAPP_GRUPO_DESTINO` | JID do grupo que recebe os feedbacks (opcional) | Passo 7 (`lista_grupos_whatsapp`) |

> O `WHATSAPP_RESPONSAVEL_DEFAULT` e o mapa `RESPONSAVES` (agente→telefone) são usados pelo
> **pipeline** para decidir o destinatário de cada chamado — o script de teste não precisa
> deles (você passa o número na linha de comando).
