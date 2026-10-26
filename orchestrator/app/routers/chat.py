"""Route principale : le dialogue avec l'orchestrateur."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..container import Services
from ..pipeline import PolicyViolation
from ..schemas import ChatRequest, ChatResponse, ToolCallTrace
from ..security import AuthenticatedUser
from ..security.dependencies import get_current_user, get_services

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["dialogue"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Envoyer un message a l'orchestrateur",
)
async def chat(
    payload: ChatRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> ChatResponse:
    """Traite un message : planification, autorisation, execution, synthese.

    Toute la logique est dans ChatPipeline.run() : cette route ne fait que
    traduire une exception en code HTTP et un resultat en reponse JSON.
    """
    try:
        outcome = await services.pipeline.run(
            user=user,
            message=payload.message,
            model=payload.model,
            synthesize=payload.synthesize,
        )
    except PolicyViolation as violation:
        # Refus avant appel au modele : 403, avec la regle precise en en-tete.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=violation.reason,
            headers={"X-Policy-Rule": violation.rule},
        ) from violation
    except Exception as exc:  # noqa: BLE001
        # On journalise le detail cote serveur, mais on ne le renvoie pas au
        # client : les messages d'erreur internes renseignent un attaquant.
        logger.exception("Echec du pipeline de dialogue")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="le traitement de la demande a echoue",
        ) from exc

    return ChatResponse(
        trace_id=outcome.trace_id,
        answer=outcome.answer,
        model_used=outcome.model_used,
        tools_used=outcome.tools_used,
        tool_trace=[
            ToolCallTrace(
                tool=trace.tool,
                arguments=trace.arguments,
                decision=trace.decision,
                reason=trace.reason,
                rule=trace.rule,
                duration_ms=trace.duration_ms,
                output_preview=trace.output_preview,
            )
            for trace in outcome.tool_trace
        ],
        planner_mode=outcome.planner_mode,
        policy_denials=outcome.denials,
        latency_ms=outcome.latency_ms,
    )
