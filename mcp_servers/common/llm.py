"""Client minimal vers la gateway LiteLLM (API OpenAI-compatible).

Point cle de l'architecture : les serveurs MCP ne parlent JAMAIS directement a
Ollama. Ils passent par la gateway, qui centralise les cles, les quotas et les
journaux. Changer de moteur d'inference = editer gateway/config.yaml, pas ce
fichier.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import ServerSettings


class GatewayError(RuntimeError):
    """Erreur remontee par la gateway (reseau, authentification, modele...)."""


async def complete(
    settings: ServerSettings,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 900,
) -> str:
    """Envoie une conversation a la gateway et renvoie le texte repondu.

    Leve GatewayError si la gateway repond autre chose que 200, ou si la reponse
    ne contient pas de contenu exploitable.
    """
    payload: dict[str, Any] = {
        "model": model or settings.writer_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    # Desactiver la phase de reflexion du modele. Ce n'est pas un reglage de
    # confort : mesure sur la pile du labo, un modele de raisonnement a qui l'on
    # donne 360 tokens de budget les consomme entierement en reflexion et rend un
    # contenu VIDE (28 s pour rien). Avec ce reglage : 336 caracteres de resume en
    # 5 s. Consequence pratique : tout appel d'outil doit fixer ce parametre, sinon
    # les outils renvoient des resultats vides de facon intermittente.
    if settings.disable_thinking:
        payload["think"] = False

    headers = {
        "Authorization": f"Bearer {settings.gateway_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(
                settings.chat_completions_url, json=payload, headers=headers
            )
            # Certains moteurs refusent un parametre qu'ils ne connaissent pas :
            # on retire le notre et on reessaie une fois, plutot que de faire
            # echouer l'appel de l'utilisateur.
            if response.status_code == 400 and "think" in payload:
                payload.pop("think")
                response = await client.post(
                    settings.chat_completions_url, json=payload, headers=headers
                )
    except httpx.HTTPError as exc:  # reseau, DNS, timeout...
        raise GatewayError(f"gateway injoignable : {exc}") from exc

    if response.status_code != 200:
        raise GatewayError(
            f"gateway a repondu {response.status_code} : {response.text[:300]}"
        )

    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise GatewayError(f"reponse gateway inattendue : {body}") from exc

    content = (content or "").strip()
    if not content:
        # Un contenu vide est une panne silencieuse : mieux vaut la signaler que
        # rendre un resultat vide a l'utilisateur. La cause la plus frequente est
        # un budget de tokens entierement consomme par la phase de reflexion.
        raise GatewayError(
            "le modele a repondu un contenu vide (budget de tokens epuise par la "
            "phase de reflexion ? voir OLLAMA_DISABLE_THINKING)"
        )

    return content
