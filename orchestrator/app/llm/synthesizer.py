"""Synthese : redaction de la reponse finale a partir des resultats d'outils.

Pourquoi une etape separee : les outils renvoient des structures de donnees
(JSON), pas de la prose. La synthese transforme ces resultats en reponse lisible,
et - c'est important - peut signaler les appels refuses par la GPO.

Deux options de configuration :
  - SYNTHESIS_ENABLED=false : la reponse est le resultat brut de l'outil
    (plus rapide, utile pour les gros documents) ;
  - SYNTHESIS_ENABLED=true  : on ajoute un passage par le modele redacteur.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from .gateway_client import GatewayClient, GatewayUnavailable

SYNTHESIS_SYSTEM_PROMPT = (
    "Tu rediges la reponse finale a l'utilisateur a partir des resultats "
    "d'outils fournis.\n"
    "Regles :\n"
    "- Reponds a la demande initiale, en francais, de facon directe.\n"
    "- Ne mentionne pas le nom des outils ni le detail technique des appels.\n"
    "- Si des appels ont ete refuses par la politique de securite, indique-le "
    "clairement et explique quoi faire pour obtenir le droit.\n"
    "- Si des appels ont echoue pour une autre raison (arguments refuses par "
    "l'outil, delai depasse), dis-le et indique comment reformuler la demande.\n"
    "- Si un resultat est vide ou en erreur, dis-le ; n'invente rien.\n"
    "- Conserve les faits, chiffres et noms exacts presents dans les resultats."
)

# Taille maximale d'un resultat d'outil transmis a la synthese. Au-dela, on
# tronque : cela protege la fenetre de contexte du modele et evite de payer pour
# du texte inutile. 6000 caracteres coupaient une documentation technique au
# milieu d'une section ; 24000 laissent passer un document entier.
MAX_RESULT_CHARS = 24_000


class Synthesizer:
    """Redige la reponse finale a partir des resultats d'outils."""

    def __init__(self, gateway: GatewayClient, settings: Settings) -> None:
        self._gateway = gateway
        self._settings = settings

    async def compose(
        self,
        *,
        user_message: str,
        tool_results: list[dict[str, Any]],
        denials: list[str],
        errors: list[str] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, str]:
        """Compose la reponse finale.

        Retourne (texte, finish_reason). Le finish_reason est transmis tel quel a
        Open WebUI : sans lui, une reponse coupee par le budget de tokens est
        indiscernable d'une reponse complete, et l'utilisateur ne voit jamais la
        fin de son document.
        """
        prompt_parts = [f"Demande initiale de l'utilisateur :\n{user_message}"]

        if tool_results:
            rendered = "\n\n".join(
                f"Resultat de l'appel {index} (outil {result.get('tool')}) :\n"
                f"{json.dumps(result.get('payload'), ensure_ascii=False)[:MAX_RESULT_CHARS]}"
                for index, result in enumerate(tool_results, start=1)
            )
            prompt_parts.append(f"Resultats d'outils :\n{rendered}")

        if denials:
            prompt_parts.append(
                "Appels refuses par la politique de securite :\n"
                + "\n".join(f"- {denial}" for denial in denials)
            )

        if errors:
            prompt_parts.append(
                "Appels en erreur (arguments refuses par l'outil, delai depasse, "
                "serveur indisponible) :\n" + "\n".join(f"- {error}" for error in errors)
            )

        if not tool_results and not denials and not errors:
            prompt_parts.append("Aucun resultat d'outil disponible.")

        try:
            reply = await self._gateway.chat(
                model=model or self._settings.writer_model,
                messages=[
                    {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(prompt_parts)},
                ],
                temperature=0.3,
                max_tokens=max_tokens or self._settings.synthesis_max_tokens,
            )
        except GatewayUnavailable as exc:
            # On ne veut jamais perdre le travail deja fait : on degrade
            # proprement en rendant les resultats bruts.
            return (
                f"[synthese indisponible : {exc}]\n\n"
                + json.dumps(tool_results, ensure_ascii=False, indent=2)[:MAX_RESULT_CHARS],
                "stop",
            )

        return reply.content, reply.finish_reason
