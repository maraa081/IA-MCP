"""Moteur de politiques (GPO) : lecture de config/policies.yaml et decisions.

Le fichier YAML est la source de verite. Ce module ne fait que l'interpreter et
repondre par une Decision explicable : un refus doit toujours pouvoir dire
POURQUOI, sinon il est impossible de diagnostiquer un probleme en production.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .rate_limit import RateLimiter

# Nom de l'argument que l'on controle en taille, pour chaque forme d'outil :
# un texte a resumer ou du code a documenter. Tout argument texte est mesure.
TEXT_ARGUMENTS = ("text", "code")


@dataclass(frozen=True)
class ToolGrant:
    """Droits effectifs sur un outil, apres fusion des roles."""

    name: str
    max_input_chars: int | None = None
    max_calls_per_request: int = 1
    allowed_frameworks: tuple[str, ...] | None = None     # None = pas de restriction
    rate_limit_requests: int | None = None
    rate_limit_window_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_input_chars": self.max_input_chars,
            "max_calls_per_request": self.max_calls_per_request,
            "allowed_frameworks": list(self.allowed_frameworks) if self.allowed_frameworks else None,
            "rate_limit": (
                {
                    "requests": self.rate_limit_requests,
                    "window_seconds": self.rate_limit_window_seconds,
                }
                if self.rate_limit_requests
                else None
            ),
        }


@dataclass(frozen=True)
class Decision:
    """Reponse du moteur : autorise ou refuse, avec la raison."""

    allowed: bool
    reason: str
    tool: str | None = None
    rule: str = "policy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tool": self.tool,
            "rule": self.rule,
        }


class PolicyEngine:
    """Charge la GPO et repond aux questions d'autorisation."""

    def __init__(self, policies_path: Path, rate_limiter: RateLimiter | None = None) -> None:
        self._path = Path(policies_path)
        self._limiter = rate_limiter or RateLimiter()
        self._roles: dict[str, dict[str, Any]] = {}
        self._limits: dict[str, Any] = {}
        self._version: int = 0
        self.reload()

    # --- Chargement ----------------------------------------------------------

    def reload(self) -> None:
        """(Re)charge le fichier de politiques. Peut etre appele a chaud."""
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        self._roles = raw.get("roles", {}) or {}
        self._limits = raw.get("limits", {}) or {}
        self._version = int(raw.get("version", 0))

    @property
    def version(self) -> int:
        return self._version

    @property
    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    def known_roles(self) -> list[str]:
        return sorted(self._roles.keys())

    # --- Droits effectifs ----------------------------------------------------

    def grants_for(self, roles: list[str]) -> dict[str, ToolGrant]:
        """Calcule les droits effectifs : union des roles portes par l'utilisateur.

        Fusion des contraintes quand plusieurs roles accordent le meme outil :
          - on retient la limite la plus LARGE pour max_input_chars / max_calls :
            un utilisateur qui porte deux roles doit obtenir la somme des droits,
            comme en gestion de parc ;
          - de meme pour les frameworks autorises : union des listes ;
          - pour la limite de debit on retient la plus permissive.
        """
        collected: dict[str, list[dict[str, Any]]] = {}

        for role in roles:
            role_definition = self._roles.get(role)
            if not role_definition:
                continue
            for tool_name, constraints in (role_definition.get("tools") or {}).items():
                collected.setdefault(tool_name, []).append(constraints or {})

        return {
            tool_name: self._merge_grant(tool_name, constraints)
            for tool_name, constraints in collected.items()
        }

    @staticmethod
    def _merge_grant(tool_name: str, constraints_list: list[dict[str, Any]]) -> ToolGrant:
        max_input = None
        max_calls = 0
        frameworks: list[str] = []
        frameworks_unrestricted = False
        rate_requests = None
        rate_window = None

        for constraints in constraints_list:
            value = constraints.get("max_input_chars")
            if value is not None:
                max_input = value if max_input is None else max(max_input, value)

            max_calls = max(max_calls, int(constraints.get("max_calls_per_request", 1)))

            if "allowed_frameworks" in constraints:
                for framework in constraints["allowed_frameworks"] or []:
                    if framework not in frameworks:
                        frameworks.append(framework)
            else:
                frameworks_unrestricted = True

            rate = constraints.get("rate_limit") or {}
            if rate.get("requests"):
                if rate_requests is None or rate["requests"] > rate_requests:
                    rate_requests = int(rate["requests"])
                    rate_window = int(rate.get("window_seconds", 60))

        return ToolGrant(
            name=tool_name,
            max_input_chars=max_input,
            max_calls_per_request=max(max_calls, 1),
            allowed_frameworks=None if frameworks_unrestricted else tuple(frameworks) or None,
            rate_limit_requests=rate_requests,
            rate_limit_window_seconds=rate_window,
        )

    # --- Decisions -----------------------------------------------------------

    def check_prompt(self, prompt: str) -> Decision:
        """Controle la taille du message utilisateur."""
        max_prompt = self._limits.get("max_prompt_chars")
        if max_prompt and len(prompt) > max_prompt:
            return Decision(
                allowed=False,
                reason=f"message trop long ({len(prompt)} > {max_prompt} caracteres)",
                rule="limits.max_prompt_chars",
            )
        return Decision(allowed=True, reason="message conforme", rule="limits.max_prompt_chars")

    def check_tool_call(
        self,
        *,
        sub: str,
        roles: list[str],
        tool: str,
        arguments: dict[str, Any],
        calls_so_far: int,
    ) -> Decision:
        """Autorise ou refuse un appel d'outil precis.

        Ordre des controles : droit d'acces, quota par requete, taille des
        entrees, valeur des parametres, debit. En cas de refus, la raison est
        renvoyee telle quelle au client et journalisee.
        """
        grants = self.grants_for(roles)
        grant = grants.get(tool)
        if grant is None:
            return Decision(
                allowed=False,
                tool=tool,
                reason=(
                    f"outil '{tool}' non accorde aux roles {roles or ['aucun']} "
                    "(deny by default)"
                ),
                rule="gpo.tool_not_granted",
            )

        if calls_so_far >= grant.max_calls_per_request:
            return Decision(
                allowed=False,
                tool=tool,
                reason=(
                    f"quota d'appels atteint pour '{tool}' "
                    f"({calls_so_far}/{grant.max_calls_per_request} par requete)"
                ),
                rule="gpo.max_calls_per_request",
            )

        # Taille des entrees : on mesure chaque argument texte.
        for argument_name in TEXT_ARGUMENTS:
            value = arguments.get(argument_name)
            if isinstance(value, str) and grant.max_input_chars:
                if len(value) > grant.max_input_chars:
                    return Decision(
                        allowed=False,
                        tool=tool,
                        reason=(
                            f"argument '{argument_name}' trop long "
                            f"({len(value)} > {grant.max_input_chars} caracteres)"
                        ),
                        rule="gpo.max_input_chars",
                    )

        # Valeurs autorisees : liste blanche des frameworks de documentation.
        framework = arguments.get("framework")
        if isinstance(framework, str) and grant.allowed_frameworks is not None:
            if framework not in grant.allowed_frameworks:
                return Decision(
                    allowed=False,
                    tool=tool,
                    reason=(
                        f"framework '{framework}' non autorise pour vos roles "
                        f"(autorises : {', '.join(grant.allowed_frameworks)})"
                    ),
                    rule="gpo.allowed_frameworks",
                )

        # Debit : la cle inclut l'utilisateur, pour qu'un utilisateur ne puisse
        # pas consommer le quota d'un autre.
        if not self._limiter.allow(
            f"{sub}:{tool}", grant.rate_limit_requests, grant.rate_limit_window_seconds
        ):
            return Decision(
                allowed=False,
                tool=tool,
                reason=(
                    f"limite de debit depassee pour '{tool}' "
                    f"({grant.rate_limit_requests} appels / "
                    f"{grant.rate_limit_window_seconds}s)"
                ),
                rule="gpo.rate_limit",
            )

        return Decision(allowed=True, tool=tool, reason="appel conforme a la GPO", rule="gpo.allow")

    def allowed_tool_names(self, roles: list[str]) -> list[str]:
        return sorted(self.grants_for(roles).keys())

    def snapshot(self) -> dict[str, Any]:
        """Vue lisible de la politique chargee (route /policy)."""
        return {
            "version": self._version,
            "path": str(self._path),
            "limits": self.limits,
            "roles": {
                role: sorted((definition.get("tools") or {}).keys())
                for role, definition in self._roles.items()
            },
        }
