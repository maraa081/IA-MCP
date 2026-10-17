"""Prompts des outils du serveur 'codedb'.

Deux usages :
  - resumer du code (ce qu'il fait, ses entrees et sorties, ses pieges) ;
  - le documenter selon un framework etabli.

Le prompt de documentation est construit au-dessus de celui du serveur
'codedoc' : meme cadre, on ajoute simplement le contexte de provenance (fichier
indexe, langage, taille). Une seule definition des consignes de redaction pour
toute l'infrastructure.
"""

from __future__ import annotations

from mcp_servers.codedoc.frameworks import DocFramework
from mcp_servers.codedoc.prompts import build_user_prompt
from mcp_servers.codedb.database import CodeEntry

SYSTEM_PROMPT = (
    "Tu es un lecteur de code. Tu expliques ce que fait reellement le code "
    "fourni, sans l'inventer et sans le modifier.\n"
    "Si une information n'est pas dans le code, tu ecris explicitement "
    "'information absente du code fourni'.\n"
    "Tu ne recopies jamais de secret, cle, jeton ou donnee personnelle : tu les "
    "remplaces par <REDACTED>.\n"
    "Ta sortie est du texte brut, sans phrase d'introduction ni conclusion."
)

STYLE_INSTRUCTIONS: dict[str, str] = {
    "fonctionnel": (
        "Resume fonctionnel : ce que fait le code du point de vue de celui qui "
        "l'utilise. Pas de detail d'implementation."
    ),
    "technique": (
        "Resume technique : structures de donnees, algorithme, complexite, "
        "dependances et points d'entree. Conserve les noms exacts."
    ),
    "revue": (
        "Resume de revue de code : comportement attendu, cas limites, gestion "
        "des erreurs, et les endroits fragiles ou surprenants."
    ),
}


def build_resume_prompt(
    entree: CodeEntry, contenu: str, max_sentences: int, style: str
) -> str:
    """Construit le message utilisateur pour resumer une entree de la base."""
    consigne = STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["fonctionnel"])
    return (
        f"Style demande : {consigne}\n"
        f"Longueur maximale : {max_sentences} phrases.\n\n"
        f"Provenance : fichier indexe '{entree.chemin}' "
        f"({entree.langage}, {entree.taille} octets).\n\n"
        "Code a expliquer entre les balises <code> :\n"
        "<code>\n"
        f"{contenu}\n"
        "</code>"
    )


def build_documentation_prompt(
    framework: DocFramework, entree: CodeEntry, contenu: str, audience: str
) -> str:
    """Construit le message utilisateur pour documenter une entree de la base."""
    contexte = (
        f"Ce code provient du fichier indexe '{entree.chemin}' "
        f"({entree.langage}, {entree.taille} octets).\n\n"
    )
    return contexte + build_user_prompt(
        framework, contenu, language=entree.langage, audience=audience
    )
