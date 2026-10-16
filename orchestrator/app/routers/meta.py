"""Routes de diagnostic : etat du systeme et introspection des droits."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..container import Services
from ..schemas import HealthResponse, ToolInfo
from ..security import AuthenticatedUser
from ..security.dependencies import get_current_user, get_services

router = APIRouter(tags=["diagnostic"])


@router.get("/health", response_model=HealthResponse, summary="Etat des dependances")
async def health(services: Services = Depends(get_services)) -> HealthResponse:
    """Verifie la gateway et les serveurs MCP declares.

    Route volontairement publique (sans jeton) : elle ne divulgue que des
    booleens, ce qui est necessaire pour la supervision.
    """
    gateway_ok = await services.gateway.health()
    servers = services.registry.server_status()
    tools_known = services.registry.count()
    status = "ok" if gateway_ok and all(servers.values()) and tools_known else "degraded"

    return HealthResponse(
        status=status,
        gateway=gateway_ok,
        mcp_servers=servers,
        tools_known=tools_known,
    )


@router.get("/tools", response_model=list[ToolInfo], summary="Outils visibles par l'appelant")
async def list_tools(
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> list[ToolInfo]:
    """Liste les outils que CET utilisateur peut utiliser.

    Le filtrage se fait ici comme partout ailleurs : la reponse est derivee des
    droits portes par le jeton, pas d'une liste globale.
    """
    grants = services.policy.grants_for(user.roles)
    visible = services.registry.filter(set(grants.keys()))
    return [
        ToolInfo(
            name=tool.name,
            server=tool.server,
            description=tool.description,
            input_schema=tool.input_schema,
        )
        for tool in visible
    ]


@router.get("/policy", summary="Politique chargee et droits de l'appelant")
async def policy_view(
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> dict:
    """Vue de diagnostic : ce qui est charge, et ce que l'appelant obtient."""
    grants = services.policy.grants_for(user.roles)
    return {
        "policy": services.policy.snapshot(),
        "caller": {
            "sub": user.sub,
            "roles": user.roles,
            "grants": {name: grant.as_dict() for name, grant in grants.items()},
        },
    }
