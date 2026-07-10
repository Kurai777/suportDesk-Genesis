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

    # --- Busca web (último recurso, ADR-015) ---
    # false = desligada (padrão seguro). Só dispara quando a base local (chamados +
    # docs) não resolveu; consulta restrita aos domínios oficiais TOTVS.
    busca_web_ativa: bool = False

    # --- Interface de teste local (ADR-019) ---
    # false = rotas /teste desativadas (padrão seguro — NÃO expor em produção, pois a
    # tela mostra os pares recuperados). Ligue só em ambiente local para inspecionar.
    interface_teste_ativa: bool = False

    # --- Leitura de imagens dos chamados (visão, ADR-023) ---
    # true = chamados novos com anexo de imagem têm o texto legível (prints de erro/logs)
    # transcrito e concatenado à query do RAG. Best-effort: falha na imagem não derruba o
    # chamado. Desligue (false) para pular a leitura de imagens.
    leitura_imagens_ativa: bool = True

    # --- Regra de negócio ---
    confianca_minima: str = "alta"

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


@lru_cache
def get_settings() -> Settings:
    """Instância única (cacheada) das configurações, para injeção nos clientes."""
    return Settings()  # type: ignore[call-arg]  # campos obrigatórios vêm do ambiente
