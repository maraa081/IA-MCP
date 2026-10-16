"""Client de la gateway LiteLLM (API compatible OpenAI).

Pourquoi passer par une passerelle et pas appeler Ollama directement :
  - un seul endroit pour la cle d'acces, les quotas et les journaux d'appels ;
  - les alias de modeles ('planner', 'writer') decouplent le code du moteur :
    changer de modele = editer gateway/config.yaml ;
  - permet de router vers plusieurs moteurs (local + cloud) sans changer le code.

Le client est volontairement mince : il ne fait aucune logique metier, il
transport des messages et normalise la reponse.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings


class GatewayUnavailable(RuntimeError):
    """La gateway n'a pas pu repondre (reseau, authentification, erreur moteur)."""


@dataclass(frozen=True)
class ToolCallRequest:
    """Appel d'outil demande par le modele."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class GatewayReply:
    """Reponse normalisee de la gateway."""

    model: str
    content: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class GatewayClient:
    """Client HTTP vers la gateway LiteLLM."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.gateway_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.gateway_api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Appels --------------------------------------------------------------

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> GatewayReply:
        """Envoie une conversation et retourne la reponse normalisee."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # tool_choice = required : le modele doit choisir un outil (voir
            # llm/planner.py et l'echappatoire 'respond_directly').
            payload["tool_choice"] = tool_choice or "auto"
        if response_format:
            payload["response_format"] = response_format
        if self._settings.ollama_disable_thinking:
            # Parametre propre a Ollama, transmis tel quel par la gateway.
            # Sur un moteur qui ne le connait pas, il est ignore (drop_params).
            payload["think"] = False

        started = time.perf_counter()
        body = await self._post_with_fallback(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        return GatewayReply(
            model=body.get("model", model),
            content=(message.get("content") or "").strip(),
            tool_calls=self._extract_tool_calls(message),
            latency_ms=latency_ms,
            raw=body,
        )

    async def _post_with_fallback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Envoie la requete ; si un parametre est refuse, reessaie sans lui.

        Certains moteurs renvoient 400 sur un parametre inconnu. Plutot que de
        faire echouer la requete utilisateur, on retire les parametres optionnels
        propres a un moteur et on reessaie une fois.
        """
        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"gateway injoignable : {exc}") from exc

        if response.status_code == 400 and "think" in payload:
            retry_payload = {key: value for key, value in payload.items() if key != "think"}
            try:
                response = await self._client.post("/v1/chat/completions", json=retry_payload)
            except httpx.HTTPError as exc:
                raise GatewayUnavailable(f"gateway injoignable : {exc}") from exc

        if response.status_code == 401:
            raise GatewayUnavailable("cle de service refusee par la gateway (401)")
        if response.status_code >= 400:
            raise GatewayUnavailable(
                f"gateway a repondu {response.status_code} : {response.text[:300]}"
            )

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise GatewayUnavailable("reponse gateway illisible") from exc

    @staticmethod
    def _extract_tool_calls(message: dict[str, Any]) -> list[ToolCallRequest]:
        """Normalise les appels d'outils, quel que soit le format du moteur."""
        calls: list[ToolCallRequest] = []

        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            calls.append(
                ToolCallRequest(
                    name=str(function.get("name", "")),
                    arguments=_as_dict(function.get("arguments")),
                    call_id=str(raw_call.get("id", "")),
                )
            )

        # Certains modeles locaux repondent dans l'ancien format "function_call".
        legacy = message.get("function_call")
        if legacy:
            calls.append(
                ToolCallRequest(
                    name=str(legacy.get("name", "")),
                    arguments=_as_dict(legacy.get("arguments")),
                )
            )

        return calls

    # --- Diagnostic ----------------------------------------------------------

    async def health(self) -> bool:
        """Verifie que la gateway repond."""
        try:
            response = await self._client.get("/health/liveliness", timeout=5.0)
            if response.status_code < 400:
                return True
        except httpx.HTTPError:
            pass
        try:
            response = await self._client.get("/v1/models", timeout=5.0)
            return response.status_code < 400
        except httpx.HTTPError:
            return False


def _as_dict(value: Any) -> dict[str, Any]:
    """Accepte un objet JSON ou une chaine JSON et retourne toujours un dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
