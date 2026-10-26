"""Client du modele de decision Laya (checkpoint multilingue).

Laya n'est pas un modele de langage generatif. C'est un modele de decision
non autoregressif ("System 1") : on lui donne un etat (le texte a juger) et des
questions a choix ferme, il rend une option et des probabilites calibrees. Il
ne produit ni texte, ni `tool_calls`.

Consequence pour ce projet : Laya ne peut pas remplacer entierement le
planificateur. Il peut choisir **quel** outil appeler ; il ne peut pas extraire
les arguments (le texte a resumer, le code a documenter, la reference d'un
fichier). Le planificateur garde donc le modele de langage pour cette partie,
et n'utilise Laya que pour la decision.

Pourquoi le checkpoint multilingue : le `laya` publie dans la bibliotheque
Ollama est le checkpoint anglais. Sa fiche Hugging Face indique qu'il
"s'effondre hors anglais en restant confiant" (confiance elevee meme quand la
reponse est fausse). Les demandes de ce labo etant en francais, on utilise
`convaiinnovations/laya-multilingual` (mmBERT, 100+ langues), servi par le
sidecar `scripts/laya_server.py`.

LiteLLM ne proxy pas `/v1/systemone` : le sidecar est donc appele directement.
Ce n'est pas une rupture du principe "un seul point d'entree vers les modeles" :
Laya n'est pas un modele de generation et ne consomme pas la gateway. En cas
d'indisponibilite, le planificateur retombe sur le mode `auto` (qwen) : Laya
est un accelerateur, jamais un point de defaillance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings


class LayaUnavailable(RuntimeError):
    """Le sidecar Laya est injoignable, lent, ou rend une reponse inexploitable."""


@dataclass(frozen=True)
class LayaDecision:
    """Decision rendue par Laya pour une question a choix ferme."""

    choice: str
    confidence: float
    probabilities: dict[str, float]

    @property
    def margin(self) -> float:
        """Ecart entre les deux options les plus probables.

        La confiance de Laya mesure la concentration de la distribution, pas la
        chance d'avoir raison. La marge est plus prudente : quand deux options
        sont a quasi-egalite, on prefere ne pas decider et laisser le modele de
        langage reprendre la main.
        """
        values = sorted(self.probabilities.values(), reverse=True)
        if len(values) < 2:
            return 1.0
        return values[0] - values[1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "confidence": self.confidence,
            "margin": self.margin,
            "probabilities": self.probabilities,
        }


class LayaClient:
    """Client HTTP du sidecar Laya (endpoint `/v1/systemone`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.laya_url.rstrip("/"),
            timeout=settings.laya_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def choose(
        self,
        *,
        state: str,
        criteria: dict[str, str],
        instructions: str,
        question_name: str = "tool",
    ) -> LayaDecision:
        """Pose une question a choix ferme et retourne l'option gagnante.

        `criteria` associe chaque option a une description. Laya attend entre 2
        et 26 options ; au-dela, la tete de decision dilue chaque libelle.
        """
        if len(criteria) < 2:
            raise LayaUnavailable("Laya a besoin d'au moins deux options")

        payload = {
            "model": self._settings.laya_model,
            "state": state,
            "questions": {
                question_name: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }

        try:
            response = await self._client.post("/v1/systemone", json=payload)
        except httpx.HTTPError as exc:
            raise LayaUnavailable(f"sidecar Laya injoignable : {exc}") from exc

        if response.status_code >= 400:
            raise LayaUnavailable(
                f"Laya a repondu {response.status_code} : {response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise LayaUnavailable("reponse Laya illisible") from exc

        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            answers = body if isinstance(body, dict) else {}
        answer = answers.get(question_name)
        if not isinstance(answer, dict):
            raise LayaUnavailable("Laya n'a pas rendu de reponse pour la question")

        choice = answer.get("choice")
        if not isinstance(choice, str) or not choice.strip():
            raise LayaUnavailable("Laya n'a pas rendu de choix exploitable")

        probabilities = answer.get("probabilities") or {}
        if not isinstance(probabilities, dict):
            probabilities = {}
        probabilities = {
            str(name): float(value)
            for name, value in probabilities.items()
            if isinstance(value, (int, float))
        }

        try:
            confidence = float(answer.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        return LayaDecision(
            choice=choice.strip(),
            confidence=max(0.0, min(1.0, confidence)),
            probabilities=probabilities,
        )
