"""Registre des outils MCP : catalogue en memoire, construit au demarrage.

Le registre joue le role d'annuaire : il interroge chaque serveur MCP declare
dans la configuration, recupere la liste de ses outils et son schema d'entree,
et conserve le tout en memoire. C'est cette liste qui alimente ensuite :

  1. le filtrage par la GPO (quels outils CET utilisateur peut voir) ;
  2. les schemas transmis au modele pour le function calling ;
  3. la resolution d'un nom d'outil vers l'URL du serveur a appeler.

Consequence pratique : ajouter un serveur MCP = ajouter son URL dans .env et
redemarrer. Aucun code a modifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .client import McpClient, McpServerError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDescriptor:
    """Description d'un outil MCP, telle qu'exposee par son serveur."""

    name: str
    server: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> dict[str, Any]:
        """Convertit la description au format attendu par les API OpenAI.

        C'est le seul endroit ou l'on traduit le vocabulaire MCP vers le
        vocabulaire du moteur de langage : si le format change, il n'y a qu'ici
        a intervenir.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    """Annuaire des outils disponibles, interroge au demarrage."""

    def __init__(self, settings: Settings, client: McpClient | None = None) -> None:
        self._settings = settings
        self._client = client or McpClient(settings)
        self._tools: dict[str, ToolDescriptor] = {}
        self._server_status: dict[str, bool] = {}

    async def refresh(self) -> dict[str, bool]:
        """Interroge tous les serveurs declares et reconstruit le catalogue.

        Un serveur en panne n'empeche pas le demarrage : il est simplement
        marque indisponible, et ses outils disparaissent du catalogue. C'est un
        choix de disponibilite, a documenter explicitement.
        """
        tools: dict[str, ToolDescriptor] = {}
        status: dict[str, bool] = {}

        for server, url in self._settings.mcp_servers.items():
            try:
                descriptors = await self._client.list_tools(server, url)
            except McpServerError as exc:
                logger.warning("Serveur MCP indisponible (%s) : %s", server, exc)
                status[server] = False
                continue

            status[server] = True
            for descriptor in descriptors:
                name = descriptor["name"]
                if name in tools:
                    # Deux serveurs avec le meme nom d'outil : on refuse le
                    # doublon plutot que d'en choisir un arbitrairement.
                    logger.warning(
                        "Outil '%s' deja fourni par '%s' : '%s' ignore",
                        name,
                        tools[name].server,
                        server,
                    )
                    continue
                tools[name] = ToolDescriptor(
                    name=name,
                    server=server,
                    description=descriptor.get("description", ""),
                    input_schema=descriptor.get("input_schema", {}),
                )

        self._tools = tools
        self._server_status = status
        logger.info(
            "Registre MCP : %s outils (%s)",
            len(tools),
            ", ".join(f"{server}={'ok' if ok else 'ko'}" for server, ok in status.items()),
        )
        return status

    # --- Lecture -------------------------------------------------------------

    def all(self) -> list[ToolDescriptor]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def filter(self, allowed_names: set[str]) -> list[ToolDescriptor]:
        """Retourne uniquement les outils autorises par la GPO."""
        return [tool for tool in self.all() if tool.name in allowed_names]

    def server_status(self) -> dict[str, bool]:
        return dict(self._server_status)

    def count(self) -> int:
        return len(self._tools)
