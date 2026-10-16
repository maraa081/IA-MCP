"""Prompts de l'outil code_to_technical_doc.

Le prompt est construit a partir du framework choisi : c'est le framework, et
non le modele, qui impose la structure du document.
"""

from __future__ import annotations

from mcp_servers.codedoc.frameworks import DocFramework

SYSTEM_PROMPT = (
    "Tu es un redacteur technique senior. Tu documentes du code source existant "
    "sans le modifier et sans inventer de comportement : si une information "
    "manque dans le code fourni, tu ecris explicitement 'information absente du "
    "code fourni'.\n"
    "Tu ne recopies jamais de secrets, cles, jetons ou donnees personnelles : tu "
    "les remplaces par <REDACTED>.\n"
    "Ta sortie est du document brut, sans phrase d'introduction ni conclusion."
)

# Consignes adaptees au public vise.
AUDIENCE_INSTRUCTIONS: dict[str, str] = {
    "developpeur": (
        "Public : developpeurs qui vont modifier ce code. Sois precis sur les "
        "contrats d'interface, les effets de bord et les pieges."
    ),
    "exploitant": (
        "Public : equipe d'exploitation. Insiste sur le demarrage, la "
        "configuration, les dependances, la supervision et les modes de panne."
    ),
    "auditeur": (
        "Public : auditeur securite ou qualite. Insiste sur les entrees non "
        "fiables, la validation, les erreurs silencieuses et les zones a risque."
    ),
    "architecte": DocFramework.__doc__ and (
        "Public : architecte. Insiste sur les frontieres de composants, les "
        "dependances externes, les compromis de conception et les evolutions."
    ),
}


def build_user_prompt(
    framework: DocFramework,
    code: str,
    language: str,
    audience: str,
) -> str:
    """Assemble le prompt complet a partir du framework et du code."""
    audience_instruction = AUDIENCE_INSTRUCTIONS.get(
        audience, AUDIENCE_INSTRUCTIONS["developpeur"]
    )
    return (
        f"Framework de documentation impose : {framework.label} "
        f"(origine : {framework.origin}).\n"
        f"Objectif du framework : {framework.best_for}.\n\n"
        "Structure exacte a respecter, dans cet ordre, avec ces titres :\n"
        f"{framework.render_structure()}\n\n"
        f"Consignes supplementaires du framework : {framework.extra_instructions}\n"
        f"{audience_instruction}\n"
        f"Format de sortie : {framework.output_format}.\n"
        f"Langage du code source : {language}.\n\n"
        "Code source a documenter entre les balises <code> :\n"
        "<code>\n"
        f"{code}\n"
        "</code>"
    )
