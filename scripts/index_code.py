#!/usr/bin/env python3
"""Indexe du code source dans la base lue par le serveur MCP 'codedb'.

Usages :
  python scripts/index_code.py                          # indexe le projet lui-meme
  python scripts/index_code.py --racine /chemin/vers/un/depot
  python scripts/index_code.py --liste                  # simulation, n'ecrit rien
  python scripts/index_code.py --vider                  # repart de zero
  python scripts/index_code.py --extensions .py,.sql    # restreint les extensions

L'indexation peut etre relancee autant de fois que voulu : l'empreinte sha256 du
contenu evite de reecrire les fichiers inchanges. C'est ce qui permet de la
mettre dans une tache planifiee.

En production, la meme logique alimenterait une base PostgreSQL, alimentee par le
systeme d'integration continue a chaque fusion plutot qu'a la main.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_servers.codedb.database import CodeDatabase  # noqa: E402

# Extension -> langage. Ajouter une entree suffit pour indexer un nouveau langage.
LANGAGES = {
    ".py": "python",
    ".sh": "bash",
    ".sql": "sql",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

# Repertoires jamais indexes.
EXCLUS_PAR_DEFAUT = {
    ".git", ".venv", ".venv-gateway", "__pycache__", "node_modules",
    "var", "dist", "build", ".pytest_cache", ".mypy_cache",
}

MAX_OCTETS_PAR_DEFAUT = 200_000


def extension_vers_langage(chemin: Path) -> str | None:
    return LANGAGES.get(chemin.suffix.lower())


def extraire_description(contenu: str, langage: str) -> str:
    """Premiere ligne utile de documentation du fichier.

    Volontairement simple : un docstring de module en Python, sinon la premiere
    ligne de commentaire. Le but est de donner au modele un indice de recherche,
    pas de faire de l'analyse statique.
    """
    lignes = contenu.splitlines()

    if langage == "python":
        debut = 0
        while debut < len(lignes) and (
            not lignes[debut].strip()
            or lignes[debut].lstrip().startswith(("#!", "# -*-", "# coding"))
        ):
            debut += 1
        if debut < len(lignes):
            bloc = re.match(r'\s*("""|\'\'\')(.*?)\1', "\n".join(lignes[debut:]), re.DOTALL)
            if bloc:
                for ligne in bloc.group(2).splitlines():
                    if ligne.strip():
                        return ligne.strip()[:200]

    for ligne in lignes[:30]:
        depouillee = ligne.strip()
        # La ligne shebang n'est pas une description : c'est l'interpreteur.
        if depouillee.startswith("#!"):
            continue
        if depouillee.startswith(("#", "//", "--", "*")):
            texte = depouillee.lstrip("#/*- ").strip()
            if texte and not texte.startswith("!"):
                return texte[:200]

    for ligne in lignes[:30]:
        depouillee = ligne.strip()
        if depouillee.startswith("#!"):
            continue
        if depouillee:
            return depouillee[:200]

    return ""


def fichiers_a_indexer(racine: Path, extensions: set[str], exclusions: set[str]) -> list[Path]:
    trouves: list[Path] = []
    for chemin in sorted(racine.rglob("*")):
        if chemin.is_dir():
            continue
        if any(partie in exclusions for partie in chemin.parts):
            continue
        if chemin.suffix.lower() in extensions:
            trouves.append(chemin)
    return trouves


def main() -> int:
    parser = argparse.ArgumentParser(description="Indexation du code pour le serveur MCP codedb")
    parser.add_argument("--racine", default=str(PROJECT_ROOT), help="racine a indexer")
    parser.add_argument(
        "--base",
        default=str(PROJECT_ROOT / "var" / "codedb.sqlite"),
        help="fichier SQLite de l'index (ou variable CODEDB_PATH)",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(sorted(LANGAGES)),
        help="extensions a indexer, separees par des virgules",
    )
    parser.add_argument("--exclure", default="", help="repertoires a exclure, en plus des defauts")
    parser.add_argument("--max-octets", type=int, default=MAX_OCTETS_PAR_DEFAUT)
    parser.add_argument("--vider", action="store_true", help="vide l'index avant d'indexer")
    parser.add_argument("--liste", action="store_true", help="affiche sans ecrire")
    args = parser.parse_args()

    racine = Path(args.racine).resolve()
    if not racine.is_dir():
        print(f"Racine introuvable : {racine}", file=sys.stderr)
        return 2

    extensions = {e if e.startswith(".") else f".{e}" for e in args.extensions.split(",") if e.strip()}
    exclusions = set(EXCLUS_PAR_DEFAUT)
    exclusions.update(e.strip() for e in args.exclure.split(",") if e.strip())
    # Ne jamais indexer la base elle-meme ni les sorties de travail.
    exclusions.update({"var", ".git"})

    candidats = fichiers_a_indexer(racine, extensions, exclusions)
    print(f"Racine    : {racine}")
    print(f"Candidats : {len(candidats)} fichier(s) avec les extensions {sorted(extensions)}")

    if args.liste:
        for chemin in candidats[:50]:
            print("  " + str(chemin.relative_to(racine)))
        if len(candidats) > 50:
            print(f"  ... et {len(candidats) - 50} autre(s)")
        return 0

    database = CodeDatabase(args.base)
    database.initialiser()
    if args.vider:
        database.vider()
        print("Index vide avant reindexation.")

    horodatage = datetime.now(timezone.utc).isoformat(timespec="seconds")
    compteurs = {"nouveau": 0, "maj": 0, "inchange": 0, "ignore": 0}

    for chemin in candidats:
        try:
            contenu = chemin.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  ignore (lecture impossible) : {chemin} ({exc})")
            compteurs["ignore"] += 1
            continue

        if len(contenu) > args.max_octets:
            compteurs["ignore"] += 1
            continue

        langage = extension_vers_langage(chemin) or "inconnu"
        resultat = database.indexer(
            chemin=str(chemin.relative_to(racine)),
            racine=str(racine),
            contenu=contenu,
            langage=langage,
            description=extraire_description(contenu, langage),
            empreinte=hashlib.sha256(contenu.encode("utf-8")).hexdigest(),
            horodatage=horodatage,
        )
        compteurs[resultat] += 1

    inventaire = database.inventaire()
    print()
    print(f"Nouveaux  : {compteurs['nouveau']}")
    print(f"Mis a jour: {compteurs['maj']}")
    print(f"Inchanges : {compteurs['inchange']}")
    print(f"Ignores   : {compteurs['ignore']} (trop volumineux ou illisibles)")
    print()
    print(f"Index     : {inventaire['entrees']} entree(s) dans {args.base}")
    for ligne in inventaire["langages"]:
        print(f"  {ligne['langage']:12s} {ligne['fichiers']:4d} fichier(s)  {ligne['octets']:8d} octets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
