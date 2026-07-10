"""Lista os grupos da instância da Evolution — para descobrir o WHATSAPP_GRUPO_DESTINO.

Ferramenta de SETUP (não envia nada). Depois de conectar o número da IA na instância e
adicioná-lo ao grupo da equipe, rode isto para ver o JID de cada grupo e copiar o certo
para o `.env` (WHATSAPP_GRUPO_DESTINO). Ver ADR-029 e TESTE_WHATSAPP.md.

Não depende de WHATSAPP_DRY_RUN (é uma leitura, não um envio). Precisa das variáveis da
Evolution preenchidas: WHATSAPP_API_URL, WHATSAPP_INSTANCE, WHATSAPP_API_KEY.

Uso:
    python -m scripts.lista_grupos_whatsapp
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.whatsapp import WhatsAppClient

_CAMPOS_EVOLUTION = {
    "WHATSAPP_API_URL": "whatsapp_api_url",
    "WHATSAPP_INSTANCE": "whatsapp_instance",
    "WHATSAPP_API_KEY": "whatsapp_api_key",
}


async def _buscar() -> list[dict[str, str]]:
    settings = get_settings()
    async with WhatsAppClient(settings) as whatsapp:
        return await whatsapp.listar_grupos()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    faltando = [
        rotulo
        for rotulo, attr in _CAMPOS_EVOLUTION.items()
        if not (getattr(settings, attr) or "").strip()
    ]
    if faltando:
        print("⚠️  Variáveis da Evolution não preenchidas no .env: " + ", ".join(faltando))
        print("    Preencha-as antes de listar os grupos — ver TESTE_WHATSAPP.md.")
        return 1

    try:
        grupos = asyncio.run(_buscar())
    except Exception as exc:
        print(f"❌ FALHA ao listar grupos: {type(exc).__name__}: {exc}")
        print("    Confira se a Evolution está no ar e se a instância está conectada.")
        return 1

    if not grupos:
        print("Nenhum grupo encontrado. O número da IA já foi ADICIONADO a algum grupo?")
        return 0

    print(f"Grupos da instância '{settings.whatsapp_instance}' ({len(grupos)}):\n")
    for g in grupos:
        print(f"  {g['nome']}")
        print(f"    JID: {g['jid']}")
    print("\nCopie o JID do grupo desejado para WHATSAPP_GRUPO_DESTINO no .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
