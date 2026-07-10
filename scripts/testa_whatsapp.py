"""⚠️ ENVIA MENSAGEM REAL DE WHATSAPP — NÃO é dry-run.

Teste manual do envio real via Evolution API, usando o `WhatsAppClient` de produção
(app/whatsapp.py). Dispara uma mensagem DE VERDADE para o número informado — use um
número DEDICADO de teste, não um cliente real.

Pré-requisitos (ver TESTE_WHATSAPP.md): Evolution API no ar, instância criada e o número
conectado (QR code), e no `.env`: WHATSAPP_API_URL, WHATSAPP_INSTANCE, WHATSAPP_API_KEY
(token da INSTÂNCIA) e **WHATSAPP_DRY_RUN=false** (só para o teste).

Segurança: se WHATSAPP_DRY_RUN não for false, o script NÃO envia — avisa e sai. Assim,
esquecer a flag ligada nunca dispara mensagem sem querer. Depois do teste, volte
WHATSAPP_DRY_RUN=true: o `WhatsAppClient` relê a flag a cada `enviar()`, então em true
toda notificação vira só log, sem tocar a Evolution.

Uso:
    # no .env: WHATSAPP_DRY_RUN=false (e as 3 variáveis da Evolution preenchidas)
    python -m scripts.testa_whatsapp 5511999999999
    python -m scripts.testa_whatsapp 5511999999999 "Mensagem de teste personalizada"
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import Settings, get_settings
from app.whatsapp import WhatsAppClient, normalizar_numero

_MSG_PADRAO = (
    "🤖 Teste do Suporte TOTVS IA (Genesis). Se você recebeu esta mensagem, a "
    "integração com o WhatsApp via Evolution API está funcionando."
)

# Variáveis da Evolution obrigatórias para o envio real (rótulo -> atributo em Settings).
_CAMPOS_EVOLUTION = {
    "WHATSAPP_API_URL": "whatsapp_api_url",
    "WHATSAPP_INSTANCE": "whatsapp_instance",
    "WHATSAPP_API_KEY": "whatsapp_api_key",
}


def config_incompleta(settings: Settings) -> list[str]:
    """Nomes das variáveis da Evolution que estão vazias (para uma mensagem de erro clara)."""
    return [
        rotulo
        for rotulo, attr in _CAMPOS_EVOLUTION.items()
        if not (getattr(settings, attr) or "").strip()
    ]


async def enviar_teste(settings: Settings, numero: str, texto: str) -> tuple[bool, str]:
    """Envia via `WhatsAppClient` REAL e devolve (sucesso, detalhe legível da resposta da API).

    Injeta um `httpx.AsyncClient` com hook de resposta só para CAPTURAR status + corpo da
    Evolution — o `enviar()` de produção esconde isso (retorna só bool). Erro de rede não
    produz resposta: nesse caso o detalhe explica que a Evolution não respondeu.
    """
    captura: dict[str, object] = {}

    async def _capturar(resp: httpx.Response) -> None:
        await resp.aread()
        captura["status"] = resp.status_code
        captura["corpo"] = resp.text

    async with httpx.AsyncClient(
        timeout=15.0, event_hooks={"response": [_capturar]}
    ) as http:
        whatsapp = WhatsAppClient(settings, client=http)
        sucesso = await whatsapp.enviar(numero, texto)

    if captura:
        detalhe = f"HTTP {captura['status']} — resposta da Evolution: {captura['corpo']}"
    else:
        detalhe = (
            "sem resposta da Evolution (falha de rede/conexão). Confira se a Evolution está "
            "no ar e se WHATSAPP_API_URL está correta."
        )
    return sucesso, detalhe


def _uso() -> None:
    print(__doc__)
    print("Erro: informe o número de destino. Ex.: python -m scripts.testa_whatsapp 5511999999999")


def main() -> int:
    # Windows: o console padrão (cp1252) não encoda os emojis do output → força utf-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    if not args:
        _uso()
        return 2
    numero = args[0]
    texto = args[1] if len(args) > 1 else _MSG_PADRAO
    settings = get_settings()

    if settings.whatsapp_dry_run:
        print(
            "⚠️  WHATSAPP_DRY_RUN está LIGADO (true) — nada será enviado de verdade.\n"
            "    Para o teste REAL, defina WHATSAPP_DRY_RUN=false no .env e rode de novo.\n"
            "    (Depois do teste, volte para true — ver TESTE_WHATSAPP.md.)"
        )
        return 1

    faltando = config_incompleta(settings)
    if faltando:
        print(
            "⚠️  Variáveis da Evolution não preenchidas no .env: " + ", ".join(faltando) + ".\n"
            "    Preencha-as antes do teste — ver TESTE_WHATSAPP.md."
        )
        return 1

    destino = normalizar_numero(numero)
    print(f"→ Enviando mensagem REAL para {destino} (instância: {settings.whatsapp_instance})...")
    sucesso, detalhe = asyncio.run(enviar_teste(settings, numero, texto))

    if sucesso:
        print(f"✅ SUCESSO. {detalhe}")
        print("   Confira o aparelho conectado. Depois, volte WHATSAPP_DRY_RUN=true no .env.")
        return 0
    print(f"❌ FALHA. {detalhe}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
