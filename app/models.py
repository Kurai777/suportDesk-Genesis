"""Schemas Pydantic — o contrato de I/O do serviço (regra inviolável nº 2).

Contém:
- `RespostaIA`       : o que o Claude DEVE devolver (validado por tool use, Módulo 5).
- `WebhookFreshdesk` : o que o webhook do Freshdesk envia (só o ticket_id).
- `TicketFreshdesk`  : o chamado normalizado, buscado via API do Freshdesk (Módulo 3).
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Imagens embutidas no corpo do e-mail: <img src="https://attachment.freshdesk.com/inline/...">.
# Prints colados viram inline-attachments do Freshdesk (token JWT na URL, baixa sem auth). ADR-035.
_IMG_INLINE = re.compile(
    r'<img[^>]+src="(https://[^"]*freshdesk[^"]*)"', re.IGNORECASE
)


def _imagens_inline_do_html(html: str) -> list[str]:
    """URLs das imagens inline (prints do erro) no HTML da descrição, em ordem, sem repetir."""
    vistas: dict[str, None] = {}
    for url in _IMG_INLINE.findall(html or ""):
        vistas.setdefault(url.replace("&amp;", "&"), None)
    return list(vistas)

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
    pedido_operacional: bool = Field(
        ...,
        description=(
            "true SÓ se o chamado é um PEDIDO OPERACIONAL — uma tarefa a ser EXECUTADA por "
            "uma pessoa (cadastro, liberação, ajuste manual; ex.: incluir cadastro do grupo "
            "tributário na SX5), não uma dúvida com resposta na base. Ajusta a saudação ao "
            "cliente e sinaliza ao time que há execução pendente."
        ),
    )
    alcada_admin: bool = Field(
        default=False,
        description=(
            "true se a solução EXIGE ou ENVOLVE uma operação de ALÇADA ADMINISTRATIVA (só "
            "admins fazem): alterar PARÂMETRO (MV_*), criar/alterar GATILHO, criar/alterar "
            "TABELA ou CAMPO (tamanho, etc.), ou criar USUÁRIO. Nesse caso o cliente NÃO recebe "
            "os passos (recebe a saudação-padrão); a solução/direção vai à EQUIPE (ADR-031)."
        ),
    )
    tipo_alcada: str = Field(
        default="",
        description=(
            "Quando alcada_admin=true, a categoria: 'parâmetro', 'gatilho', 'tabela/campo' ou "
            "'usuário'. Vazio quando alcada_admin=false."
        ),
    )
    # `empresa` NÃO entra aqui: é um fato do chamado (TicketFreshdesk.empresa), não algo
    # que o modelo deva gerar — evita o Claude "pegar" a empresa de um vizinho recuperado.
    # Ela é acoplada depois em `ResultadoChamado`, montado pelo pipeline.


class QueryReformulada(BaseModel):
    """Contrato de saída do Claude na reformulação de query (ADR-024).

    Só a INTENÇÃO de busca, nunca a resposta. Este texto alimenta EXCLUSIVAMENTE o
    embedding da busca vetorial — não vira contexto nem chega ao cliente.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Uma frase curta, impessoal, no vocabulário da documentação TOTVS, com a "
            "intenção de busca do chamado. Preserve TODOS os códigos técnicos."
        ),
    )


class ResultadoChamado(BaseModel):
    """Resultado completo do processamento de um chamado, montado pelo PIPELINE.

    Combina a resposta gerada pelo Claude (`RespostaIA`) com os fatos do chamado que
    vêm do Freshdesk, não do modelo: `empresa` e `ticket_id`.
    """

    ticket_id: int
    empresa: str
    resposta: RespostaIA


# --- Entrada: webhook ------------------------------------------------------


class ImagemTeste(BaseModel):
    """Print de erro anexado na interface de teste, em base64 (ADR-025).

    Enviado direto pela tela (não vem do Freshdesk); é transcrito pelo VisaoClient e
    concatenado ao texto antes da busca, exatamente como um anexo real do chamado.
    """

    model_config = ConfigDict(extra="forbid")

    content_type: str = Field(..., description="MIME da imagem, ex.: image/png.")
    dados_base64: str = Field(
        ..., description="Conteúdo da imagem em base64 (sem o prefixo 'data:...;base64,')."
    )


class TesteRequest(BaseModel):
    """Entrada da interface de teste (ADR-019): texto colado + empresa + prints opcionais.

    Como o texto vem colado (não do Freshdesk), a `empresa` é informada à mão para
    permitir auditar isolamento de dados por empresa na tela.
    """

    model_config = ConfigDict(extra="forbid")

    texto: str = Field(..., description="Texto do chamado a inspecionar.")
    empresa: str | None = Field(None, description="Empresa do chamado (opcional).")
    imagens: list[ImagemTeste] = Field(
        default_factory=list, description="Prints de erro anexados (opcional, ADR-025)."
    )


class ParInspecao(BaseModel):
    """Um item recuperado, exposto na tela de teste para auditoria de fonte/isolamento."""

    fonte: str
    titulo: str | None = None
    ticket_id: int | None = None
    empresa: str | None = None
    distancia: float
    problema: str
    solucao: str


class TesteResposta(BaseModel):
    """Saída da interface de teste: tudo que o pipeline PRODUZIRIA, sem efeito real."""

    empresa: str
    problema: str
    query: str  # o que REALMENTE foi buscado no pgvector (reformulado, ou o problema cru)
    decisao: str  # "resolvido" | "escalar"
    encontrou_solucao: bool
    confianca: str
    pedido_operacional: bool  # tarefa a executar por uma pessoa (ADR-020)
    alcada_admin: bool  # solução é operação de admin -> vai à equipe, não ao cliente (ADR-031)
    tipo_alcada: str  # categoria da alçada (parâmetro/gatilho/tabela/usuário) ou vazio
    resposta_cliente: str  # o rascunho que iria ao cliente
    resumo_para_responsavel: str
    urgencia: str
    via_web: bool  # se a busca web foi acionada
    auto_elegivel: bool  # candidato a resposta automática (recorte ADR-041) — só marcador
    query_web: str  # a query REAL enviada aos domínios TOTVS ("" = web não acionada)
    nota: str  # nota interna que SERIA criada no Freshdesk
    whatsapp: str  # mensagem que SERIA enviada no WhatsApp
    pares: list[ParInspecao]  # recuperação local
    pares_web: list[ParInspecao]  # trechos da web (se acionada)


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


class Anexo(BaseModel):
    """Anexo de um chamado (subset dos campos do Freshdesk). Usado p/ leitura de imagens."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = ""
    content_type: str = ""
    attachment_url: str = ""  # URL pré-assinada (S3) — baixada SEM auth do Freshdesk
    size: int = 0

    @property
    def eh_imagem(self) -> bool:
        return self.content_type.lower().startswith("image/")

    @property
    def eh_pdf(self) -> bool:
        return self.content_type.lower().split(";", 1)[0].strip() == "application/pdf"

    @property
    def eh_texto(self) -> bool:
        """Anexo de texto puro (log de erro, .txt/.log) — lido direto, sem Claude (ADR-039)."""
        ct = self.content_type.lower()
        return ct.startswith("text/") or self.name.lower().endswith((".txt", ".log"))


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
    attachments: list[Anexo] = Field(default_factory=list)
    # URLs de imagens EMBUTIDAS (inline) no corpo do e-mail — prints colados, não anexados
    # (ADR-035). A maioria dos chamados manda o screenshot do erro assim, não como anexo.
    imagens_inline: list[str] = Field(default_factory=list)

    @property
    def imagens(self) -> list[Anexo]:
        """Anexos que são imagens (prints de erro/logs) — candidatos à transcrição (ADR-023)."""
        return [a for a in self.attachments if a.eh_imagem]

    @property
    def pdfs(self) -> list[Anexo]:
        """Anexos PDF (logs de erro, comprovantes de NF) — também transcritos (ADR-037)."""
        return [a for a in self.attachments if a.eh_pdf]

    @property
    def anexos_texto(self) -> list[Anexo]:
        """Anexos de texto (.txt/.log — logs de erro) — lidos direto na busca (ADR-039)."""
        return [a for a in self.attachments if a.eh_texto]

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
            attachments=[Anexo.model_validate(a) for a in (payload.get("attachments") or [])],
            imagens_inline=_imagens_inline_do_html(payload.get("description") or ""),
        )
