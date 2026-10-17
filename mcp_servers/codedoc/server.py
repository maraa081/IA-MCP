"""Serveur MCP 'codedoc' : expose l'outil code_to_technical_doc.

Cet outil transforme du code source en documentation technique structuree,
en s'appuyant sur un framework etabli (Diataxis, arc42, C4, MADR, Sphinx).
Le framework est choisi par l'appelant : le modele ne decide pas de la structure.

Lancement :
  python -m mcp_servers.codedoc.server --port 9102
"""

from __future__ import annotations

import argparse
from typing import Literal

from mcp.server.mcpserver import MCPServer

from mcp_servers.codedoc.frameworks import (
    available_keys,
    find_missing_sections,
    get_framework,
)
from mcp_servers.codedoc.prompts import build_user_prompt
from mcp_servers.common import complete, load_settings

MAX_CODE_CHARS = 60_000
ALLOWED_AUDIENCES = ("developpeur", "exploitant", "auditeur", "architecte")

# Valeurs fermees declarees en types : le SDK en deduit un schema JSON avec la
# liste des valeurs autorisees (champ "enum"), que le modele voit. Sans cela, le
# modele devine et produit des valeurs hors liste (constate en execution avec
# 'audience'). Les listes restent derivees du registre et de la constante
# ci-dessus : une seule source de verite, verifiee par le test d'alignement.
FrameworkName = Literal[*tuple(available_keys())]
AudienceName = Literal[*ALLOWED_AUDIENCES]

mcp = MCPServer(
    name="codedoc",
    title="Serveur MCP de documentation technique",
    instructions=(
        "Fournit l'outil code_to_technical_doc, qui produit une documentation "
        "technique structuree selon un framework reconnu."
    ),
)


def _tool_description() -> str:
    """Description de l'outil, generee depuis le registre des frameworks.

    C'est ce texte, et non le docstring, qui est transmis au modele : il doit
    donc contenir les valeurs attendues pour les parametres a valeurs ouvertes
    (le langage du code, par exemple).
    """
    frameworks = ", ".join(available_keys())
    return (
        "Produit une documentation technique avancee a partir de code source, "
        "en respectant un framework etabli. A utiliser quand l'utilisateur "
        "demande de documenter, expliquer ou formaliser du code. "
        f"Frameworks disponibles : {frameworks}. "
        "Parametre 'language' : langage du code source, a indiquer en clair "
        "(python, javascript, sql, bash, c...), jamais par un code de langue "
        "comme 'fr' ou 'en'."
    )


@mcp.tool(description=_tool_description())
async def code_to_technical_doc(
    code: str,
    framework: FrameworkName = "diataxis",
    language: str = "python",
    audience: AudienceName = "developpeur",
) -> dict:
    """Documente un code source selon un framework de documentation etabli.

    Args:
        code: le code source a documenter.
        framework: diataxis, arc42, c4, madr ou sphinx.
        language: langage du code (python, javascript, sql...).
        audience: developpeur, exploitant, auditeur ou architecte.

    Returns:
        Un dictionnaire contenant la documentation produite et des metadonnees,
        dont la liste des sections du framework qui n'ont pas ete couvertes.
    """
    doc_framework = get_framework(framework)
    if doc_framework is None:
        return {
            "tool": "code_to_technical_doc",
            "error": f"framework inconnu : '{framework}'",
            "available_frameworks": available_keys(),
        }

    validation_error = _validate(code, audience)
    if validation_error:
        return {"tool": "code_to_technical_doc", "error": validation_error}

    settings = load_settings()
    documentation = await complete(
        settings,
        [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": build_user_prompt(doc_framework, code, language, audience),
            },
        ],
        temperature=0.4,
        max_tokens=1800,
    )

    return {
        "tool": "code_to_technical_doc",
        "framework": doc_framework.key,
        "framework_label": doc_framework.label,
        "framework_origin": doc_framework.origin,
        "audience": audience,
        "language": language,
        "documentation": documentation,
        "expected_sections": doc_framework.sections,
        "missing_sections": find_missing_sections(doc_framework.sections, documentation),
        "input_chars": len(code),
        "model": settings.writer_model,
    }


def _system_prompt() -> str:
    from mcp_servers.codedoc.prompts import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def _validate(code: str, audience: str) -> str | None:
    if not code or not code.strip():
        return "le parametre 'code' est vide"
    if len(code) > MAX_CODE_CHARS:
        return f"le code depasse la limite du serveur ({MAX_CODE_CHARS} caracteres)"
    if audience not in ALLOWED_AUDIENCES:
        return f"'audience' doit etre l'un de : {', '.join(ALLOWED_AUDIENCES)}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP de documentation technique")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9102)
    args = parser.parse_args()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
