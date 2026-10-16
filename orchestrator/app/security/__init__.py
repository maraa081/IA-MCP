"""Securite : materiel cryptographique, emission et verification des jetons.

Trois responsabilites separees, dans trois modules :
  - keys.py       : fabriquer et charger la paire de cles RSA (RS256) ;
  - jwt_service.py: emettre et verifier les jetons JWT ;
  - revocation.py : empecher la reutilisation d'un jeton pourtant valide.
"""

from .jwt_service import AuthenticatedUser, JwtService, TokenError
from .keys import ensure_keypair, generate_keypair, public_key_to_jwk
from .revocation import RevocationStore

__all__ = [
    "AuthenticatedUser",
    "JwtService",
    "TokenError",
    "RevocationStore",
    "ensure_keypair",
    "generate_keypair",
    "public_key_to_jwk",
]
