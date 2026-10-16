"""Routes d'authentification.

ATTENTION, a lire avant de s'inspirer de ce fichier :
la route POST /auth/token est une SIMULATION de fournisseur d'identite. Elle
compare un couple identifiant/mot de passe stocke en configuration. C'est
acceptable pour un labo sur reseau local, et inacceptable en production.

En production, on remplace :
  - l'emission par un vrai fournisseur d'identite (Keycloak, Entra ID, Okta) ;
  - le mot de passe par un vrai facteur d'authentification (MFA, SSO) ;
  - et l'orchestrateur ne conserve que GET /auth/jwks et la verification.

Le reste du code (verification, GPO, audit) ne change pas : c'est justement
l'interet d'avoir isole cette route dans son propre module.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, status

from ..audit import AuditLogger
from ..container import Services
from ..schemas import TokenRequest, TokenResponse, UserInfo
from ..security import AuthenticatedUser
from ..security.dependencies import get_current_user, get_services

router = APIRouter(prefix="/auth", tags=["authentification"])


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtenir un jeton d'acces (demonstration)",
)
async def create_token(
    payload: TokenRequest,
    services: Services = Depends(get_services),
) -> TokenResponse:
    """Verifie les identifiants de demonstration et emet un JWT signe."""
    settings = services.settings

    # Comparaison a temps constant : on ne veut pas qu'un attaquant puisse
    # deviner le mot de passe en mesurant le temps de reponse.
    username_ok = hmac.compare_digest(payload.username, settings.dev_username)
    password_ok = hmac.compare_digest(payload.password, settings.dev_password)

    if not (username_ok and password_ok):
        await services.audit.record(
            {
                "trace_id": AuditLogger.new_trace_id(),
                "event": "auth.failed",
                "sub": payload.username,
                "allowed": False,
                "rule": "auth.invalid_credentials",
            }
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="identifiants invalides",
            headers={"WWW-Authenticate": 'Bearer realm="orchestrator"'},
        )

    roles = settings.dev_role_list
    token, jti = services.jwt_service.issue_access_token(sub=payload.username, roles=roles)

    await services.audit.record(
        {
            "trace_id": AuditLogger.new_trace_id(),
            "event": "auth.token_issued",
            "sub": payload.username,
            "roles": roles,
            "jti": jti,
            "allowed": True,
        }
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_seconds,
        roles=roles,
    )


@router.get(
    "/jwks",
    summary="Cle publique de signature (format JWKS)",
)
async def jwks(services: Services = Depends(get_services)) -> dict:
    """Publie la cle publique : permet a d'autres services de verifier nos jetons."""
    return services.jwt_service.jwks()


@router.get("/me", response_model=UserInfo, summary="Identite et droits effectifs")
async def me(
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> UserInfo:
    """Retourne l'identite portee par le jeton et ce qu'elle a le droit de faire."""
    grants = services.policy.grants_for(user.roles)
    return UserInfo(
        sub=user.sub,
        roles=user.roles,
        tools={name: grant.as_dict() for name, grant in grants.items()},
        limits=services.policy.limits,
        token_expires_at=user.expires_at,
    )


@router.post("/revoke", summary="Revoquer son propre jeton")
async def revoke(
    user: AuthenticatedUser = Depends(get_current_user),
    services: Services = Depends(get_services),
) -> dict:
    """Revoque le jeton courant.

    A utiliser a la deconnexion, ou immediatement en cas de fuite de jeton.
    """
    services.jwt_service.revoke(user.jti, user.expires_at)
    await services.audit.record(
        {
            "trace_id": AuditLogger.new_trace_id(),
            "event": "auth.token_revoked",
            "sub": user.sub,
            "jti": user.jti,
            "allowed": True,
        }
    )
    return {"revoked": True, "jti": user.jti, "revoked_tokens": services.revocation.size()}
