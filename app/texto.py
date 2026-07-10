"""Limpeza de texto de e-mails de chamados — função pura e testável (ADR-011/013).

Remove ruído de e-mail (saudações e cordialidades, despedidas + assinatura, blocos
citados, cabeçalhos de encaminhamento, `[cid:...]`, caracteres invisíveis) e normaliza
espaços, PRESERVANDO o corpo técnico. Trata tanto e-mails multi-linha quanto os de uma
linha só ("Hi Fulano, Bom dia, tudo bem? <conteúdo> Att,"), comuns no Freshdesk.
"""

from __future__ import annotations

import re

_INVISIVEIS = re.compile(r"[​‌‍﻿]")
_CID = re.compile(r"\[cid:[^\]]*\]", re.IGNORECASE)
_ESPACOS = re.compile(r"[ \t]+")

# --- padrões por LINHA (e-mails multi-linha) -------------------------------

_SAUDACAO = re.compile(
    r"^(ol[áa]|oi|hi|hello|prezad[oa]s?|car[oa]s?|bom dia|boa tarde|boa noite|"
    r"senhor[ea]s?|sr\.?|sra\.?)\b",
    re.IGNORECASE,
)
_DESPEDIDA = re.compile(
    r"^(atenciosamente|att\b|abra[çc]os?|abs\b|grat[oa]\b|obrigad[oa]\b|cordialmente|"
    r"sauda[çc][õo]es|desde j[áa]|no aguardo|fico [àa] disposi[çc][ãa]o|"
    r"qualquer d[úu]vida|--\s*$)",
    re.IGNORECASE,
)
_CABECALHO_EMAIL = re.compile(
    r"^(de|para|assunto|enviad[ao]|data|from|to|subject|sent|cc)\s*:",
    re.IGNORECASE,
)
_CITACAO_INTRO = re.compile(r"^(em .*escreveu:|on .*wrote:)", re.IGNORECASE)

_MAX_SAUDACAO = 50
_MAX_DESPEDIDA = 60

# --- padrões INLINE (e-mails de uma linha só) ------------------------------

# "Hi Fulano," / "Olá pessoal," no início (saudação + nome, até a vírgula).
_SAUDACAO_NOME = re.compile(
    r"^\s*(?:hi|hello|ol[áa]|oi|prezad[oa]s?|car[oa]s?|senhor[ea]?s?|sr\.?|sra\.?)"
    r"\b[^.!?\n]{0,40}?,\s*",
    re.IGNORECASE,
)
# "Rafael," no início, MAS só quando seguido de uma cordialidade (evita cortar frase real).
_NOME_ANTES_CORDIALIDADE = re.compile(
    r"^\s*[A-Za-zÀ-ÿ][^.!?\n]{0,30}?,\s*(?=bom dia|boa tarde|boa noite|tudo bem)",
    re.IGNORECASE,
)
# Cordialidades iniciais: "bom dia, tudo bem? ..."
_CORDIALIDADES = re.compile(
    r"^\s*(?:(?:bom dia|boa tarde|boa noite|tudo bem\??|como vai\??|blz\??)[\s,.!?]*)+",
    re.IGNORECASE,
)
# Assinatura curta no fim: "... Att," / "... Obrigado."
_ASSINATURA_FINAL = re.compile(
    r"[\s,.\-–]*\b(?:att\.?|atenciosamente|abra[çc]os?|obrigad[oa]|grat[oa]|cordialmente|"
    r"sauda[çc][õo]es|no aguardo|desde j[áa])\b[\s,.!]*$",
    re.IGNORECASE,
)


def _remover_cortesias_inline(texto: str) -> str:
    texto = _SAUDACAO_NOME.sub("", texto, count=1)
    texto = _NOME_ANTES_CORDIALIDADE.sub("", texto, count=1)
    texto = _CORDIALIDADES.sub("", texto, count=1)
    texto = _ASSINATURA_FINAL.sub("", texto, count=1)
    return texto.strip()


def limpar_texto(texto: str) -> str:
    """Remove ruído de e-mail e normaliza espaços, mantendo o corpo técnico."""
    if not texto:
        return ""
    texto = _CID.sub("", _INVISIVEIS.sub("", texto))
    uteis: list[str] = []
    for linha in texto.splitlines():
        s = linha.strip()
        if not s or s.startswith(">"):
            continue  # linha vazia ou bloco citado
        if _DESPEDIDA.match(s) and len(s) <= _MAX_DESPEDIDA:
            break  # da despedida em diante vem a assinatura → descarta o resto
        eh_saudacao = _SAUDACAO.match(s) and len(s) <= _MAX_SAUDACAO
        if eh_saudacao or _CABECALHO_EMAIL.match(s) or _CITACAO_INTRO.match(s):
            continue
        uteis.append(_ESPACOS.sub(" ", s))
    return _remover_cortesias_inline("\n".join(uteis).strip())
