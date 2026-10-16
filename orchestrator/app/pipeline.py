"""Le coeur de l'orchestrateur : le pipeline d'une requete.

Une requete traverse six etapes, toujours dans le meme ordre. Lire cette
fonction, c'est lire l'architecture :

  1. controle du message       -> plafonds globaux de la GPO
  2. droits de l'utilisateur   -> quels outils il peut voir
  3. planification             -> le modele propose des outils (ou repond)
  4. autorisation + execution  -> la GPO tranche, puis on appelle les serveurs MCP
  5. synthese                  -> redaction de la reponse finale
  6. audit                     -> trace complete de la decision

Point capital : a l'etape 3, le modele ne recoit QUE les outils autorises. A
l'etape 4, chaque appel est revalide avant execution. Ces deux verrous
volontairement redondants sont la reponse a la question "et si le modele dit
n'importe quoi ?" : il peut le dire, cela ne change rien aux droits.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLogger
from .config import Settings
from .llm import GatewayClient, Synthesizer, ToolPlanner
from .mcp import McpClient, ToolRegistry
from .policy import PolicyEngine
from .security import AuthenticatedUser

# Nombre de caracteres d'un resultat d'outil conserves dans la reponse HTTP.
# La reponse complete reste disponible cote serveur (journal d'audit).
MAX_PREVIEW_CHARS = 1500


class PolicyViolation(RuntimeError):
    """La requete est refusee avant meme d'atteindre le modele."""

    def __init__(self, reason: str, rule: str = "policy") -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


@dataclass
class ToolCallTrace:
    """Trace d'un appel d'outil dans la reponse."""

    tool: str
    arguments: dict[str, Any]
    decision: str            # allowed | denied | error
    reason: str | None = None
    duration_ms: int | None = None
    output_preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "decision": self.decision,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "output_preview": self.output_preview,
        }


@dataclass
class PipelineOutcome:
    """Resultat complet d'une requete."""

    trace_id: str
    answer: str
    model_used: str
    tools_used: list[str] = field(default_factory=list)
    tool_trace: list[ToolCallTrace] = field(default_factory=list)
    denials: list[str] = field(default_factory=list)
    planner_mode: str = "none"
    latency_ms: int = 0


class ChatPipeline:
    """Enchaine les etapes de traitement d'un message utilisateur."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy: PolicyEngine,
        registry: ToolRegistry,
        mcp_client: McpClient,
        gateway: GatewayClient,
        planner: ToolPlanner,
        synthesizer: Synthesizer,
        audit: AuditLogger,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._registry = registry
        self._mcp = mcp_client
        self._gateway = gateway
        self._planner = planner
        self._synthesizer = synthesizer
        self._audit = audit

    async def run(
        self,
        *,
        user: AuthenticatedUser,
        message: str,
        model: str | None = None,
        synthesize: bool | None = None,
    ) -> PipelineOutcome:
        """Traite un message et retourne la reponse complete."""
        trace_id = AuditLogger.new_trace_id()
        started = time.perf_counter()

        # --- Etape 1 : plafonds globaux --------------------------------------
        prompt_decision = self._policy.check_prompt(message)
        if not prompt_decision.allowed:
            await self._audit.record(
                {
                    "trace_id": trace_id,
                    "event": "chat.rejected",
                    "sub": user.sub,
                    "roles": user.roles,
                    "jti": user.jti,
                    "rule": prompt_decision.rule,
                    "reason": prompt_decision.reason,
                    "prompt_chars": len(message),
                }
            )
            raise PolicyViolation(prompt_decision.reason, prompt_decision.rule)

        # --- Etape 2 : droits de l'utilisateur -------------------------------
        grants = self._policy.grants_for(user.roles)
        visible_tools = self._registry.filter(set(grants.keys()))
        max_calls = int(self._policy.limits.get("max_tool_calls_per_request", 3))

        # --- Etape 3 : planification -----------------------------------------
        plan = await self._planner.plan(user_message=message, tools=visible_tools)

        # --- Etape 4 : autorisation puis execution ---------------------------
        traces: list[ToolCallTrace] = []
        results: list[dict[str, Any]] = []
        denials: list[str] = []
        errors: list[str] = []
        executed = 0

        for call in plan.calls[:max_calls]:
            descriptor = self._registry.get(call.tool)

            if descriptor is None:
                reason = f"outil inconnu : '{call.tool}'"
                denials.append(reason)
                traces.append(
                    ToolCallTrace(
                        tool=call.tool, arguments=call.arguments,
                        decision="denied", reason=reason,
                    )
                )
                continue

            decision = self._policy.check_tool_call(
                sub=user.sub,
                roles=user.roles,
                tool=call.tool,
                arguments=call.arguments,
                calls_so_far=executed,
            )
            if not decision.allowed:
                denials.append(f"{call.tool} : {decision.reason}")
                traces.append(
                    ToolCallTrace(
                        tool=call.tool, arguments=call.arguments,
                        decision="denied", reason=decision.reason,
                    )
                )
                continue

            executed += 1
            result = await self._mcp.call_tool(
                server=descriptor.server,
                url=self._settings.mcp_servers[descriptor.server],
                tool=call.tool,
                arguments=call.arguments,
            )

            if not result.ok:
                # Echec de l'outil (argument invalide, serveur en erreur, delai
                # depasse) : ce n'est pas un refus de politique, c'est une
                # erreur d'execution. On les separe pour que l'utilisateur
                # sache s'il doit demander un droit ou corriger sa demande.
                errors.append(f"{call.tool} : {result.error}")
                traces.append(
                    ToolCallTrace(
                        tool=call.tool, arguments=call.arguments,
                        decision="error", reason=result.error,
                        duration_ms=result.duration_ms,
                    )
                )
                continue

            payload = result.payload if result.payload is not None else {"text": result.text}
            results.append({"tool": call.tool, "server": descriptor.server, "payload": payload})
            traces.append(
                ToolCallTrace(
                    tool=call.tool,
                    arguments=call.arguments,
                    decision="allowed",
                    reason="execute",
                    duration_ms=result.duration_ms,
                    output_preview=result.preview(MAX_PREVIEW_CHARS),
                )
            )

        # --- Etape 5 : reponse finale ----------------------------------------
        answer, model_used = await self._finalize(
            user_message=message,
            plan_content=plan.content,
            results=results,
            denials=denials,
            errors=errors,
            model=model,
            synthesize=synthesize,
        )

        # --- Etape 6 : audit -------------------------------------------------
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._audit.record(
            {
                "trace_id": trace_id,
                "event": "chat.completed",
                "sub": user.sub,
                "roles": user.roles,
                "jti": user.jti,
                "planner_mode": plan.mode,
                "tools_visible": [tool.name for tool in visible_tools],
                "tools_proposed": [call.tool for call in plan.calls],
                "tools_executed": [result["tool"] for result in results],
                "denials": denials,
                "errors": errors,
                "prompt_fingerprint": AuditLogger.fingerprint(message),
                "prompt_chars": len(message),
                "model_used": model_used,
                "latency_ms": latency_ms,
                "allowed": not denials,
            }
        )

        return PipelineOutcome(
            trace_id=trace_id,
            answer=answer,
            model_used=model_used,
            tools_used=[result["tool"] for result in results],
            tool_trace=traces,
            denials=denials,
            planner_mode=plan.mode,
            latency_ms=latency_ms,
        )

    # --- Etape 5, detail -----------------------------------------------------

    async def _finalize(
        self,
        *,
        user_message: str,
        plan_content: str,
        results: list[dict[str, Any]],
        denials: list[str],
        errors: list[str],
        model: str | None,
        synthesize: bool | None,
    ) -> tuple[str, str]:
        """Choisit la meilleure reponse disponible, sans jamais en inventer."""
        want_synthesis = (
            self._settings.synthesis_enabled if synthesize is None else synthesize
        )

        # Cas 1 : aucun outil n'a produit de resultat. Soit le modele a repondu
        # directement, soit les appels ont ete refuses ou ont echoue.
        if not results:
            if plan_content:
                return plan_content, self._settings.planner_model
            if denials or errors:
                return _explain_failure(denials, errors), "policy"
            return (
                "Aucun outil n'a pu etre appele et le modele n'a pas produit de "
                "reponse exploitable.",
                "none",
            )

        # Cas 2 : un seul outil a reussi et la synthese est desactivee. Les deux
        # outils de ce labo produisent deja un texte redige : legerer serait du
        # gaspillage d'appel de modele.
        if len(results) == 1 and not want_synthesis and not denials and not errors:
            payload = results[0]["payload"]
            return _render_payload(payload), str(payload.get("model", self._settings.writer_model))

        # Cas 3 : synthese par le modele redacteur.
        answer = await self._synthesizer.compose(
            user_message=user_message,
            tool_results=results,
            denials=denials,
            errors=errors,
            model=model,
        )
        return answer, model or self._settings.writer_model


def _explain_failure(denials: list[str], errors: list[str]) -> str:
    """Explique un echec en distinguant refus de droits et erreur d'execution.

    La distinction compte pour l'utilisateur : un refus se regle aupres de qui
    administre les droits, une erreur se regle en corrigeant la demande.
    """
    parts: list[str] = []

    if denials:
        parts.append(
            "Appels refuses par la politique de securite :\n"
            + "\n".join(f"- {denial}" for denial in denials)
        )
    if errors:
        parts.append(
            "Appels en erreur :\n" + "\n".join(f"- {error}" for error in errors)
        )

    return "La demande n'a pas pu etre traitee.\n\n" + "\n\n".join(parts)


def _render_payload(payload: dict[str, Any]) -> str:
    """Rend un resultat d'outil de facon lisible pour l'utilisateur."""
    for key in ("summary", "documentation", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(payload, ensure_ascii=False, indent=2)
