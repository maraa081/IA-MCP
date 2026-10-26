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
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLogger
from .config import Settings
from .llm import (
    GatewayClient,
    PlannedCall,
    Synthesizer,
    ToolPlanner,
    assistant_tool_call_message,
    tool_result_message,
)
from .mcp import McpClient, ToolDescriptor, ToolRegistry
from .policy import PolicyEngine
from .security import AuthenticatedUser

# Nombre de caracteres d'un resultat d'outil conserves dans la reponse HTTP.
# La reponse complete reste disponible cote serveur (journal d'audit).
MAX_PREVIEW_CHARS = 1500

# Taille maximale d'un resultat d'outil reinjecte dans la conversation, au tour
# suivant. Borne la consommation de la fenetre de contexte.
MAX_TOOL_MESSAGE_CHARS = 8000

# Champs par lesquels un outil rend un texte deja redige. "resume" est le nom
# utilise par le serveur de code indexe, les autres viennent des autres outils.
TEXT_KEYS = ("summary", "documentation", "resume", "text")


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
    # Regle de la GPO (ou du garde-fou) qui a produit la decision. Elle est
    # journalisee et exposee dans la reponse : un refus sans regle est
    # impossible a diagnostiquer, et impossible a agreger en metriques.
    rule: str | None = None
    duration_ms: int | None = None
    output_preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "decision": self.decision,
            "reason": self.reason,
            "rule": self.rule,
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
        history: list[dict[str, Any]] | None = None,
        allowed_tools: set[str] | None = None,
    ) -> PipelineOutcome:
        """Traite un message et retourne la reponse complete.

        `history` : tours precedents de la conversation (role/contenu), pour
        qu'Open WebUI puisse enchainer plusieurs messages. Les droits ne sont
        jamais tires de l'historique : seuls le jeton et la GPO font foi.

        `allowed_tools` : restriction volontaire supplementaire (un modele
        Open WebUI = un jeu d'outils). Ce n'est qu'une INTERSECTION avec les
        droits : elle ne peut jamais elargir ce que la GPO autorise.
        """
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
        if allowed_tools is not None:
            # Intersection, jamais union : la GPO reste la seule source de
            # verite. Un modele ne peut que retirer des outils.
            visible_tools = [tool for tool in visible_tools if tool.name in allowed_tools]
        max_calls = int(self._policy.limits.get("max_tool_calls_per_request", 3))
        want_synthesis = (
            self._settings.synthesis_enabled if synthesize is None else synthesize
        )

        # --- Etapes 3 et 4 : boucle planification, autorisation, execution ----
        # Le modele est rappele apres chaque tour d'outils : c'est ce qui lui
        # permet, par exemple, de chercher un fichier dans la base puis de le
        # resumer au tour suivant. La boucle est bornee : une erreur de
        # modelisation ne peut pas faire tourner l'orchestrateur indefiniment.
        conversation: list[dict[str, Any]] = list(history or [])
        conversation.append({"role": "user", "content": message})
        traces: list[ToolCallTrace] = []
        results: list[dict[str, Any]] = []
        denials: list[str] = []
        denial_details: list[dict[str, Any]] = []
        errors: list[str] = []
        error_details: list[dict[str, Any]] = []
        proposed: list[str] = []
        executed = 0
        # Compteur par outil. Le quota de la GPO est un quota PAR OUTIL : passer
        # le total global ferait refuser documenter_code apres un chercher_code,
        # alors que les deux outils ont chacun leur propre limite.
        executed_by_tool: dict[str, int] = {}
        rounds = 0
        planner_mode = "none"
        answer = ""

        for _ in range(max(1, self._settings.max_tool_rounds)):
            rounds += 1
            plan = await self._planner.plan(messages=conversation, tools=visible_tools)
            planner_mode = plan.mode
            proposed.extend(call.tool for call in plan.calls)

            if not plan.calls:
                # Le modele repond en texte : fin de la boucle.
                answer = plan.content
                break

            # Le message assistant liste TOUS les appels demandes : le protocole
            # impose un resultat pour chacun, y compris pour un appel refuse.
            conversation.append(assistant_tool_call_message(plan.calls))

            for call in plan.calls:
                descriptor = self._registry.get(call.tool)
                texte, trace, payload = await self._execute_call(
                    call=call,
                    user=user,
                    descriptor=descriptor,
                    executed=executed,
                    calls_for_tool=executed_by_tool.get(call.tool, 0),
                    max_calls=max_calls,
                )
                traces.append(trace)

                if trace.decision == "allowed":
                    executed += 1
                    executed_by_tool[call.tool] = (
                        executed_by_tool.get(call.tool, 0) + 1
                    )
                    if payload is not None:
                        results.append(
                            {
                                "tool": call.tool,
                                "server": descriptor.server,
                                "payload": payload,
                            }
                        )
                elif trace.decision == "denied":
                    denials.append(f"{call.tool} : {trace.reason}")
                    denial_details.append(
                        {
                            "tool": call.tool,
                            "reason": trace.reason,
                            "rule": trace.rule,
                        }
                    )
                else:
                    errors.append(f"{call.tool} : {trace.reason}")
                    error_details.append(
                        {
                            "tool": call.tool,
                            "reason": trace.reason,
                            "rule": trace.rule,
                        }
                    )

                conversation.append(tool_result_message(call, texte))

            # Inventaire : le resultat contient deja la liste des fichiers.
            # Un second tour risquerait un nouvel appel (refuse par le quota
            # 1/1 de la GPO) sans rien apporter. On s'arrete apres le premier
            # appel, que la synthese soit activee ou non.
            if len(results) == 1 and results[0]["tool"] == "code_disponible":
                break

            # Sortie anticipee : l'outil a deja rendu un texte redige. Un
            # second tour de planification risquerait un nouvel appel (refuse
            # par le quota 1/1) sans rien apporter. Si la synthese est activee,
            # elle partira de ce resultat unique.
            if len(results) == 1 and _contient_un_texte(results[0]["payload"]):
                break

        # --- Etape 5 : reponse finale ----------------------------------------
        if answer:
            # Le modele a conclu lui-meme, apres avoir vu les resultats.
            model_used = self._settings.planner_model
        else:
            # Il n'a rien produit d'exploitable : on compose a partir des
            # resultats obtenus, ou on explique l'echec.
            answer, model_used = await self._finalize(
                user_message=message,
                plan_content="",
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
                "planner_mode": planner_mode,
                "rounds": rounds,
                "tools_visible": [tool.name for tool in visible_tools],
                "tools_proposed": proposed,
                "tools_executed": [result["tool"] for result in results],
                "denials": denials,
                "denial_details": denial_details,
                "errors": errors,
                "error_details": error_details,
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
            planner_mode=planner_mode,
            latency_ms=latency_ms,
        )

    # --- Execution d'un appel d'outil ----------------------------------------

    async def _execute_call(
        self,
        *,
        call: PlannedCall,
        user: AuthenticatedUser,
        descriptor: ToolDescriptor | None,
        executed: int,
        calls_for_tool: int,
        max_calls: int,
    ) -> tuple[str, ToolCallTrace, dict[str, Any] | None]:
        """Autorise puis execute un appel.

        Retourne (texte pour le modele, trace, donnees). Le texte est ajoute a la
        conversation : c'est ce que le modele lira au tour suivant, refus et
        erreurs compris, ce qui lui permet de se corriger.
        """
        if descriptor is None:
            reason = f"outil inconnu : '{call.tool}'"
            return (
                reason,
                ToolCallTrace(
                    tool=call.tool,
                    arguments=call.arguments,
                    decision="denied",
                    reason=reason,
                    rule="gpo.tool_not_granted",
                ),
                None,
            )

        # Nettoie les arguments du modele avant la GPO et l'appel MCP :
        # "None" en chaine de caracteres est traite comme un argument absent.
        arguments = _sanitize_arguments(call.arguments, descriptor.input_schema)

        if executed >= max_calls:
            reason = f"quota global d'appels atteint ({max_calls} par requete)"
            return (
                reason,
                ToolCallTrace(
                    tool=call.tool,
                    arguments=arguments,
                    decision="denied",
                    reason=reason,
                    rule="limits.max_tool_calls_per_request",
                ),
                None,
            )

        decision = self._policy.check_tool_call(
            sub=user.sub,
            roles=user.roles,
            tool=call.tool,
            arguments=arguments,
            calls_so_far=calls_for_tool,
        )
        if not decision.allowed:
            return (
                decision.reason,
                ToolCallTrace(
                    tool=call.tool,
                    arguments=arguments,
                    decision="denied",
                    reason=decision.reason,
                    rule=decision.rule,
                ),
                None,
            )

        result = await self._mcp.call_tool(
            server=descriptor.server,
            url=self._settings.mcp_servers[descriptor.server],
            tool=call.tool,
            arguments=arguments,
        )

        if not result.ok:
            # Echec d'execution (argument refuse par l'outil, delai depasse,
            # serveur en erreur) : ce n'est pas un refus de droits.
            reason = result.error or "erreur d'execution"
            return (
                f"erreur : {reason}",
                ToolCallTrace(
                    tool=call.tool,
                    arguments=arguments,
                    decision="error",
                    reason=reason,
                    rule="tool.execution_error",
                    duration_ms=result.duration_ms,
                ),
                None,
            )

        payload = result.payload if result.payload is not None else {"text": result.text}
        texte = json.dumps(payload, ensure_ascii=False)[:MAX_TOOL_MESSAGE_CHARS]
        return (
            texte,
            ToolCallTrace(
                tool=call.tool,
                arguments=arguments,
                decision="allowed",
                reason="execute",
                rule=decision.rule,
                duration_ms=result.duration_ms,
                output_preview=result.preview(MAX_PREVIEW_CHARS),
            ),
            payload,
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


def _sanitize_arguments(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """Nettoie les arguments d'un appel avant la GPO et le serveur MCP.

    Les petits modeles rendent parfois la chaine "None" au lieu d'omettre un
    argument optionnel. L'outil refuse alors l'appel (validation Pydantic) et
    l'utilisateur voit une erreur au lieu du resultat. On supprime ces valeurs,
    puis on convertit les types simples d'apres le schema de l'outil.
    """
    properties = (schema or {}).get("properties") or {}
    cleaned: dict[str, Any] = {}

    for key, value in (arguments or {}).items():
        if value is None:
            continue

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.lower() in {"none", "null", "nil", "undefined"}:
                continue
            expected = (properties.get(key) or {}).get("type")
            if expected == "integer" and re.fullmatch(r"-?\d+", stripped):
                cleaned[key] = int(stripped)
                continue
            if expected == "number":
                try:
                    cleaned[key] = float(stripped)
                    continue
                except ValueError:
                    pass
            if expected == "boolean" and stripped.lower() in {"true", "false"}:
                cleaned[key] = stripped.lower() == "true"
                continue
            cleaned[key] = value
            continue

        if (
            isinstance(value, float)
            and (properties.get(key) or {}).get("type") == "integer"
            and value.is_integer()
        ):
            cleaned[key] = int(value)
            continue

        cleaned[key] = value

    return cleaned


def _contient_un_texte(payload: dict[str, Any]) -> bool:
    """Vrai si l'outil a deja rendu un texte redige (resume, documentation).

    Sert a la sortie anticipee de la boucle : si l'outil a deja produit la
    prose attendue et que la synthese est desactivee, rappeler le modele
    n'apporterait rien. Un resultat qui n'est qu'une liste de fichiers, par
    exemple, n'est pas un texte redige : la boucle continue.
    """
    for cle in TEXT_KEYS:
        valeur = payload.get(cle)
        if isinstance(valeur, str) and valeur.strip():
            return True
    return False


def _render_payload(payload: dict[str, Any]) -> str:
    """Rend un resultat d'outil de facon lisible pour l'utilisateur."""
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(payload, ensure_ascii=False, indent=2)
