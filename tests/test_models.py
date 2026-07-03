"""Testes do Módulo 2: contratos RespostaIA, ResultadoChamado e WebhookFreshdesk.

Sem chamadas externas — só validação Pydantic.
"""

import pytest
from pydantic import ValidationError

from app.models import RespostaIA, ResultadoChamado, WebhookFreshdesk

# `empresa` NÃO entra em RespostaIA (é fato do chamado, montado em ResultadoChamado).
RESPOSTA_VALIDA = {
    "resposta_cliente": "Atualize a taxa da moeda 3 do Ativo Fixo e reprocesse a NF.",
    "encontrou_solucao": True,
    "confianca": "alta",
    "resumo_para_responsavel": "MV_ATFMOED apontava moeda 3 sem taxa do dia.",
    "urgencia": "alta",
}


def test_resposta_ia_valida():
    r = RespostaIA(**RESPOSTA_VALIDA)
    assert r.encontrou_solucao is True
    assert r.confianca == "alta"
    assert r.urgencia == "alta"


def test_resposta_ia_confianca_invalida():
    dados = {**RESPOSTA_VALIDA, "confianca": "altissima"}
    with pytest.raises(ValidationError):
        RespostaIA(**dados)


def test_resposta_ia_proibe_campo_extra():
    # Campo fora do contrato = possível alucinação de schema → rejeitado.
    dados = {**RESPOSTA_VALIDA, "parametro_inventado": "MV_XYZ"}
    with pytest.raises(ValidationError):
        RespostaIA(**dados)


def test_resposta_ia_nao_aceita_empresa():
    # empresa saiu do contrato de saída do Claude; agora é campo extra proibido.
    dados = {**RESPOSTA_VALIDA, "empresa": "Cliente Exemplo Ltda"}
    with pytest.raises(ValidationError):
        RespostaIA(**dados)


def test_resposta_ia_campo_obrigatorio_faltando():
    dados = {k: v for k, v in RESPOSTA_VALIDA.items() if k != "encontrou_solucao"}
    with pytest.raises(ValidationError):
        RespostaIA(**dados)


def test_resultado_chamado_compoe_resposta_com_fatos_do_chamado():
    resposta = RespostaIA(**RESPOSTA_VALIDA)
    resultado = ResultadoChamado(ticket_id=101, empresa="Cliente Exemplo Ltda", resposta=resposta)

    assert resultado.ticket_id == 101
    assert resultado.empresa == "Cliente Exemplo Ltda"
    assert resultado.resposta.encontrou_solucao is True


def test_webhook_le_ticket_id_e_ignora_extras():
    w = WebhookFreshdesk(ticket_id=12345, evento="ticket_created", ignorado=True)
    assert w.ticket_id == 12345


def test_webhook_ticket_id_obrigatorio():
    with pytest.raises(ValidationError):
        WebhookFreshdesk(evento="ticket_created")
