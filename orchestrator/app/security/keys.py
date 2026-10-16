"""Gestion de la paire de cles de signature (RS256).

Pourquoi RS256 et pas HS256 : avec un algorithme asymetrique, le service qui
VERIFIE les jetons n'a besoin que de la cle publique. Un orchestrateur compromis
ne peut donc pas fabriquer de nouveaux jetons. C'est la base pour pouvoir
externaliser la verification (autres services, passerelle d'API) sans partager
de secret.

La cle privee ne doit JAMAIS etre versionnee : elle vit dans config/keys/, qui
est dans le .gitignore. En production, elle est fournie par un coffre-fort de
secrets (Vault, KMS, Secrets Manager...).
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_keypair(
    private_key_path: Path,
    public_key_path: Path,
    key_size: int = 2048,
) -> None:
    """Cree une paire de cles RSA sur disque (droits 0600 sur la cle privee)."""
    private_key_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o600)

    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_key_path.chmod(0o644)


def ensure_keypair(private_key_path: Path, public_key_path: Path) -> bool:
    """Cree la paire si elle n'existe pas. Retourne True si elle a ete creee."""
    if private_key_path.exists() and public_key_path.exists():
        return False
    generate_keypair(private_key_path, public_key_path)
    return True


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_public_key(path: Path):
    return serialization.load_pem_public_key(path.read_bytes())


def public_key_to_jwk(public_key, key_id: str, algorithm: str = "RS256") -> dict:
    """Convertit une cle publique RSA au format JWK (RFC 7517).

    Publier le JWK permet a d'autres services de verifier nos jetons sans
    partager de secret, et de gerer une rotation de cles (champ 'kid').
    """
    numbers = public_key.public_numbers()

    def to_base64url(value: int, length: int) -> str:
        raw = value.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": algorithm,
        "kid": key_id,
        "n": to_base64url(numbers.n, (numbers.n.bit_length() + 7) // 8),
        "e": to_base64url(numbers.e, (numbers.e.bit_length() + 7) // 8),
    }
