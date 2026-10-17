"""Passerelle OpenAI : le chemin par lequel Open WebUI parle a l'orchestrateur.

Open WebUI ne dialogue pas avec notre API maison : il attend une API
"compatible OpenAI". Ce module fait donc la traduction dans les deux sens, et
c'est desormais le SEUL point d'entree utilise en pratique :

    navigateur -> Open WebUI -> /v1/chat/completions (ici) -> pipeline
                                                              |
                                                              +-> gateway LiteLLM -> Ollama
                                                              +-> serveurs MCP

Pourquoi ne pas laisser Open WebUI appeler les outils lui-meme (passerelle
mcpo) ? Parce que ce chemin court-circuite le JWT, la GPO et le journal
d'audit : les outils seraient appeles sans qu'aucune regle ne soit verifiee.
Ici, chaque message traverse le pipeline complet.

Choix des outils par l'utilisateur : Open WebUI ne propose plus de cases a
cocher (elles contournaient la GPO). A la place, chaque "modele" de la liste
correspond a un jeu d'outils. Choisir un modele dans la conversation, c'est
choisir ce que l'assistant a le droit d'appeler. C'est plus simple a utiliser,
et la GPO reste la seule source de verite : un modele ne peut pas elargir les
droits, il peut seulement les restreindre.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Settings
from ..container import Services
from ..pipeline import PolicyViolation
from ..security import AuthenticatedUser
from ..security.dependencies import get_current_user, get_services

router = APIRouter(prefix="/v1", tags=["openai"])

# --- Catalogue des modeles ---------------------------------------------------
# Un seul modele, volontairement : l'utilisateur ne doit pas avoir a choisir son
# outil avant de savoir ce qu'il va demander. Le planificateur propose l'outil,
# la GPO tranche, et le pipeline repond.
#
# `tools` = None signifie "tous les outils autorises par la GPO". Un ensemble
# nomme restreindrait le catalogue (intersection, jamais elargissement) : c'est
# la porte ouverte si un jour un modele specialise redevient utile.
MODELES: dict[str, dict[str, Any]] = {
    "Assistant": {
        "tools": None,
        "description": "Choisit automatiquement les outils autorises par la GPO",
    },
}

MODELE_PAR_DEFAUT = "Assistant"


def _erreur(status_code: int, message: str, type_: str = "invalid_request_error") -> JSONResponse:
    """Erreur au format OpenAI (Open WebUI affiche `error.message`)."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": type_, "code": status_code}},
    )


def _texte(contenu: Any) -> str:
    """Le champ `content` d'un message OpenAI peut etre une chaine ou une liste
    de parties (multimodal). On ne garde que le texte."""
    if isinstance(contenu, str):
        return contenu
    if isinstance(contenu, list):
        morceaux = []
        for partie in contenu:
            if isinstance(partie, dict) and partie.get("type") == "text":
                morceaux.append(str(partie.get("text", "")))
        return "\n".join(morceaux)
    return ""


def _extraire(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Separe le dernier message utilisateur de l'historique de conversation.

    L'historique est transmis au planificateur tel quel : sans lui, un
    utilisateur ne pourrait pas enchainer "resume ce fichier" puis "et celui
    d'avant ?". Les messages systeme sont ignores : le prompt du planificateur
    est fixe par le code, pas par le client.
    """
    historique: list[dict[str, str]] = []
    dernier = ""
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        contenu = _texte(message.get("content"))
        if not contenu:
            continue
        if role == "user":
            dernier = contenu
        historique.append({"role": role, "content": contenu})
    # Le dernier message utilisateur termine l'historique : on le retire pour
    # ne pas le compter deux fois.
    if historique and historique[-1]["role"] == "user":
        historique = historique[:-1]
    return dernier, historique


def _estimer_tokens(texte: str) -> int:
    """Estimation grossiere (4 caracteres par token) : suffit pour l'affichage."""
    return max(1, len(texte) // 4)


def _morceaux(texte: str, taille: int = 160) -> list[str]:
    """Decoupe la reponse en petits morceaux pour l'effet de frappe."""
    if not texte:
        return []
    return [texte[i : i + taille] for i in range(0, len(texte), taille)]


def _identifiant() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _modele(nom: str | None) -> tuple[str, set[str] | None]:
    """Resout un identifiant de modele en (nom canonique, jeu d'outils)."""
    if not nom:
        return MODELE_PAR_DEFAUT, MODELES[MODELE_PAR_DEFAUT]["tools"]
    if nom in MODELES:
        return nom, MODELES[nom]["tools"]
    # Un modele inconnu (ancienne conversation, par exemple) ne doit pas
    # echouer : on retombe sur le comportement le plus sur, aucun outil.
    return nom, set()


@router.get("/models", summary="Modeles exposes a Open WebUI")
async def lister_modeles(user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, Any]:
    """Liste des modeles. Un modele = un jeu d'outils (voir MODELES)."""
    maintenant = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": nom,
                "object": "model",
                "created": maintenant,
                "owned_by": "mcp-orchestrator-lab",
                "description": definition["description"],
            }
            for nom, definition in MODELES.items()
        ],
    }


@router.post("/chat/completions", summary="Dialogue compatible OpenAI")
async def chat_completions(
    request: Request,
    payload: dict[str, Any],
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> Any:
    """Traduit une requete OpenAI en passage dans le pipeline, puis repond.

    Deux formats de reponse : JSON simple, ou flux SSE (`stream: true`), que
    Open WebUI utilise par defaut. Le pipeline n'est pas capable de produire des
    tokens au fil de l'eau (il attend le modele) : on emet donc la reponse en
    un seul bloc decoupe en petits morceaux, ce qui suffit a l'interface.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _erreur(status.HTTP_400_BAD_REQUEST, "le champ 'messages' est obligatoire")

    message, historique = _extraire(messages)
    if not message.strip():
        return _erreur(status.HTTP_400_BAD_REQUEST, "aucun message utilisateur exploitable")

    nom_modele, outils = _modele(payload.get("model"))
    # Reglage de la synthese : les modeles "sans outil" n'en ont pas besoin, et
    # la desactiver quand un seul outil rend deja un texte redige evite un
    # appel de modele inutile (le pipeline sait le faire, voir pipeline.py).
    synthese = True

    try:
        resultat = await services.pipeline.run(
            user=user,
            message=message,
            synthesize=synthese,
            history=historique,
            allowed_tools=outils,
        )
    except PolicyViolation as violation:
        return _erreur(
            status.HTTP_403_FORBIDDEN,
            f"refuse par la politique ({violation.rule}) : {violation.reason}",
            type_="policy_violation",
        )
    except Exception:  # noqa: BLE001
        # Le detail reste dans les journaux du serveur : un message d'erreur
        # interne renseigne un attaquant.
        import logging

        logging.getLogger(__name__).exception("Echec du pipeline (passerelle OpenAI)")
        return _erreur(
            status.HTTP_502_BAD_GATEWAY,
            "le traitement de la demande a echoue",
            type_="upstream_error",
        )

    identifiant = _identifiant()
    creation = int(time.time())
    flux = bool(payload.get("stream"))

    if not flux:
        return {
            "id": identifiant,
            "object": "chat.completion",
            "created": creation,
            "model": nom_modele,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": resultat.answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": _estimer_tokens(message),
                "completion_tokens": _estimer_tokens(resultat.answer),
                "total_tokens": _estimer_tokens(message) + _estimer_tokens(resultat.answer),
            },
        }

    async def evenements() -> AsyncIterator[str]:
        """Flux SSE au format OpenAI (un delta par morceau, puis [DONE])."""
        debut = {
            "id": identifiant,
            "object": "chat.completion.chunk",
            "created": creation,
            "model": nom_modele,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(debut, ensure_ascii=False)}\n\n"
        for morceau in _morceaux(resultat.answer):
            paquet = {
                "id": identifiant,
                "object": "chat.completion.chunk",
                "created": creation,
                "model": nom_modele,
                "choices": [
                    {"index": 0, "delta": {"content": morceau}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(paquet, ensure_ascii=False)}\n\n"
        fin = {
            "id": identifiant,
            "object": "chat.completion.chunk",
            "created": creation,
            "model": nom_modele,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(fin, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        evenements(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
