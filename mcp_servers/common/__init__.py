"""Code partage entre les serveurs MCP : configuration et client LLM.

Pourquoi ce dossier : les serveurs MCP sont des processus independants, mais ils
ont besoin des memes reglages (URL de la gateway, cle de service). On factorise
ici pour eviter la duplication et garder chaque serveur court et lisible.
"""

from .config import ServerSettings, load_settings
from .llm import GatewayError, complete

__all__ = ["ServerSettings", "load_settings", "GatewayError", "complete"]
