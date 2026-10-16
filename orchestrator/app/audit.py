"""Journal d'audit.

Toute decision doit etre tracable a posteriori : qui a demande quoi, quels
outils ont ete proposes par le modele, lesquels ont ete autorises ou refuses et
pourquoi, combien de temps cela a pris. Sans ces traces, un incident de
securite est impossible a reconstituer - et une revue de conformite impossible
a passer.

Format : une ligne JSON par evenement (JSON Lines). Ce format s'importe
directement dans un SIEM (Splunk, Elastic, Wazuh...) sans transformation.

Le fichier ne contient jamais le contenu des documents : seulement des
metadonnees et des empreintes. Un journal d'audit n'est pas un journal de
donnees personnelles.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """Ecrit les evenements d'audit en JSON Lines."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:16]

    @staticmethod
    def fingerprint(value: str) -> str:
        """Empreinte courte d'un contenu, sans conserver le contenu lui-meme."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    async def record(self, event: dict[str, Any]) -> None:
        """Ajoute un evenement au journal (et aux journaux applicatifs)."""
        enriched = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "epoch_ms": int(time.time() * 1000),
            **event,
        }

        logger.info(
            "audit trace_id=%s user=%s event=%s allowed=%s tools=%s",
            enriched.get("trace_id"),
            enriched.get("sub"),
            enriched.get("event"),
            enriched.get("allowed"),
            enriched.get("tools"),
        )

        # L'ecriture disque ne doit pas bloquer la boucle d'evenements.
        async with self._lock:
            await asyncio.to_thread(self._append, enriched)

    def _append(self, event: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:  # disque plein, droits... on ne casse pas la requete
            logger.error("Ecriture du journal d'audit impossible : %s", exc)
