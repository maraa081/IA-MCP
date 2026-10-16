"""Limitation de debit (rate limiting) en memoire.

Objectif : empecher qu'un utilisateur (ou un script avec un jeton vole) sature
les serveurs MCP et le moteur d'inference. C'est une protection de disponibilite
autant qu'une protection de cout.

Fenetre glissante : on garde les horodatages des appels recents par cle. Une cle
est generalement (utilisateur, outil). En production, la limitation se place
plutot dans la passerelle d'API (une seule instance, etanche a la mise a
l'echelle) ; ici, en memoire, cela suffit pour comprendre le mecanisme.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Fenetre glissante, par cle, en memoire."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int | None, window_seconds: int | None) -> bool:
        """Retourne True si l'appel est autorise, et l'enregistre.

        limit a None : aucune limite configuree, l'appel passe toujours.
        """
        if not limit or not window_seconds:
            return True

        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()

            if len(hits) >= limit:
                return False

            hits.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Vide le compteur d'une cle (tests) ou de toutes les cles."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def remaining(self, key: str, limit: int | None, window_seconds: int | None) -> int | None:
        """Nombre d'appels restants (diagnostic, en-tetes HTTP)."""
        if not limit or not window_seconds:
            return None
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key, deque())
            cutoff = now - window_seconds
            used = sum(1 for stamp in hits if stamp >= cutoff)
        return max(0, limit - used)
