#!/usr/bin/env python3
"""Choisir le modele Ollama utilise par les alias de la gateway.

`gateway/config.yaml` definit deux alias (planner et writer) qui pointent vers le
meme modele physique. Changer de modele revient donc a reecrire ces lignes, plus
eventuellement les fenetres de contexte (num_ctx) : un 2b sur un CPU modeste ne
tient pas 16k de contexte en memoire.

Le script ne touche a rien d'autre dans le fichier : commentaires et autres cles
sont preserves, et une seconde execution avec les memes valeurs ne modifie rien.

Usages :

    python scripts/set_model.py --show
    python scripts/set_model.py --model qwen3.5:2b
    python scripts/set_model.py --model qwen3.5:4b --planner-ctx 8192 --writer-ctx 16384

Codes de sortie : 0 si la configuration est conforme, 1 en cas d'erreur.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "gateway" / "config.yaml"

# Un bloc commence par "  - model_name: <alias>" et va jusqu'au bloc suivant.
BLOC_RE = re.compile(
    r"(^  - model_name: (?P<alias>[A-Za-z0-9_.-]+)[ \t]*\n)(?P<corps>.*?)(?=^  - model_name: |\Z)",
    re.MULTILINE | re.DOTALL,
)
MODELE_RE = re.compile(r"^(?P<indent>[ \t]+)model:[ \t]*(?P<valeur>\S+)[ \t]*$", re.MULTILINE)
CTX_RE = re.compile(r"^(?P<indent>[ \t]+)num_ctx:[ \t]*(?P<valeur>\d+)[ \t]*$", re.MULTILINE)


def lire_etat(texte: str) -> dict[str, dict[str, str]]:
    """Retourne {alias: {model, num_ctx}} tel que lu dans le fichier."""
    etat: dict[str, dict[str, str]] = {}
    for bloc in BLOC_RE.finditer(texte):
        corps = bloc.group("corps")
        modele = MODELE_RE.search(corps)
        ctx = CTX_RE.search(corps)
        etat[bloc.group("alias")] = {
            "model": modele.group("valeur") if modele else "?",
            "num_ctx": ctx.group("valeur") if ctx else "?",
        }
    return etat


def appliquer(corps: str, modele: str, num_ctx: int | None) -> tuple[str, bool]:
    """Reecrit model (et num_ctx si demande) dans un bloc. Retourne (texte, change)."""
    change = False

    def _modele(m: re.Match[str]) -> str:
        nonlocal change
        nouveau = f"{m.group('indent')}model: ollama_chat/{modele}"
        if m.group("valeur") != f"ollama_chat/{modele}":
            change = True
        return nouveau

    corps = MODELE_RE.sub(_modele, corps, count=1)

    if num_ctx is not None:
        def _ctx(m: re.Match[str]) -> str:
            nonlocal change
            if m.group("valeur") != str(num_ctx):
                change = True
            return f"{m.group('indent')}num_ctx: {num_ctx}"

        corps = CTX_RE.sub(_ctx, corps, count=1)

    return corps, change


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Choisit le modele Ollama des alias planner et writer",
    )
    parser.add_argument("--model", help="nom exact du modele Ollama (ex : qwen3.5:2b)")
    parser.add_argument("--planner-ctx", type=int, help="num_ctx de l'alias planner")
    parser.add_argument("--writer-ctx", type=int, help="num_ctx de l'alias writer")
    parser.add_argument("--show", action="store_true", help="affiche l'etat courant et sort")
    parser.add_argument("--config", default=str(CONFIG), help="chemin du config.yaml (tests)")
    arguments = parser.parse_args()

    chemin = Path(arguments.config)
    if not chemin.is_file():
        print(f"config introuvable : {chemin}", file=sys.stderr)
        return 1

    texte = chemin.read_text(encoding="utf-8")
    etat = lire_etat(texte)
    if not etat:
        print("aucun alias trouve dans le fichier (format inattendu)", file=sys.stderr)
        return 1

    if arguments.show or not arguments.model:
        for alias, valeurs in etat.items():
            print(f"  {alias:<8} {valeurs['model']:<28} num_ctx={valeurs['num_ctx']}")
        return 0

    contextes = {"planner": arguments.planner_ctx, "writer": arguments.writer_ctx}
    modifie = False

    def _bloc(m: re.Match[str]) -> str:
        nonlocal modifie
        alias = m.group("alias")
        corps, change = appliquer(m.group("corps"), arguments.model, contextes.get(alias))
        modifie = modifie or change
        return m.group(1) + corps

    nouveau = BLOC_RE.sub(_bloc, texte)
    if modifie:
        chemin.write_text(nouveau, encoding="utf-8")

    print(f"  modele : ollama_chat/{arguments.model}")
    for alias, valeurs in lire_etat(nouveau).items():
        print(f"  {alias:<8} num_ctx={valeurs['num_ctx']}")
    print(f"  fichier {chemin} {'mis a jour' if modifie else 'deja conforme'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
