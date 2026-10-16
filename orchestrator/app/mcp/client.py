"""Client MCP : dialogue avec les serveurs d'outils.

MCP (Model Context Protocol) est un protocole JSON-RPC : le client ouvre une
session, demande la liste des outils, puis appelle ceux qu'il veut. Ici on
utilise le transport "streamable HTTP", c'est-a-dire des serveurs d'outils
joignables par le reseau - la forme utile en entreprise (l'alternative stdio
lance le serveur en sous-processus, pratique sur un poste de travail).

Choix assume pour le labo : une session par appel. C'est simple a lire et a
raisonner (aucun etat partage), au prix d'une poignee de main supplementaire.
En production, on garderait des sessions ouvertes dans un pool par serveur.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..config import Settings

# Bornes de securite cote client : le serveur peut etre lent ou bloque, on ne
# veut pas immobiliser l'orchestrateur indefiniment.
MAX_RESULT_CHARS = 200_000


@dataclass
class ToolCallResult:
    """Resultat normalise d'un appel d'outil MCP."""

    tool: str
    server: str
    ok: bool
    payload: dict[str, Any] | None = None
    text: str = ""
    error: str | None = None
    duration_ms: int = 0
    raw_content: list[Any] = field(default_factory=list)

    def preview(self, max_chars: int = 400) -> str:
        """Apercu textuel, pour les journaux et les traces d'API."""
        source = self.text or json.dumps(self.payload, ensure_ascii=False)
        return source[:max_chars] + ("..." if len(source) > max_chars else "")


class McpClient:
    """Client MCP : decouverte des outils et appels."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- Decouverte ----------------------------------------------------------

    async def list_tools(self, server: str, url: str) -> list[dict[str, Any]]:
        """Recupere la description des outils d'un serveur.

        Retourne une liste de dictionnaires {name, description, input_schema}.
        Leve McpServerError si le serveur est injoignable.
        """
        try:
            async with asyncio.timeout(self._settings.mcp_discovery_timeout_seconds):
                async with streamable_http_client(url) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        listing = await session.list_tools()
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise McpServerError(f"delai de decouverte depasse pour {server}") from exc
        except Exception as exc:  # noqa: BLE001 - on normalise toute erreur reseau
            raise McpServerError(f"serveur {server} injoignable : {exc}") from exc

        descriptors = []
        for tool in getattr(listing, "tools", []) or []:
            descriptors.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                }
            )
        return descriptors

    # --- Appel ---------------------------------------------------------------

    async def call_tool(
        self,
        *,
        server: str,
        url: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        """Appelle un outil et normalise le resultat."""
        started = time.perf_counter()

        try:
            async with asyncio.timeout(self._settings.mcp_timeout_seconds):
                async with streamable_http_client(url) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
        except (asyncio.TimeoutError, TimeoutError):
            return ToolCallResult(
                tool=tool,
                server=server,
                ok=False,
                error=f"delai depasse ({self._settings.mcp_timeout_seconds}s)",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolCallResult(
                tool=tool,
                server=server,
                ok=False,
                error=f"appel MCP en echec : {exc}",
                duration_ms=_elapsed_ms(started),
            )

        payload, text, content_blocks = _normalize_result(result)

        # Un appel est considere en echec des qu'une erreur est detectee, que
        # l'erreur vienne du protocole ou du contenu renvoye par l'outil.
        error_message = _detect_error(
            tool=tool,
            payload=payload,
            text=text,
            protocol_error=bool(getattr(result, "is_error", False)),
        )

        return ToolCallResult(
            tool=tool,
            server=server,
            ok=error_message is None,
            payload=payload,
            text=text,
            error=error_message,
            duration_ms=_elapsed_ms(started),
            raw_content=content_blocks,
        )


class McpServerError(RuntimeError):
    """Serveur MCP injoignable ou en erreur."""


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _detect_error(
    *,
    tool: str,
    payload: dict[str, Any] | None,
    text: str,
    protocol_error: bool,
) -> str | None:
    """Retourne le message d'erreur d'un appel, ou None si l'appel a reussi.

    Deux sources d'erreur, volontairement traitees de la meme facon :

    1. l'erreur de protocole (`is_error`) : argument refuse par le schema de
       l'outil, exception levee dans l'outil, delai depasse ;
    2. l'erreur applicative : l'outil repond normalement, mais son contenu
       structure contient une cle 'error' (validation metier).

    Le point 2 corrige un defaut constate en execution : un outil qui refusait
    ses arguments etait comptabilise comme un succes, avec un resultat vide.
    """
    if protocol_error:
        return text or f"l'outil '{tool}' a signale une erreur"

    if isinstance(payload, dict) and payload.get("error"):
        return f"l'outil '{tool}' a refuse les arguments : {payload['error']}"

    return None


def _normalize_result(result: Any) -> tuple[dict[str, Any] | None, str, list[Any]]:
    """Transforme un CallToolResult MCP en (payload structure, texte, blocs bruts).

    Un outil MCP peut repondre soit avec du contenu structure (recommandé pour
    les donnees), soit avec des blocs de texte. On accepte les deux : le
    rendu doit rester exploitable meme avec un serveur tiers moins rigoureux.
    """
    content_blocks = list(getattr(result, "content", []) or [])
    structured = getattr(result, "structured_content", None)

    payload: dict[str, Any] | None = None
    if isinstance(structured, dict):
        # Certains serveurs encapsulent la valeur dans {"result": {...}}.
        if set(structured.keys()) == {"result"} and isinstance(structured["result"], dict):
            payload = structured["result"]
        else:
            payload = structured

    texts = [
        block.text
        for block in content_blocks
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    text = "\n".join(texts).strip()

    # Pas de contenu structure : on tente de lire du JSON dans le texte.
    if payload is None and text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = None

    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[tronque]"

    return payload, text, content_blocks
