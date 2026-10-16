"""Dependances FastAPI : injection de la configuration, des services, de l'utilisateur.

Une dependance FastAPI est une fonction qui prepare ce dont la route a besoin
(ici : le porteur du jeton, la configuration, les services). C'est le mecanisme
qui evite de repeter la meme logique d'authentification dans chaque route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import Settings, get_settings
from ..security import AuthenticatedUser, JwtService, TokenError

if TYPE_CHECKING:  # pragma: no cover - uniquement pour les outils de typage
    from ..container import Services

# auto_error=False : on veut gerer nous-memes le 401 pour renvoyer un en-tete
# WWW-Authenticate correct (exigence de la RFC 6750).
bearer_scheme = HTTPBearer(auto_error=False, description="Jeton JWT Bearer")


def get_services(request: Request) -> "Services":
    """Recupere les services construits au demarrage (voir container.py)."""
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - ne doit pas arriver en pratique
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="services non initialises",
        )
    return services


def get_jwt_service(services: "Services" = Depends(get_services)) -> JwtService:
    return services.jwt_service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    jwt_service: JwtService = Depends(get_jwt_service),
) -> AuthenticatedUser:
    """Verifie le jeton et retourne l'utilisateur authentifie.

    Toute erreur de jeton se traduit par un 401 identique : on ne dit pas au
    client si le jeton etait expire, mal signe ou revoque. Moins d'information
    donnee a un attaquant.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized("jeton d'acces manquant")

    try:
        return jwt_service.decode(credentials.credentials)
    except TokenError:
        raise _unauthorized("jeton d'acces invalide")


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message,
        headers={"WWW-Authenticate": 'Bearer realm="orchestrator", error="invalid_token"'},
    )


def require_roles(*required: str):
    """Fabrique de dependance : exige au moins un des roles donnes.

    Sert aux routes d'administration. Les droits sur les outils, eux, passent
    par la GPO (policy/engine.py), pas par ce mecanisme.
    """

    def dependency(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not any(role in user.roles for role in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role requis : {', '.join(required)}",
            )
        return user

    return dependency


__all__ = [
    "Settings",
    "get_settings",
    "get_services",
    "get_jwt_service",
    "get_current_user",
    "require_roles",
]
