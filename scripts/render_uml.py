#!/usr/bin/env python3
"""Rend les fichiers .puml de docs/uml en PNG via le serveur PlantUML.

Pourquoi le serveur : PlantUML est un outil Java ; la machine du labo n'a pas de
JVM. Le serveur public fait le rendu, et le PNG est versionne dans le depot.

Encodage : PlantUML attend la source compressee (zlib/deflate) puis encodee dans
son propre alphabet base64. C'est la meme chose que le bouton "Copy URL" de
l'editeur PlantUML.
"""

from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs" / "uml"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"


def encoder(source: str) -> str:
    donnees = zlib.compress(source.encode("utf-8"))[2:-4]
    resultat = []
    for i in range(0, len(donnees), 3):
        bloc = donnees[i : i + 3]
        octets = [0, 0, 0]
        for j, valeur in enumerate(bloc):
            octets[j] = valeur
        resultat.append(ALPHABET[(octets[0] >> 2) & 0x3F])
        resultat.append(ALPHABET[((octets[0] & 0x3) << 4) | ((octets[1] >> 4) & 0xF)])
        if len(bloc) > 1:
            resultat.append(ALPHABET[((octets[1] & 0xF) << 2) | ((octets[2] >> 6) & 0x3)])
        if len(bloc) > 2:
            resultat.append(ALPHABET[octets[2] & 0x3F])
    return "".join(resultat)


def main() -> None:
    for source in sorted(DOCS.glob("*.puml")):
        cible = source.with_suffix(".png")
        url = f"https://www.plantuml.com/plantuml/png/{encoder(source.read_text(encoding='utf-8'))}"
        resultat = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "--max-time", "90",
             "-o", str(cible), "-w", "%{http_code}", url],
            capture_output=True, text=True,
        )
        taille = cible.stat().st_size if cible.exists() else 0
        entete = cible.read_bytes()[:8] if taille else b""
        ok = resultat.stdout == "200" and entete.startswith(b"\x89PNG")
        print(f"{source.name:38s} -> {cible.name:28s} {'OK' if ok else 'FAIL'} "
              f"(http={resultat.stdout}, {taille} octets)")
        if not ok and taille:
            print("   reponse:", cible.read_text(errors="replace")[:300])


if __name__ == "__main__":
    main()
