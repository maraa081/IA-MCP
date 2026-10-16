"""Configuration de l'orchestrateur.

Regle : aucune valeur sensible dans le code. Tout vient de l'environnement, avec
des valeurs par defaut uniquement utilisables en labo. Le fichier .env n'est
jamais versionne (voir .gitignore).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Racine du projet : orchestrator/app/config.py -> remonter de deux niveaux.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Parametres de l'application, lus dans l'environnement et le .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Identite ------------------------------------------------------------
    app_name: str = "mcp-orchestrator-lab"
    environment: Literal["lab", "prod"] = "lab"
    log_level: str = "INFO"

    # --- Gateway LLM ---------------------------------------------------------
    gateway_url: str = "http://127.0.0.1:4000"
    gateway_api_key: str = "sk-lab-internal-0001"
    planner_model: str = "planner"
    writer_model: str = "writer"
    # native = function calling ; json = le modele repond en JSON ; auto = essaie
    # native puis retombe sur json si le modele ne produit pas d'appel d'outil.
    planner_mode: Literal["native", "json", "auto"] = "auto"
    # required : le modele doit produire un appel d'outil, y compris l'echappatoire
    # 'respond_directly'. Rend le choix deterministe. Mesure sur la pile du labo :
    # c'est ce reglage qui fait que le modele appelle l'outil de resume au lieu de
    # rediger le resume lui-meme. Voir llm/planner.py.
    planner_tool_choice: Literal["auto", "required"] = "required"
    # Ollama : desactiver la phase de reflexion accelere fortement la decision.
    ollama_disable_thinking: bool = True
    llm_timeout_seconds: float = 300.0
    synthesis_enabled: bool = True

    # --- JWT -----------------------------------------------------------------
    issuer: str = "mcp-orchestrator-lab"
    audience: str = "orchestrator-api"
    jwt_algorithm: str = "RS256"
    jwt_key_id: str = "lab-key-1"
    access_token_ttl_seconds: int = 900
    jwt_leeway_seconds: int = 10

    # --- Chemins -------------------------------------------------------------
    policies_path: Path = PROJECT_ROOT / "config" / "policies.yaml"
    keys_dir: Path = PROJECT_ROOT / "config" / "keys"
    revocation_path: Path = PROJECT_ROOT / "config" / "revoked_tokens.json"
    audit_log_path: Path = PROJECT_ROOT / "var" / "logs" / "audit.jsonl"

    # --- Serveurs MCP --------------------------------------------------------
    mcp_summarizer_url: str = "http://127.0.0.1:9101/mcp"
    mcp_codedoc_url: str = "http://127.0.0.1:9102/mcp"
    mcp_timeout_seconds: float = 300.0
    mcp_discovery_timeout_seconds: float = 10.0

    # --- Authentification de demonstration ----------------------------------
    # En production, cette section disparait : les jetons sont emis par un
    # fournisseur d'identite (Keycloak, Entra ID, Okta) et l'orchestrateur ne
    # fait que VERIFIER la signature.
    dev_username: str = "demo"
    dev_password: str = "changeme-lab"
    dev_roles: str = "analyst"

    @property
    def private_key_path(self) -> Path:
        return self.keys_dir / "private.pem"

    @property
    def public_key_path(self) -> Path:
        return self.keys_dir / "public.pem"

    @property
    def mcp_servers(self) -> dict[str, str]:
        """Catalogue des serveurs MCP : nom logique -> URL du point d'entree."""
        return {
            "summarizer": self.mcp_summarizer_url,
            "codedoc": self.mcp_codedoc_url,
        }

    @property
    def dev_role_list(self) -> list[str]:
        return [role.strip() for role in self.dev_roles.split(",") if role.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retourne la configuration (mise en cache : lue une seule fois)."""
    return Settings()
