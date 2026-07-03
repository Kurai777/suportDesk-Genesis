"""Schemas Pydantic — o contrato de I/O do serviço (regra inviolável nº 2).

Contém:
- `RespostaIA`       : o que o Claude DEVE devolver (validado por tool use, Módulo 5).
- `WebhookFreshdesk` : o que o webhook do Freshdesk envia (só o ticket_id).
- `TicketFreshdesk`  : o chamado normalizado, buscado via API do Freshdesk (Módulo 3).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Vocabulários controlados ---------------------------------------------

# `confianca` é enumerado pelo CLAUDE.md.
Confianca = Literal["alta", "media", "baixa"]

# Prioridade oficial do Freshdesk, mapeada de int para rótulo (ver `_PRIORIDADE`).
Prioridade = Literal["baixa", "media", "alta", "urgente"]

# Urgência PERCEBIDA PELO CONTEÚDO do chamado (inferida pelo Claude). Espelha o
# vocabulário da prioridade oficial do Freshdesk, mas é um sinal COMPLEMENTAR: a
# prioridade oficial vem do campo `priority` do ticket; a urgência aqui é a
# leitura que o modelo faz do texto (logs, tom, impacto descrito no chamado).
Urgencia = Prioridade

# Freshdesk: 1=Low, 2=Medium, 3=High, 4=Urgent. Desconhecido -> "media".
_PRIORIDADE: dict[int, Prioridade] = {1: "baixa", 2: "media", 3: "alta", 4: "urgente"}

EMPRESA_DESCONHECIDA = "Empresa não identificada"


# --- Contrato de saída do Claude ------------------------------------------


class RespostaIA(BaseModel):
    """Contrato de saída do Claude. O modelo devolve SOMENTE isto, em JSON válido.

    `extra="forbid"`: qualquer campo inventado além do contrato é rejeitado —
    reforço à regra de ouro anti-alucinação já na fronteira de validação.
    """

    model_config = ConfigDict(extra="forbid")

    resposta_cliente: str = Field(
        ...,
        description="Rascunho de resposta ao cliente (revisado por humano na Fase 1).",
    )
    encontrou_solucao: bool = Field(
        ...,
        description="true SÓ se a solução estiver no contexto recuperado da base TOTVS.",
    )
    confianca: Confianca = Field(
        ...,
        description="Confiança na resposta: 'alta', 'media' ou 'baixa'.",
    )
    resumo_para_responsavel: str = Field(
        ...,
        description="Resumo curto do caso para o WhatsApp/nota interna.",
    )
    urgencia: Urgencia = Field(
        ...,
        description=(
            "Urgência percebida pelo conteúdo do chamado "
            "(complementar à prioridade oficial)."
        ),
    )
    # `empresa` NÃO entra aqui: é um fato do chamado (TicketFreshdesk.empresa), não algo
    # que o modelo deva gerar — evita o Claude "pegar" a empresa de um vizinho recuperado.
    # Ela é acoplada depois em `ResultadoChamado`, montado pelo pipeline.


class ResultadoChamado(BaseModel):
    """Resultado completo do processamento de um chamado, montado pelo PIPELINE.

    Combina a resposta gerada pelo Claude (`RespostaIA`) com os fatos do chamado que
    vêm do Freshdesk, não do modelo: `empresa` e `ticket_id`.
    """

    ticket_id: int
    empresa: str
    resposta: RespostaIA


# --- Entrada: webhook ------------------------------------------------------


class WebhookFreshdesk(BaseModel):
    """Payload mínimo do webhook do Freshdesk.

    A regra de automação do Freshdesk deve enviar apenas o ID do ticket, ex.:
        {"ticket_id": {{ticket.id}}}
    O chamado completo é buscado via API em freshdesk.py (Módulo 3).
    `extra="ignore"`: toleramos campos a mais que o Freshdesk venha a incluir.
    """

    model_config = ConfigDict(extra="ignore")

    ticket_id: int = Field(..., description="ID do chamado recém-aberto no Freshdesk.")


# --- Chamado normalizado (buscado via API do Freshdesk) --------------------


class Requester(BaseModel):
    """Solicitante do chamado (subset de campos que usamos)."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    email: str | None = None


class TicketFreshdesk(BaseModel):
    """Chamado normalizado a partir de GET /tickets/{id}?include=requester,company,stats."""

    model_config = ConfigDict(extra="ignore")

    id: int
    subject: str = ""
    description_text: str = ""
    priority: Prioridade
    status: int
    requester: Requester
    empresa: str
    responder_id: int | None = None

    @classmethod
    def from_freshdesk(cls, payload: dict[str, Any]) -> "TicketFreshdesk":
        """Normaliza o JSON da API: mapeia prioridade e resolve o nome da empresa."""
        company = payload.get("company") or {}
        requester = payload.get("requester") or {}
        return cls(
            id=payload["id"],
            subject=payload.get("subject") or "",
            description_text=payload.get("description_text") or "",
            priority=_PRIORIDADE.get(payload.get("priority"), "media"),
            status=payload.get("status", 2),
            requester=Requester(
                name=requester.get("name") or "",
                email=requester.get("email"),
            ),
            empresa=company.get("name") or EMPRESA_DESCONHECIDA,
            responder_id=payload.get("responder_id"),
        )
