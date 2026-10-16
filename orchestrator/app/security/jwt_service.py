"""Emission et verification des jetons JWT.

Choix de conception, a assumer et a expliquer en entretien :
  - algorithme RS256 (asymetrique) : la verification ne demande que la cle
    publique ;
  - liste blanche d'algorithmes a la verification : empeche l'attaque par
    confusion d'algorithme (alg=none, ou HS256 signe avec la cle publique) ;
  - claims obligatoires (require) : un jeton sans 'exp', 'iss', 'aud', 'sub' ou
    'jti' est rejete, meme si la signature est valide ;
  - 'jti' unique : permet de revoquer un jeton precis avant son expiration ;
  - roles dans le jeton : l'orchestrateur ne consulte pas d'annuaire a chaque
    appel (le jeton est la source d'identite, la GPO la source des droits).

En production, ce module ne fait plus que VERIFIER : l'emission revient a un
fournisseur d'identite (Keycloak, Entra ID, Okta...). Le reste du code est
identique, ce qui est exactement le but recherche.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from ..config import Settings
from .keys import load_private_key, load_public_key, public_key_to_jwk
from .revocation import RevocationStore


class TokenError(Exception):
    """Jeton absent, malforme, expire, mal signe ou revoque."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identite resolue a partir d'un jeton valide."""

    sub: str
    roles: list[str]
    scopes: list[str]
    jti: str
    issued_at: int
    expires_at: int
    claims: dict[str, Any] = field(default_factory=dict)


# Claims exiges : un jeton incomplet est refuse.
REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]


class JwtService:
    """Service d'emission et de verification des jetons d'acces."""

    def __init__(self, settings: Settings, revocation: RevocationStore) -> None:
        self._settings = settings
        self._revocation = revocation
        self._private_key: RSAPrivateKey = load_private_key(settings.private_key_path)
        self._public_key: RSAPublicKey = load_public_key(settings.public_key_path)

    # --- Emission ------------------------------------------------------------

    def issue_access_token(
        self,
        *,
        sub: str,
        roles: list[str],
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[str, str]:
        """Emettre un jeton. Retourne (jeton, jti)."""
        settings = self._settings
        now = int(time.time())
        ttl = ttl_seconds or settings.access_token_ttl_seconds
        jti = uuid.uuid4().hex

        payload: dict[str, Any] = {
            "iss": settings.issuer,
            "aud": settings.audience,
            "sub": sub,
            "roles": roles,
            "scopes": scopes or [],
            "jti": jti,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "typ": "access",
        }
        token = jwt.encode(
            payload,
            self._private_key,
            algorithm=settings.jwt_algorithm,
            headers={"kid": settings.jwt_key_id},
        )
        return token, jti

    # --- Verification --------------------------------------------------------

    def decode(self, token: str) -> AuthenticatedUser:
        """Verifier un jeton et retourner l'identite associee."""
        settings = self._settings
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=[settings.jwt_algorithm],   # liste blanche stricte
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.jwt_leeway_seconds,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("jeton expire") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError("audience invalide") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError("emetteur invalide") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenError(f"claim obligatoire manquant : {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"jeton invalide : {exc}") from exc

        jti = str(claims.get("jti", ""))
        if self._revocation.is_revoked(jti):
            raise TokenError("jeton revoque")

        roles = claims.get("roles") or []
        if isinstance(roles, str):          # tolerance : "analyst" au lieu de [...]
            roles = [roles]

        return AuthenticatedUser(
            sub=str(claims.get("sub", "")),
            roles=[str(role) for role in roles],
            scopes=[str(scope) for scope in (claims.get("scopes") or [])],
            jti=jti,
            issued_at=int(claims.get("iat", 0)),
            expires_at=int(claims.get("exp", 0)),
            claims=claims,
        )

    # --- Publication et revocation -------------------------------------------

    def jwks(self) -> dict[str, Any]:
        """Cle publique au format JWKS, pour les autres services."""
        return {
            "keys": [
                public_key_to_jwk(
                    self._public_key, self._settings.jwt_key_id, self._settings.jwt_algorithm
                )
            ]
        }

    def revoke(self, jti: str, expires_at: int) -> None:
        """Revoquer un jeton (deconnexion, incident de securite)."""
        self._revocation.revoke(jti, expires_at)
