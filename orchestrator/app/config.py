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

    # Interface d'ecoute de l'API.
    #   127.0.0.1 : accessible uniquement depuis la machine (le plus sur, et le
    #               seul choix raisonnable en production derriere un reverse proxy)
    #   0.0.0.0   : accessible depuis le reseau. En labo sous WSL, c'est ce qu'il
    #               faut pour ouvrir l'interface depuis le navigateur Windows.
    # Attention : l'API exigera toujours un jeton, mais elle devient joignable
    # depuis le reseau local. A remettre sur 127.0.0.1 apres les essais.
    orchestrator_host: str = "127.0.0.1"
    orchestrator_port: int = 8090

    # --- Gateway LLM ---------------------------------------------------------
    gateway_url: str = "http://127.0.0.1:4000"
    gateway_api_key: str = "sk-lab-internal-0001"
    planner_model: str = "planner"
    writer_model: str = "writer"
    # native = function calling ; json = le modele repond en JSON ; auto = essaie
    # native puis retombe sur json si le modele ne produit pas d'appel d'outil.
    # laya = un modele de decision choisit l'outil, le modele de langage extrait
    # ensuite les arguments (voir llm/planner.py et llm/laya_client.py).
    planner_mode: Literal["native", "json", "auto", "laya"] = "auto"
    # required : le modele doit produire un appel d'outil, y compris l'echappatoire
    # 'respond_directly'. Rend le choix deterministe. Mesure sur la pile du labo :
    # c'est ce reglage qui fait que le modele appelle l'outil de resume au lieu de
    # rediger le resume lui-meme. Voir llm/planner.py.
    planner_tool_choice: Literal["auto", "required"] = "required"
    # Nombre maximal de tours de planification par requete. Au-dela, la boucle
    # s'arrete et la reponse est composee a partir de ce qui a ete obtenu. Deux
    # tours suffisent au cas d'usage courant : chercher un fichier en base, puis
    # le resumer. Une borne est indispensable : sans elle, une erreur de
    # modelisation ferait tourner l'orchestrateur indefiniment.
    max_tool_rounds: int = 2
    # Ollama : desactiver la phase de reflexion accelere fortement la decision.
    ollama_disable_thinking: bool = True
    llm_timeout_seconds: float = 900.0
    synthesis_enabled: bool = True
    # Budget de tokens de la reponse finale. 1200 coupait les documents longs
    # en plein milieu, sans que l'utilisateur voie autre chose qu'une reponse
    # qui s'arrete. La valeur reste surchargeable par la requete Open WebUI.
    synthesis_max_tokens: int = 4096

    # --- Modele de decision Laya (planificateur) -----------------------------
    # Sidecar local lance par scripts/laya.sh. Laya choisit l'outil ; le modele
    # de langage extrait les arguments. Si le sidecar est indisponible, le
    # planificateur retombe sur le mode `auto` : aucune panne utilisateur.
    laya_url: str = "http://127.0.0.1:9104"
    laya_model: str = "convaiinnovations/laya-multilingual"
    laya_timeout_seconds: float = 60.0
    # La confiance de Laya mesure la concentration, pas la justesse. On garde
    # un plancher tres bas et on prefere la marge entre les deux premieres
    # options : une quasi-egalite doit renvoyer la decision au modele de langage.
    laya_min_confidence: float = 0.01
    laya_min_margin: float = 0.02
    laya_max_state_chars: int = 6000

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
    mcp_codedb_url: str = "http://127.0.0.1:9103/mcp"
    mcp_timeout_seconds: float = 1000.0
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
        """Catalogue des serveurs MCP : nom logique -> URL du point d'entree.

        Ajouter un serveur ici suffit a l'exposer : le registre decouvre ses
        outils au demarrage, et la GPO decide qui y a acces.
        """
        return {
            "summarizer": self.mcp_summarizer_url,
            "codedoc": self.mcp_codedoc_url,
            "codedb": self.mcp_codedb_url,
        }

    @property
    def dev_role_list(self) -> list[str]:
        return [role.strip() for role in self.dev_roles.split(",") if role.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retourne la configuration (mise en cache : lue une seule fois)."""
    return Settings()
