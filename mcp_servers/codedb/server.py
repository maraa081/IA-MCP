"""Serveur MCP 'codedb' : le code vient de la base, pas du prompt.

Trois outils qui se repondent l'un l'autre :

  code_disponible   que contient l'index ? (langages, fichiers les plus gros)
  chercher_code     quels fichiers parlent de tel sujet ?
  resumer_code      expliquer un fichier, designe par une reference
  documenter_code   produire une documentation structuree de ce fichier

Le point important : aucun de ces outils ne recoit de code en argument. Ils
recoivent une REFERENCE (identifiant ou chemin), et c'est la base qui fournit le
contenu. L'orchestrateur peut donc enregistrer ce que le modele a demande
(« documente policy/engine.py ») sans jamais transporter le code dans le prompt,
et la taille des fichiers n'a plus d'incidence sur la fenetre de contexte.

Lancement :
  CODEDB_PATH=var/codedb.sqlite python -m mcp_servers.codedb.server --port 9103
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from mcp_servers.codedb.database import CodeDatabase
from mcp_servers.codedb.prompts import (
    SYSTEM_PROMPT,
    STYLE_INSTRUCTIONS,
    build_documentation_prompt,
    build_resume_prompt,
)
from mcp_servers.codedoc.frameworks import (
    available_keys,
    find_missing_sections,
    get_framework,
)
from mcp_servers.common import complete, load_settings

# Valeurs fermees declarees en types : le schema transmis au modele contient donc
# la liste des valeurs acceptees. Le modele les voit au lieu de les deviner.
StyleNom = Literal[*tuple(STYLE_INSTRUCTIONS)]
FrameworkNom = Literal[*tuple(available_keys())]
AudienceNom = Literal["developpeur", "exploitant", "auditeur", "architecte"]

# Bornes de securite du serveur (defense en profondeur : l'orchestrateur applique
# deja ses propres limites via la GPO).
MAX_CONTENU = 400_000
MAX_REFERENCES_LISTEES = 200


def chemin_base() -> Path:
    """Emplacement de la base, surchargeable par CODEDB_PATH."""
    return Path(os.environ.get("CODEDB_PATH", "var/codedb.sqlite"))


def base() -> CodeDatabase:
    """Ouvre la base (l'objet est sans etat : une connexion par operation)."""
    database = CodeDatabase(chemin_base())
    database.initialiser()
    return database


mcp = MCPServer(
    name="codedb",
    title="Serveur MCP de code indexe",
    instructions=(
        "Donne acces a un catalogue de code indexe en base. Les outils prennent "
        "une reference de fichier (identifiant ou chemin), jamais du code : "
        "commencer par code_disponible ou chercher_code pour trouver la "
        "reference, puis resumer_code ou documenter_code."
    ),
)


# --- 1. Inventaire -----------------------------------------------------------

@mcp.tool(
    description=(
        "Donne le contenu de l'index de code : nombre de fichiers, langages "
        "disponibles et fichiers les plus volumineux. A utiliser en premier "
        "quand on ne sait pas ce que contient la base."
    )
)
async def code_disponible() -> dict[str, Any]:
    """Inventaire de l'index et exemples de fichiers."""
    database = base()
    inventaire = database.inventaire()
    plus_gros = database.lister(limite=15)

    return {
        "tool": "code_disponible",
        **inventaire,
        "exemples": [entree.as_dict() for entree in plus_gros],
        "note": (
            "Les references indiquees servent ensuite a resumer_code et "
            "documenter_code."
        ),
    }


# --- 2. Recherche ------------------------------------------------------------

@mcp.tool(
    description=(
        "Cherche dans le code indexe, sur le chemin, la description et le "
        "contenu des fichiers. A utiliser pour retrouver la reference d'un "
        "fichier avant de le resumer ou de le documenter."
    )
)
async def chercher_code(
    requete: str,
    langage: str | None = None,
    limite: int = 10,
) -> dict[str, Any]:
    """Recherche des fichiers par mot-cle.

    Args:
        requete: texte cherche (nom de fonction, concept, nom de fichier).
        langage: filtre optionnel sur le langage ('python', 'sql'...).
        limite: nombre maximal de resultats (1 a 50).

    Returns:
        Les fichiers correspondants, avec leur reference.
    """
    if not requete or not requete.strip():
        return {"tool": "chercher_code", "error": "la requete est vide"}
    if not 1 <= limite <= 50:
        return {"tool": "chercher_code", "error": "'limite' doit etre entre 1 et 50"}

    entrees = base().chercher(
        requete=requete.strip(), langage=(langage or None), limite=limite
    )
    return {
        "tool": "chercher_code",
        "requete": requete,
        "langage": langage,
        "resultats": [entree.as_dict() for entree in entrees],
        "nombre": len(entrees),
        "note": "Aucun resultat : essayez un terme plus general." if not entrees else "",
    }


# --- 3. Resume ---------------------------------------------------------------

@mcp.tool(
    description=(
        "Explique un fichier de code deja indexe, designe par sa reference "
        "(identifiant ou chemin, par exemple '42' ou 'policy/engine.py'). A "
        "utiliser quand l'utilisateur demande ce que fait un fichier."
    )
)
async def resumer_code(
    reference: str,
    max_sentences: int = 5,
    style: StyleNom = "fonctionnel",
) -> dict[str, Any]:
    """Resume un fichier de l'index sans recevoir son contenu en argument.

    Args:
        reference: identifiant numerique ou chemin du fichier indexe.
        max_sentences: nombre maximal de phrases (1 a 20).
        style: fonctionnel, technique ou revue.

    Returns:
        Le resume, et la provenance (chemin, langage, taille).
    """
    if not 1 <= max_sentences <= 20:
        return {"tool": "resumer_code", "error": "'max_sentences' doit etre entre 1 et 20"}

    database = base()
    trouve = database.lire(reference)
    if trouve is None:
        return _reference_inconnue("resumer_code", database, reference)

    entree, contenu = trouve
    if len(contenu) > MAX_CONTENU:
        return {
            "tool": "resumer_code",
            "error": f"fichier trop volumineux pour ce serveur ({len(contenu)} caracteres)",
        }

    settings = load_settings()
    resume = await complete(
        settings,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_resume_prompt(entree, contenu, max_sentences, style),
            },
        ],
        temperature=0.3,
        max_tokens=120 * max_sentences,
    )

    return {
        "tool": "resumer_code",
        "resume": resume,
        "style": style,
        "provenance": entree.as_dict(),
        "model": settings.writer_model,
    }


# --- 4. Documentation --------------------------------------------------------

@mcp.tool(
    description=(
        "Produit une documentation technique structuree d'un fichier de code "
        "indexe, selon un framework etabli. A utiliser quand l'utilisateur "
        "demande de documenter un fichier present dans la base. "
        f"Frameworks disponibles : {', '.join(available_keys())}. "
        "Aucun code n'est a fournir : passer la reference du fichier."
    )
)
async def documenter_code(
    reference: str,
    framework: FrameworkNom = "diataxis",
    audience: AudienceNom = "developpeur",
) -> dict[str, Any]:
    """Documente un fichier de l'index selon un framework.

    Args:
        reference: identifiant numerique ou chemin du fichier indexe.
        framework: arc42, c4, diataxis, madr ou sphinx.
        audience: developpeur, exploitant, auditeur ou architecte.

    Returns:
        La documentation produite, la provenance, et les sections du framework
        qui n'ont pas ete couvertes.
    """
    doc_framework = get_framework(framework)
    if doc_framework is None:
        return {
            "tool": "documenter_code",
            "error": f"framework inconnu : '{framework}'",
            "frameworks_disponibles": available_keys(),
        }

    database = base()
    trouve = database.lire(reference)
    if trouve is None:
        return _reference_inconnue("documenter_code", database, reference)

    entree, contenu = trouve
    if len(contenu) > MAX_CONTENU:
        return {
            "tool": "documenter_code",
            "error": f"fichier trop volumineux pour ce serveur ({len(contenu)} caracteres)",
        }

    settings = load_settings()
    documentation = await complete(
        settings,
        [
            {"role": "system", "content": _system_prompt_documentation()},
            {
                "role": "user",
                "content": build_documentation_prompt(
                    doc_framework, entree, contenu, audience
                ),
            },
        ],
        temperature=0.4,
        max_tokens=1800,
    )

    return {
        "tool": "documenter_code",
        "framework": doc_framework.key,
        "framework_label": doc_framework.label,
        "framework_origin": doc_framework.origin,
        "audience": audience,
        "documentation": documentation,
        "expected_sections": doc_framework.sections,
        "missing_sections": find_missing_sections(doc_framework.sections, documentation),
        "provenance": entree.as_dict(),
        "model": settings.writer_model,
    }


def _system_prompt_documentation() -> str:
    from mcp_servers.codedoc.prompts import SYSTEM_PROMPT as PROMPT_DOCUMENTATION

    return PROMPT_DOCUMENTATION


def _reference_inconnue(tool: str, database: CodeDatabase, reference: str) -> dict[str, Any]:
    """Reponse d'erreur utile : on propose des references existantes.

    Un modele qui s'est trompe de reference doit pouvoir se rattraper sans
    nouvel aller-retour vers l'utilisateur.
    """
    exemples = database.lister(limite=5)
    return {
        "tool": tool,
        "error": f"reference inconnue : '{reference}'",
        "references_possibles": [entree.chemin for entree in exemples],
        "suggestion": "Utiliser chercher_code ou code_disponible pour trouver la bonne reference.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP de code indexe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9103)
    args = parser.parse_args()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
