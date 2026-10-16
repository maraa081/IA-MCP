"""Serveur MCP 'summarizer' : expose l'outil summarize_text.

Cycle d'un appel :
  orchestreur --(JSON-RPC tools/call)--> ce serveur --(HTTP)--> gateway --> Ollama

Lancement :
  python -m mcp_servers.summarizer.server --port 9101
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Literal

from mcp.server.mcpserver import MCPServer

from mcp_servers.common import complete, load_settings
from mcp_servers.summarizer.prompts import SYSTEM_PROMPT, STYLE_INSTRUCTIONS, build_user_prompt

# Bornes de securite du serveur. L'orchestrateur applique DEJA des limites via la
# GPO ; on les repete ici volontairement (defense en profondeur). Un serveur MCP
# ne doit jamais supposer que son appelant a fait son travail de validation.
MAX_INPUT_CHARS = 200_000
MAX_SENTENCES = 20
ALLOWED_STYLES = tuple(STYLE_INSTRUCTIONS.keys())

# Le type des valeurs fermees est declare ici, et non dans une chaine de
# caracteres : le SDK en deduit un schema JSON contenant la liste des valeurs
# autorisees (champ "enum"). Le modele voit donc les valeurs valides au lieu de
# les deviner. Corrige un defaut constate en execution : le modele inventait des
# valeurs hors liste (par exemple un style inexistant) et l'appel echouait.
# La liste reste derivee de STYLE_INSTRUCTIONS : une seule source de verite.
StyleName = Literal[*ALLOWED_STYLES]

mcp = MCPServer(
    name="summarizer",
    title="Serveur MCP de resume",
    instructions=(
        "Fournit l'outil summarize_text, qui produit un resume fidele d'un "
        "document texte long."
    ),
)


@mcp.tool(
    description=(
        "Resume un document texte. A utiliser quand l'utilisateur demande un "
        "resume, une synthese ou les points cles d'un texte long."
    )
)
async def summarize_text(
    text: str,
    max_sentences: int = 5,
    style: StyleName = "neutral",
) -> dict:
    """Resume un texte en au plus `max_sentences` phrases.

    Args:
        text: le document a resumer.
        max_sentences: nombre maximal de phrases du resume (1 a 20).
        style: neutral, bullets, executive ou technical.

    Returns:
        Un dictionnaire {summary, style, max_sentences, input_chars, model}.
    """
    validation_error = _validate(text, max_sentences, style)
    if validation_error:
        # On renvoie une erreur explicite plutot que de laisser filer l'appel :
        # le modele appelant pourra corriger ou renoncer.
        return {"error": validation_error, "tool": "summarize_text"}

    settings = load_settings()
    summary = await complete(
        settings,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(text, max_sentences, style)},
        ],
        temperature=0.3,
        max_tokens=120 * max_sentences,
    )

    return {
        "tool": "summarize_text",
        "summary": summary,
        "style": style,
        "max_sentences": max_sentences,
        "input_chars": len(text),
        "model": settings.writer_model,
    }


def _validate(text: str, max_sentences: int, style: str) -> str | None:
    """Retourne un message d'erreur, ou None si les arguments sont valides."""
    if not text or not text.strip():
        return "le parametre 'text' est vide"
    if len(text) > MAX_INPUT_CHARS:
        return f"le texte depasse la limite du serveur ({MAX_INPUT_CHARS} caracteres)"
    if not 1 <= max_sentences <= MAX_SENTENCES:
        return f"'max_sentences' doit etre entre 1 et {MAX_SENTENCES}"
    if style not in ALLOWED_STYLES:
        return f"'style' doit etre l'un de : {', '.join(ALLOWED_STYLES)}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP de resume")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()

    # Transport streamable HTTP : un service reseau, comme un microservice
    # classique. Alternative stdio : utile quand le serveur est lance par le
    # client lui-meme (poste de travail, conteneur ephemere).
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
