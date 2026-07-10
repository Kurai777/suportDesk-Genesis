"""Testes do coletor da Central de Atendimento (ADR-017).

Funções de limpeza são puras; a coleta usa a API do Zendesk MOCKADA com respx — nenhuma
chamada de rede real.
"""

import httpx
import respx

from scripts.coletar_central import (
    BASE,
    LINHA_FILTRO,
    LOCALE,
    Alvo,
    _ja_baixado,
    _limpar_corpo,
    _nome_arquivo,
    _paginar,
    _promover_rotulos,
    _slug,
    coletar,
)

API = f"{BASE}/api/v2/help_center/{LOCALE}"  # acompanha a constante do coletor

# Corpo típico: HTML com script/estilo/botão + conteúdo real (Dúvida/Solução grudados).
BODY_OK = (
    "<style>.x{color:red}</style><script>var a=1;</script>"
    "<div><button>Ouvir Descrição</button>"
    "<p>DúvidaComo emitir a nota fiscal no site da prefeitura?</p>"
    "<p>SoluçãoPara emitir a nota, acesse o portal, informe o período de no máximo três "
    "meses e clique em detalhes para imprimir ou salvar o documento em PDF conforme o "
    "passo a passo descrito neste artigo oficial.</p></div>"
)
BODY_SHELL = "<style>:root{--x:1px}</style><script>load()</script><div><h2></h2></div>"


# --- limpeza (pura) --------------------------------------------------------


def test_limpar_corpo_extrai_conteudo_sem_chrome():
    txt = _limpar_corpo(BODY_OK)
    assert "emitir a nota fiscal" in txt
    assert "Ouvir Descrição" not in txt  # boilerplate removido
    assert "color:red" not in txt and "var a" not in txt  # style/script fora


def test_limpar_corpo_promove_rotulos_grudados():
    txt = _limpar_corpo(BODY_OK)
    assert "## Dúvida" in txt  # "DúvidaComo" -> heading
    assert "## Solução" in txt  # "SoluçãoPara" -> heading


def test_limpar_corpo_promove_rotulo_em_elemento_proprio():
    # Rótulo como elemento separado (espaçado, NÃO grudado) também vira heading (nível HTML).
    html = (
        "<div><p><strong>Dúvida</strong></p><p>Como emitir a nota fiscal?</p>"
        "<p><strong>Solução</strong></p><p>Acesse o portal, informe o período de no "
        "máximo três meses e clique em detalhes para imprimir o documento em PDF.</p></div>"
    )
    txt = _limpar_corpo(html)
    assert "## Dúvida" in txt
    assert "## Solução" in txt


def test_limpar_corpo_shell_js_vira_vazio():
    assert _limpar_corpo(BODY_SHELL) == ""
    assert _limpar_corpo("") == ""


def test_promover_rotulos_so_quando_grudado():
    assert "## Solução" in _promover_rotulos("fizemos oSolução X")  # grudado à esquerda
    assert "## Dúvida" in _promover_rotulos("DúvidaComo fazer")  # grudado à direita
    # cercado de espaços = ambíguo -> não promove
    assert "##" not in _promover_rotulos("a Solução foi boa")


def test_slug_e_nome_arquivo():
    assert _slug("Emissão de Nota Fiscal!") == "emissao-de-nota-fiscal"
    assert _nome_arquivo("nf", 123, "Título Ção") == "nf__123__titulo-cao.txt"


def test_ja_baixado_por_tema_e_id(tmp_path):
    assert not _ja_baixado(tmp_path, "nf", 5)
    (tmp_path / "nf__5__qualquer-slug.txt").write_text("x", encoding="utf-8")
    assert _ja_baixado(tmp_path, "nf", 5)
    assert not _ja_baixado(tmp_path, "nf", 6)  # id diferente


# --- coleta (respx, sem rede) ----------------------------------------------


def _artigos_json(artigos, next_page=None):
    return httpx.Response(200, json={"articles": artigos, "next_page": next_page})


@respx.mock
async def test_coletar_salva_limpa_pula_draft_e_shell(tmp_path):
    respx.get(f"{API}/sections/999/articles.json").mock(
        return_value=_artigos_json(
            [
                {"id": 1, "title": "Emissão de NF", "draft": False, "body": BODY_OK},
                {"id": 2, "title": "Rascunho", "draft": True, "body": BODY_OK},
                {"id": 3, "title": "Shell", "draft": False, "body": BODY_SHELL},
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        resumo = await coletar(client, [Alvo("nf", "secao", 999)], tmp_path, pausa_s=0)

    assert resumo.salvos == 1
    assert resumo.pulados_draft == 1
    assert resumo.sem_corpo == 1
    arquivos = list(tmp_path.glob("*.txt"))
    assert len(arquivos) == 1
    conteudo = arquivos[0].read_text(encoding="utf-8")
    assert conteudo.startswith("# Emissão de NF")
    assert "## Solução" in conteudo


@respx.mock
async def test_coletar_idempotente_pula_ja_baixado(tmp_path):
    respx.get(f"{API}/sections/999/articles.json").mock(
        return_value=_artigos_json(
            [{"id": 1, "title": "Emissão de NF", "draft": False, "body": BODY_OK}]
        )
    )
    async with httpx.AsyncClient() as client:
        r1 = await coletar(client, [Alvo("nf", "secao", 999)], tmp_path, pausa_s=0)
        r2 = await coletar(client, [Alvo("nf", "secao", 999)], tmp_path, pausa_s=0)

    assert r1.salvos == 1
    assert r2.salvos == 0
    assert r2.ja_baixados == 1
    assert len(list(tmp_path.glob("*.txt"))) == 1  # não duplicou


@respx.mock
async def test_coletar_respeita_limite(tmp_path):
    respx.get(f"{API}/sections/999/articles.json").mock(
        return_value=_artigos_json(
            [
                {"id": i, "title": f"NF {i}", "draft": False, "body": BODY_OK}
                for i in (1, 2, 3)
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        resumo = await coletar(
            client, [Alvo("nf", "secao", 999)], tmp_path, limite=2, pausa_s=0
        )

    assert resumo.salvos == 2  # parou no limite


@respx.mock
async def test_paginar_segue_next_page():
    # rota específica (page=2) registrada ANTES, para não ser capturada pela base
    respx.get(f"{API}/x.json", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"items": [{"id": 2}], "next_page": None})
    )
    respx.get(f"{API}/x.json").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}], "next_page": f"{API}/x.json?page=2"}
        )
    )
    async with httpx.AsyncClient() as client:
        itens = await _paginar(client, f"{API}/x.json", "items", robots=None, pausa_s=0)

    assert [i["id"] for i in itens] == [1, 2]


@respx.mock
async def test_coletar_categoria_percorre_secoes(tmp_path):
    respx.get(f"{API}/categories/500/sections.json").mock(
        return_value=httpx.Response(
            200, json={"sections": [{"id": 10}, {"id": 11}], "next_page": None}
        )
    )
    respx.get(f"{API}/sections/10/articles.json").mock(
        return_value=_artigos_json(
            [{"id": 1, "title": "A", "draft": False, "body": BODY_OK}]
        )
    )
    respx.get(f"{API}/sections/11/articles.json").mock(
        return_value=_artigos_json(
            [{"id": 2, "title": "B", "draft": False, "body": BODY_OK}]
        )
    )
    async with httpx.AsyncClient() as client:
        resumo = await coletar(client, [Alvo("adm", "categoria", 500)], tmp_path, pausa_s=0)

    assert resumo.salvos == 2  # 1 artigo de cada seção


@respx.mock
async def test_coletar_filtro_secoes_por_nome_e_linha(tmp_path):
    # 'filtro': só a seção cujo NOME casa o padrão; 'linha': só artigos Protheus.
    respx.get(f"{API}/categories/500/sections.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "sections": [
                    {"id": 10, "name": "Fiscal - SIGAFIS"},
                    {"id": 11, "name": "Financeiro - Baixas"},  # fora do padrão
                ],
                "next_page": None,
            },
        )
    )
    respx.get(f"{API}/sections/10/articles.json").mock(
        return_value=_artigos_json(
            [
                {"id": 1, "title": "Linha Protheus - SIGAFIS", "draft": False, "body": BODY_OK},
                {"id": 2, "title": "MP - SIGAFIN - FINA914", "draft": False, "body": BODY_OK},
                {"id": 3, "title": "Linha RM - Z reforma", "draft": False, "body": BODY_OK},
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        resumo = await coletar(
            client, [Alvo("fiscal", "filtro", 500, r"fiscal")], tmp_path,
            pausa_s=0, linha=LINHA_FILTRO,
        )

    # Protheus explícito (id 1) E Protheus via código SIGA sem a palavra (id 2) são mantidos.
    assert resumo.salvos == 2
    assert resumo.fora_da_linha == 1  # só o de Linha RM foi descartado
    assert any("Linha RM" in t for t in resumo.exemplos_descartados)
    assert {p.name.split("__")[1] for p in tmp_path.glob("*.txt")} == {"1", "2"}


@respx.mock
async def test_coletar_busca_usa_search_api_e_filtra_linha(tmp_path):
    # tipo 'busca': usa a API de search do Zendesk (rotina no título) + filtro de linha.
    respx.get(url__regex=r".*/help_center/articles/search\.json.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "title": "Linha Protheus - SIGAEST - MATA010 - Como cadastrar",
                     "draft": False, "body": BODY_OK},
                    {"id": 2, "title": "ELEVE - COM - Cadastro de produto",
                     "draft": False, "body": BODY_OK},
                ],
                "next_page": None,
            },
        )
    )
    async with httpx.AsyncClient() as client:
        resumo = await coletar(
            client, [Alvo("produto", "busca", 0, "MATA010 cadastro produto")], tmp_path,
            pausa_s=0, linha=LINHA_FILTRO,
        )

    assert resumo.salvos == 1  # só o Protheus (SIGAEST/MATA010)
    assert resumo.fora_da_linha == 1  # ELEVE descartado
