#!/usr/bin/env python3
"""Emettre un jeton JWT a la main, sans passer par l'API.

Utile pour :
  - tester l'orchestrateur avant d'avoir un vrai fournisseur d'identite ;
  - fabriquer un jeton avec un role different (verifier la GPO) ;
  - fabriquer un jeton volontairement expire (verifier la verification).

Usages :
  python scripts/mint_token.py --sub demo --roles analyst
  python scripts/mint_token.py --sub lecteur --roles reader --ttl 60
  python scripts/mint_token.py --sub test --ttl -10     # jeton deja expire

Le jeton est ecrit sur la sortie standard, ce qui permet :
  TOKEN=$(python scripts/mint_token.py --roles analyst)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.app.config import get_settings                # noqa: E402
from orchestrator.app.security import JwtService, RevocationStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Emission d'un jeton JWT de test")
    parser.add_argument("--sub", default="demo", help="identifiant du porteur")
    parser.add_argument(
        "--roles",
        default="analyst",
        help="roles separes par des virgules (ex : analyst,reader)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="duree de vie en secondes (negatif = jeton deja expire)",
    )
    parser.add_argument("--quiet", action="store_true", help="n'afficher que le jeton")
    args = parser.parse_args()

    settings = get_settings()
    service = JwtService(settings, RevocationStore(settings.revocation_path))

    roles = [role.strip() for role in args.roles.split(",") if role.strip()]
    token, jti = service.issue_access_token(sub=args.sub, roles=roles, ttl_seconds=args.ttl)

    if not args.quiet:
        print(f"# sub={args.sub} roles={roles} jti={jti}", file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
