"""Prompts de l'outil de resume.

Ils sont isoles dans ce module pour une raison pratique : en entreprise, ce sont
les textes que l'on fait relire par un expert metier, et que l'on fait evoluer
sans toucher a la logique du serveur.
"""

from __future__ import annotations

# Consigne systeme : on cadre fortement le modele pour obtenir une sortie
# directement exploitable (pas de preambule, pas de commentaire meta).
SYSTEM_PROMPT = (
    "Tu es un moteur de resume technique. Tu produis uniquement le resume "
    "demande, sans introduction, sans conclusion, sans commentaire sur ta "
    "demarche. Tu conserves les chiffres, noms propres, versions et unites "
    "exactement tels qu'ils apparaissent dans la source."
)

# Styles de resume proposes par l'outil. Un style = une consigne de format.
STYLE_INSTRUCTIONS: dict[str, str] = {
    "neutral": "Resume neutre et factuel, en prose.",
    "bullets": "Resume sous forme de liste a puces, une idee par puce.",
    "executive": (
        "Resume de direction : commence par une phrase de synthese, puis 3 a 5 "
        "points cles, puis les risques ou points d'attention s'il y en a."
    ),
    "technical": (
        "Resume technique : conserve la terminologie exacte, les versions, les "
        "mesures et les liens de cause a effet."
    ),
}


def build_user_prompt(text: str, max_sentences: int, style: str) -> str:
    """Construit le message utilisateur envoye au modele."""
    instruction = STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["neutral"])
    return (
        f"Style demande : {instruction}\n"
        f"Longueur maximale : {max_sentences} phrases.\n\n"
        "Document a resumer entre les balises <document> :\n"
        "<document>\n"
        f"{text}\n"
        "</document>"
    )
