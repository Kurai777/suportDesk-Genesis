"""Configurações do serviço, carregadas de variáveis de ambiente (.env).

Regra inviolável nº 5: nenhum segredo no código — tudo vem do ambiente.
As classes-cliente (FreshdeskClient, ClaudeClient, VoyageClient, WhatsAppClient)
recebem uma instância de `Settings` por injeção, para permitir testes sem tocar
em serviços reais (Padrões de Engenharia do CLAUDE.md).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Todas as variáveis de ambiente do serviço, tipadas e validadas."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Freshdesk ---
    freshdesk_domain: str
    freshdesk_api_key: str
    freshdesk_webhook_secret: str

    # --- Claude (Anthropic) ---
    anthropic_api_key: str
    claude_model: str = "claude-haiku-4-5-20251001"

    # --- Embeddings (Voyage AI) ---
    voyage_api_key: str
    voyage_model: str = "voyage-3"
    voyage_embedding_dim: int = 1024  # deve casar com o VECTOR(n) em db/init.sql

    # --- Banco vetorial (Postgres + pgvector) ---
    database_url: str

    # --- WhatsApp (Evolution API v2 no piloto) ---
    whatsapp_api_url: str
    whatsapp_api_key: str  # token da INSTÂNCIA da Evolution (não o token global)
    whatsapp_instance: str
    whatsapp_responsavel_default: str
    whatsapp_dry_run: bool = True  # padrão seguro em dev: não envia de verdade
    # Mapa id_do_agente (Freshdesk responder_id, como string) -> telefone. Ex. (.env):
    # RESPONSAVEIS={"67": "5511999999999"}
    responsaveis: dict[str, str] = Field(default_factory=dict)
    # Grupo do WhatsApp que recebe TODOS os feedbacks (ADR-029). É o JID do grupo na Evolution
    # (ex.: 120363018941234567@g.us), NÃO um telefone. Descubra com scripts/lista_grupos_whatsapp.
    # Vazio = mantém o modelo antigo (notifica o telefone do responsável).
    whatsapp_grupo_destino: str = ""
    # Segredo do webhook de ENTRADA (Evolution -> nós). Vazio = webhook rejeita tudo (padrão
    # seguro). Configure na Evolution como header `x-webhook-secret` OU `?secret=` na URL. ADR-026.
    whatsapp_webhook_secret: str = ""

    # --- Busca web (último recurso, ADR-015) ---
    # false = desligada (padrão seguro). Só dispara quando a base local (chamados +
    # docs) não resolveu; consulta restrita aos domínios oficiais TOTVS.
    busca_web_ativa: bool = False

    # --- Interface de teste local (ADR-019) ---
    # false = rotas /teste desativadas (padrão seguro — NÃO expor em produção, pois a
    # tela mostra os pares recuperados). Ligue só em ambiente local para inspecionar.
    interface_teste_ativa: bool = False

    # --- Reformulação de query antes do RAG (ADR-024) ---
    # true = o Claude reescreve o chamado como INTENÇÃO de busca antes do embedding
    # (o texto cru, com saudação/assinatura/assunto em CAIXA ALTA, infla a distância).
    # Custa uma chamada Haiku curta por chamado. Best-effort: falha usa o texto limpo.
    # Afeta SÓ a busca — nunca o contexto entregue ao Claude nem o texto ao cliente.
    reformular_query_ativa: bool = True

    # --- Leitura de imagens dos chamados (visão, ADR-023) ---
    # true = chamados novos com anexo de imagem têm o texto legível (prints de erro/logs)
    # transcrito e concatenado à query do RAG. Best-effort: falha na imagem não derruba o
    # chamado. Desligue (false) para pular a leitura de imagens.
    leitura_imagens_ativa: bool = True

    # --- Portal do Cliente TOTVS (API interna, ADR-026) ---
    # Cliente da API JSON privada do Portal (get-tickets / get-comments). A autenticação é
    # um JWT de sessão (login 2FA humano) injetado em runtime — NÃO fica aqui. false = a
    # integração não é acionada (padrão seguro). Ver ADR-026.
    portal_totvs_base_url: str = "https://ti-services.totvs.com.br/customer-portal-backend"
    portal_totvs_ativo: bool = False
    # Números autorizados a responder o OTP (2FA) no grupo, em JSON (ex.: ["5511999998888"]).
    # Vazio = qualquer membro do grupo pode fornecer o código (o grupo já é a fronteira de
    # confiança). Usado no relay de OTP (ADR-026).
    portal_otp_autorizados: list[str] = Field(default_factory=list)

    # --- Login do Portal (provedor de sessão 2FA, ADR-026) ---
    # Credencial do Portal (ALTO VALOR — conta de parceiro). NUNCA no código/git/chat; só aqui,
    # do .env (cofre em produção). Usada pelo PortalLoginProvider (browser) na re-auth ocasional.
    portal_login_usuario: str = ""
    portal_login_senha: str = ""
    # URL que dispara o get-tickets (e o login SAML, se deslogado).
    portal_login_url: str = (
        "https://suporte.totvs.com/portal/p/10098/customer-portal-dashboard/tickets"
    )
    # Perfil Chrome persistido (lembra o dispositivo → 2FA raro). Vazio = padrão no home.
    portal_login_profile_dir: str = ""
    portal_login_headless: bool = True  # false = janela visível (útil no primeiro teste)
    # 2FA: código expira (~30s). Se vier inválido/expirado, avisa no grupo e PEDE outro, até N.
    portal_login_max_2fa: int = 4
    portal_login_2fa_timeout_s: float = 90.0  # espera por tentativa

    # --- Regra de negócio ---
    confianca_minima: str = "alta"
    # Guardrail de distância (ADR-030): só RESOLVE se o melhor par recuperado estiver a uma
    # distância de cosseno <= este limiar. Cruza o auto-relato do Claude com um sinal OBJETIVO —
    # match distante (ex.: doc de NFSE para uma NF de entrada, ~0,46) escala mesmo com "alta".
    # Calibrável com dados; um bom match fica ~0,31, os medíocres a partir de ~0,45.
    distancia_maxima_confiavel: float = 0.40
    # Recorte de AUTO-RESPOSTA (ADR-041): limiar de distância para um RESOLVIDO ser candidato a
    # resposta automática (Fase 2). Hoje é só um MARCADOR de medição — nada é enviado (Fase 1
    # copiloto). Default = mesmo do guardrail; aperte com dados antes de ligar a Fase 2.
    limiar_auto_resposta: float = 0.40
    # MODO SOMBRA da Fase 2 (ADR-042): true = a nota dos chamados auto-elegíveis ganha um aviso de
    # que o rascunho SERIA enviado automaticamente (mas NÃO é — segue copiloto). Serve para a equipe
    # comparar, em produção, o que a IA enviaria vs o que ela responde. NÃO envia nada ao cliente.
    modo_sombra_auto: bool = False

    def telefone_responsavel(self, agente_id: int | None) -> str:
        """Resolve o telefone do responsável (agente -> telefone), com fallback.

        Usado pelo PIPELINE (Módulo 7): dado o `responder_id` do chamado, procura em
        RESPONSAVEIS; se não achar (ou sem agente), cai no WHATSAPP_RESPONSAVEL_DEFAULT.
        """
        if agente_id is not None:
            telefone = self.responsaveis.get(str(agente_id))
            if telefone:
                return telefone
        return self.whatsapp_responsavel_default

    def destino_notificacao(self, agente_id: int | None) -> str:
        """Destino do WhatsApp: o GRUPO da equipe (se configurado) ou o telefone do responsável.

        Fase 1 (ADR-029): com WHATSAPP_GRUPO_DESTINO preenchido, TODO chamado notifica o grupo —
        o `agente_id` é ignorado. Sem ele, cai no `telefone_responsavel` (comportamento anterior).
        """
        grupo = self.whatsapp_grupo_destino.strip()
        if grupo:
            return grupo
        return self.telefone_responsavel(agente_id)


@lru_cache
def get_settings() -> Settings:
    """Instância única (cacheada) das configurações, para injeção nos clientes."""
    return Settings()  # type: ignore[call-arg]  # campos obrigatórios vêm do ambiente
