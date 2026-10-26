"""Tests de la chaine d'authentification JWT.

Ces tests verifient les proprietes de securite, pas seulement le chemin nominal :
un jeton absent, expire, mal signe, destine a une autre audience ou revoque doit
etre refuse. C'est ce qui distingue un test utile d'un test decoratif.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import jwt as pyjwt
import pytest

from orchestrator.app.config import Settings
from orchestrator.app.security import JwtService, RevocationStore, TokenError
from orchestrator.app.security.keys import ensure_keypair, generate_keypair


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    keys_dir = tmp_path / "keys"
    generate_keypair(keys_dir / "private.pem", keys_dir / "public.pem")
    return Settings(
        planner_mode="auto",
        keys_dir=keys_dir,
        revocation_path=tmp_path / "revoked.json",
        policies_path=tmp_path / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        issuer="test-issuer",
        audience="test-audience",
    )


@pytest.fixture()
def service(settings: Settings) -> JwtService:
    return JwtService(settings, RevocationStore(settings.revocation_path))


def test_jeton_valide_est_accepte(service: JwtService) -> None:
    token, jti = service.issue_access_token(sub="demo", roles=["analyst"])
    user = service.decode(token)

    assert user.sub == "demo"
    assert user.roles == ["analyst"]
    assert user.jti == jti
    assert user.expires_at > user.issued_at


def test_jeton_expire_est_refuse(service: JwtService, settings: Settings) -> None:
    token, _ = service.issue_access_token(sub="demo", roles=["analyst"], ttl_seconds=-30)
    with pytest.raises(TokenError, match="expire"):
        service.decode(token)


def test_signature_invalide_est_refusee(settings: Settings, service: JwtService, tmp_path: Path) -> None:
    """Un jeton signe par une autre cle doit etre refuse."""
    other_keys = tmp_path / "other"
    generate_keypair(other_keys / "private.pem", other_keys / "public.pem")
    foreign = JwtService(
        Settings(
            keys_dir=other_keys,
            revocation_path=tmp_path / "revoked2.json",
            policies_path=tmp_path / "policies.yaml",
            audit_log_path=tmp_path / "audit.jsonl",
            issuer=settings.issuer,
            audience=settings.audience,
        ),
        RevocationStore(tmp_path / "revoked2.json"),
    )
    token, _ = foreign.issue_access_token(sub="intrus", roles=["analyst"])

    with pytest.raises(TokenError):
        service.decode(token)


def test_audience_etrangere_est_refusee(settings: Settings, service: JwtService) -> None:
    token, _ = service.issue_access_token(sub="demo", roles=["analyst"])
    # On re-signe le meme contenu avec une autre audience : sans la verification
    # de l'audience, un jeton emis pour un autre service serait accepte ici.
    payload = pyjwt.decode(token, options={"verify_signature": False})
    payload["aud"] = "autre-service"
    forged = pyjwt.encode(payload, settings.private_key_path.read_bytes(), algorithm="RS256")

    with pytest.raises(TokenError, match="audience"):
        service.decode(forged)


def test_claim_obligatoire_manquant_est_refuse(settings: Settings, service: JwtService) -> None:
    payload = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": "demo",
        "iat": int(time.time()),
        "nbf": int(time.time()),
        "exp": int(time.time()) + 600,
        # 'jti' volontairement absent
    }
    forged = pyjwt.encode(payload, settings.private_key_path.read_bytes(), algorithm="RS256")

    with pytest.raises(TokenError):
        service.decode(forged)


def test_jeton_revoque_est_refuse(service: JwtService) -> None:
    token, jti = service.issue_access_token(sub="demo", roles=["analyst"])
    assert service.decode(token).jti == jti

    claims = pyjwt.decode(token, options={"verify_signature": False})
    service.revoke(jti, claims["exp"])

    with pytest.raises(TokenError, match="revoque"):
        service.decode(token)


def test_confusion_d_algorithme_est_refusee(settings: Settings, service: JwtService) -> None:
    """Attaque classique : signer en HS256 avec la cle publique comme secret.

    Elle ne fonctionne que si l'application accepte l'algorithme annonce dans
    l'en-tete du jeton. On impose une liste blanche (RS256) : le test verifie
    que cette protection est bien en place.

    Note : on fabrique le jeton a la main plutot qu'avec PyJWT, qui refuse
    desormais de signer en HMAC avec une cle PEM. C'est aussi plus fidele : un
    attaquant n'utilise pas la bibliotheque de sa victime.
    """
    forged = _forge_hs256(
        secret=settings.public_key_path.read_bytes(),
        claims={
            "iss": settings.issuer,
            "aud": settings.audience,
            "sub": "intrus",
            "roles": ["analyst"],
            "jti": "forge",
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "exp": int(time.time()) + 600,
        },
    )

    with pytest.raises(TokenError):
        service.decode(forged)


def test_algorithme_none_est_refuse(settings: Settings, service: JwtService) -> None:
    """Attaque historique : alg=none, jeton non signe."""
    header = _base64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _base64url(
        json.dumps(
            {
                "iss": settings.issuer,
                "aud": settings.audience,
                "sub": "intrus",
                "roles": ["analyst"],
                "jti": "none",
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 600,
            }
        ).encode()
    )

    with pytest.raises(TokenError):
        service.decode(f"{header}.{payload}.")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _forge_hs256(*, secret: bytes, claims: dict) -> str:
    """Fabrique un JWT signe en HS256 avec un secret arbitraire."""
    header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _base64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_base64url(signature)}"


def test_jwks_expose_la_cle_publique(service: JwtService) -> None:
    jwks = service.jwks()
    key = jwks["keys"][0]

    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["n"] and key["e"]
    assert "d" not in key  # jamais de cle privee dans le JWKS


def test_keypair_existant_n_est_pas_ecrase(tmp_path: Path) -> None:
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    assert ensure_keypair(private, public) is True
    signature = private.read_bytes()
    assert ensure_keypair(private, public) is False
    assert private.read_bytes() == signature
