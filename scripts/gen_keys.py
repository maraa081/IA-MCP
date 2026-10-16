#!/usr/bin/env python3
"""Genere la paire de cles RSA utilisee pour signer les jetons JWT.

Usage :
  python scripts/gen_keys.py            # cree les cles si absentes
  python scripts/gen_keys.py --force    # les remplace (invalide les jetons)

Les cles vont dans config/keys/ (ignore par git). La cle privee est creee avec
des droits 0600 : seul le proprietaire peut la lire.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.app.config import get_settings            # noqa: E402
from orchestrator.app.security.keys import generate_keypair  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generation des cles JWT (RS256)")
    parser.add_argument("--force", action="store_true", help="ecraser des cles existantes")
    args = parser.parse_args()

    settings = get_settings()
    private_path = settings.private_key_path
    public_path = settings.public_key_path

    if private_path.exists() and not args.force:
        print(f"Cles deja presentes : {private_path}")
        print("Relancez avec --force pour les remplacer (les jetons en cours seront invalides).")
        return 0

    generate_keypair(private_path, public_path)
    print(f"Cle privee : {private_path}")
    print(f"Cle publique : {public_path}")
    print("La cle privee ne doit jamais etre versionnee ni copiee dans une image.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
