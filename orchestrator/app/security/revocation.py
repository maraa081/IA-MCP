"""Liste de revocation des jetons.

Un JWT est valide jusqu'a son expiration : il est sans etat par construction.
C'est un avantage (pas de requete base a chaque appel) et une limite (impossible
de deconnecter vraiment quelqu'un). On ajoute donc une liste de revocation,
indexee par 'jti'.

Mise en oeuvre volontairement simple pour le labo : un fichier JSON recharge en
memoire. En production : Redis ou la base de la passerelle d'API, avec un TTL
egal a la duree de vie du jeton (au-dela, le jeton est expire de toute facon :
inutile de garder la ligne).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class RevocationStore:
    """Liste de revocation persistee dans un fichier JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._revoked: dict[str, int] = {}     # jti -> horodatage d'expiration
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._revoked = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Un fichier corrompu ne doit pas empecher le service de demarrer :
            # on repart d'une liste vide (et on le signale dans les journaux).
            self._revoked = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._revoked, indent=2), encoding="utf-8"
        )

    def revoke(self, jti: str, expires_at: int) -> None:
        with self._lock:
            self._revoked[jti] = int(expires_at)
            self.purge_expired()
            self._save()

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return jti in self._revoked

    def purge_expired(self, now: int | None = None) -> int:
        """Retire les entrées dont le jeton est de toute facon expire."""
        current = int(now if now is not None else time.time())
        expired = [jti for jti, exp in self._revoked.items() if exp <= current]
        for jti in expired:
            self._revoked.pop(jti, None)
        return len(expired)

    def size(self) -> int:
        with self._lock:
            return len(self._revoked)
