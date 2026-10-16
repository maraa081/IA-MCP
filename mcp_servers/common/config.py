"""Configuration des serveurs MCP.

Toute la configuration vient de l'environnement (fichier .env charge par le
script de lancement). Aucune valeur sensible n'est ecrite en dur dans le code :
c'est le premier reflexe a prendre avant une mise en production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    """Lit une variable d'environnement, avec valeur par defaut (labo)."""
    value = os.environ.get(name, default)
    return value.strip()


@dataclass(frozen=True)
class ServerSettings:
    """Reglages communs a tous les serveurs MCP."""

    gateway_url: str
    gateway_api_key: str
    writer_model: str
    request_timeout_seconds: float
    disable_thinking: bool

    @property
    def chat_completions_url(self) -> str:
        return f"{self.gateway_url.rstrip('/')}/v1/chat/completions"


def load_settings(default_model: str = "writer") -> ServerSettings:
    """Construit les reglages depuis l'environnement."""
    return ServerSettings(
        gateway_url=_env("GATEWAY_URL", "http://127.0.0.1:4000"),
        gateway_api_key=_env("GATEWAY_API_KEY", "sk-lab-internal-0001"),
        writer_model=_env("WRITER_MODEL", default_model),
        request_timeout_seconds=float(_env("TOOL_TIMEOUT_SECONDS", "300")),
        disable_thinking=_env("OLLAMA_DISABLE_THINKING", "true").lower()
        in ("1", "true", "yes"),
    )
