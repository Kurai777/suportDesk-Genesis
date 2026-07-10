"""Sobe o serviço localmente para desenvolvimento/teste.

Garante o Python certo: se você chamar com um interpretador FORA do `.venv` (ex.:
`python run_local.py` pegando o Python do sistema), ele se re-executa no `.venv` — onde as
dependências estão instaladas (evita ModuleNotFoundError).

No Windows, o psycopg async exige o SelectorEventLoop, mas o uvicorn instala o
ProactorEventLoop por padrão. Este runner fixa a política ANTES de subir o uvicorn
(loop='asyncio' sem subprocess, para o uvicorn não sobrescrever). Em Linux é no-op.

Uso:
    # habilite a interface (uma vez): no .env, INTERFACE_TESTE_ATIVA=true
    python run_local.py                  # http://127.0.0.1:8000  → abra /teste
    #   PORT=8077 python run_local.py     # outra porta
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Re-executa no Python do venv se estivermos com outro interpretador. `if _VENV_PY.exists()`
# torna isto no-op em ambientes sem venv (ex.: Railway, onde as deps são globais).
_VENV_PY = Path(__file__).resolve().with_name(".venv") / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)
if _VENV_PY.exists() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

# uvicorn só existe no venv — importado APÓS o re-exec acima.
import uvicorn  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=porta, loop="asyncio")
