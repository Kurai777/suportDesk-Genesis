"""Testes da limpeza de texto (ADR-011) — função pura, sem I/O."""

from app.texto import extrair_codigos_tecnicos, limpar_texto


def test_remove_saudacao_despedida_e_assinatura():
    texto = (
        "Olá, bom dia!\n"
        "O erro SCC19070 aparece ao lançar a NF no parâmetro MV_ATFMOED.\n"
        "Atenciosamente,\n"
        "João da Silva\n"
        "Genesis Consulting\n"
        "(11) 99999-9999"
    )
    limpo = limpar_texto(texto)

    assert "SCC19070" in limpo and "MV_ATFMOED" in limpo
    assert "Olá" not in limpo
    assert "Atenciosamente" not in limpo
    assert "João da Silva" not in limpo  # assinatura cortada junto com a despedida


def test_remove_blocos_citados_e_cid():
    texto = (
        "Segue o erro atual [cid:image001.png@01D].\n"
        "> Em 12/03, Fulano escreveu:\n"
        "> mensagem antiga citada\n"
        "Detalhe técnico: tabela SC5 corrompida."
    )
    limpo = limpar_texto(texto)

    assert "[cid:" not in limpo
    assert "mensagem antiga citada" not in limpo
    assert "SC5" in limpo


def test_remove_cabecalhos_de_email():
    texto = (
        "De: fulano@x.com\n"
        "Para: suporte@genesis.com\n"
        "Assunto: Erro NF\n"
        "O problema é o MV_ATFMOED sem taxa da moeda."
    )
    limpo = limpar_texto(texto)

    assert "De:" not in limpo and "Assunto:" not in limpo
    assert "MV_ATFMOED" in limpo


def test_normaliza_espacos():
    limpo = limpar_texto("linha   com    espaços\n\n\n\noutra linha")
    assert "   " not in limpo
    assert "\n\n\n" not in limpo


def test_texto_vazio():
    assert limpar_texto("") == ""
    assert limpar_texto("   \n  \n") == ""


def test_mantem_linha_tecnica_longa_iniciada_por_palavra_de_despedida():
    # "Obrigado" no início de uma linha LONGA não deve cortar o conteúdo técnico.
    texto = (
        "Obrigado pelo retorno, mas o erro MV_ATFMOED persiste ao reprocessar a "
        "nota fiscal de entrada com a moeda 3."
    )
    limpo = limpar_texto(texto)
    assert "MV_ATFMOED" in limpo


def test_remove_saudacao_inline_e_assinatura_final():
    # E-mail de UMA LINHA: "Hi Fulano, Bom dia, tudo bem? <conteúdo> Att,"
    limpo = limpar_texto(
        "Hi Aldenir Domingos, Bom dia, tudo bem? Conforme analisado, foi feita a correção. Att,"
    )
    assert "Hi Aldenir" not in limpo
    assert "Bom dia" not in limpo and "tudo bem" not in limpo
    assert "Att" not in limpo
    assert limpo.startswith("Conforme analisado")


def test_remove_nome_antes_de_cordialidade_preservando_conteudo():
    # "Rafael, boa tarde, tudo bem?" no início (nome sem 'Hi'), + caractere invisível.
    limpo = limpar_texto(
        "​Rafael, boa tarde, tudo bem? Após mapeamento identificamos a moeda do Ativo Fixo."
    )
    assert "Rafael" not in limpo
    assert "boa tarde" not in limpo
    assert "​" not in limpo
    assert limpo.startswith("Após mapeamento")
    assert "moeda do Ativo Fixo" in limpo


def test_nao_corta_clausula_real_que_comeca_com_virgula():
    # "Após análise," NÃO deve ser removido (não é saudação, apesar da vírgula no início).
    limpo = limpar_texto("Após análise, o problema estava no cálculo do imposto.")
    assert limpo.startswith("Após análise")
    assert "cálculo do imposto" in limpo


# --- extrair_codigos_tecnicos (ADR-024) ------------------------------------


def test_extrai_parametro_rotina_modulo_tabela_campo_e_erro():
    texto = (
        "Erro SCC19070 ao rodar MATA010 no SIGAEST. "
        "Parâmetro MV_ATFMOED e campo B1_COD; conferir a tabela SX5."
    )
    assert extrair_codigos_tecnicos(texto) == [
        "SCC19070",
        "MATA010",
        "SIGAEST",
        "MV_ATFMOED",
        "B1_COD",
        "SX5",
    ]


def test_preserva_ordem_e_remove_repetidos():
    assert extrair_codigos_tecnicos("MATA010 e SX5, de novo MATA010") == ["MATA010", "SX5"]


def test_assunto_em_caixa_alta_sem_codigo_nao_gera_falso_positivo():
    # O texto cru vem com assunto em CAIXA ALTA (COLETA.md); exigir dígito/prefixo evita ruído.
    assert extrair_codigos_tecnicos("RELATÓRIO SMART VIEW NÃO ABRE PARA O USUÁRIO") == []


def test_numero_solto_e_palavra_comum_nao_sao_codigo():
    assert extrair_codigos_tecnicos("Nota 123456 emitida em 2024 sem erro") == []


def test_extrair_codigos_de_texto_vazio():
    assert extrair_codigos_tecnicos("") == []
