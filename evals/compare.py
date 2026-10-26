"""Compare deux campagnes d'evaluation du planificateur.

Usage :
  python evals/compare.py var/evals/2026-10-26-planner.json \
                          var/evals/2026-10-27-planner.json

Le premier fichier est la reference, le second la nouvelle campagne. Le script
affiche les metriques globales, puis les regressions et ameliorations cas par
cas. Il sort en code 1 si une regression est detectee (sauf --no-fail), ce qui
permet de l'utiliser dans une chaine d'integration.

Une regression est une baisse de l'exactitude d'outil ou d'arguments, ou une
hausse du taux d'outil interdit propose. Un changement de signature sans impact
sur ces metriques est affiche separement : il peut etre volontaire (nouveau
prompt, nouveau modele) et ne doit pas faire echouer une CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Metriques exprimees en taux (0..1) : l'affichage et le delta sont en points.
METRIQUES_EN_TAUX = {
    "Exactitude outil",
    "Exactitude arguments",
    "Arguments valides",
    "Enum invalide",
    "Outil interdit proposé",
    "Réponse directe à tort",
    "Outil appelé à tort",
}

# (nom affiche, chemin dans le JSON, sens favorable)
SPECIFICATIONS_METRIQUES: list[tuple[str, tuple[str, ...], str]] = [
    ("Exactitude outil", ("exactitude_outil",), "haut"),
    ("Exactitude arguments", ("exactitude_arguments",), "haut"),
    ("Arguments valides", ("arguments_valides",), "haut"),
    ("Enum invalide", ("enum_invalide",), "bas"),
    ("Outil interdit proposé", ("outil_interdit_propose",), "bas"),
    ("Réponse directe à tort", ("reponse_directe_a_tort",), "bas"),
    ("Outil appelé à tort", ("outil_appele_a_tort",), "bas"),
    ("Latence p50 (ms)", ("latence_ms", "p50"), "bas"),
    ("Latence p95 (ms)", ("latence_ms", "p95"), "bas"),
    ("Stabilité", ("stabilite",), "haut"),
]


def load_result(path: Path) -> dict[str, Any]:
    """Charge un fichier de resultats et verifie sa forme minimale."""
    if not path.exists():
        raise FileNotFoundError(f"fichier introuvable : {path}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("cas"), list):
        raise ValueError(f"format inattendu dans {path} : clé 'cas' absente")
    return data


def _mean_bool(values: list[Any]) -> float | None:
    propres = [bool(value) for value in values if value is not None]
    if not propres:
        return None
    return sum(1 for value in propres if value) / len(propres)


def _mean_optional(values: list[Any]) -> float | None:
    propres = [float(value) for value in values if value is not None]
    if not propres:
        return None
    return sum(propres) / len(propres)


def _modal(values: list[Any]) -> Any:
    propres = [value for value in values if value is not None]
    if not propres:
        return None
    return Counter(propres).most_common(1)[0][0]


def _case_summary(case: dict[str, Any]) -> dict[str, Any]:
    """Resume un cas : execution modale + moyennes sur les repetitions.

    La signature modale represente le comportement le plus frequent. Les
    moyennes disent si ce comportement est stable ou minoritaire.
    """
    executions = case.get("executions") or []
    if not executions:
        return {
            "outil": None,
            "etapes": None,
            "arguments": None,
            "refus": None,
            "signature": None,
            "succes_outil": None,
            "succes_arguments": None,
            "interdit": None,
        }

    signatures = [
        execution.get("signature")
        or json.dumps(execution, sort_keys=True, ensure_ascii=False)
        for execution in executions
    ]
    signature_modale = Counter(signatures).most_common(1)[0][0]
    modal = next(
        (
            execution
            for execution, signature in zip(executions, signatures)
            if signature == signature_modale
        ),
        executions[0],
    )

    return {
        "outil": modal.get("outil"),
        "etapes": modal.get("etapes"),
        "arguments": modal.get("arguments"),
        "refus": modal.get("refus"),
        "signature": signature_modale,
        "succes_outil": _mean_bool(
            [execution.get("succes_outil") for execution in executions]
        ),
        "succes_arguments": _mean_optional(
            [execution.get("succes_arguments") for execution in executions]
        ),
        "interdit": _mean_bool(
            [
                execution.get("outil_interdit_propose")
                for execution in executions
            ]
        ),
    }


def _get_path(data: dict[str, Any], chemin: tuple[str, ...]) -> Any:
    courant: Any = data
    for cle in chemin:
        if not isinstance(courant, dict) or cle not in courant:
            return None
        courant = courant[cle]
    return courant


def _delta(avant: Any, apres: Any) -> float | None:
    if avant is None or apres is None:
        return None
    try:
        return float(apres) - float(avant)
    except (TypeError, ValueError):
        return None


def _comparer_metriques(
    metriques_avant: dict[str, Any], metriques_apres: dict[str, Any]
) -> list[dict[str, Any]]:
    resultat = []
    for nom, chemin, sens in SPECIFICATIONS_METRIQUES:
        avant = _get_path(metriques_avant, chemin)
        apres = _get_path(metriques_apres, chemin)
        delta = _delta(avant, apres)
        tendance = None
        if delta is not None and abs(delta) > 1e-9:
            if (sens == "haut" and delta > 0) or (sens == "bas" and delta < 0):
                tendance = "amélioration"
            else:
                tendance = "régression"
        resultat.append(
            {
                "nom": nom,
                "avant": avant,
                "apres": apres,
                "delta": delta,
                "sens": sens,
                "tendance": tendance,
            }
        )
    return resultat


def compare_results(avant: dict[str, Any], apres: dict[str, Any]) -> dict[str, Any]:
    """Compare deux resultats et retourne un objet structure."""
    cas_avant = {case["id"]: case for case in avant.get("cas", [])}
    cas_apres = {case["id"]: case for case in apres.get("cas", [])}

    communs = sorted(set(cas_avant) & set(cas_apres))
    regressions: list[dict[str, Any]] = []
    ameliorations: list[dict[str, Any]] = []
    changements: list[dict[str, Any]] = []

    for identifiant in communs:
        resume_avant = _case_summary(cas_avant[identifiant])
        resume_apres = _case_summary(cas_apres[identifiant])

        delta_outil = _delta(
            resume_avant["succes_outil"], resume_apres["succes_outil"]
        )
        delta_arguments = _delta(
            resume_avant["succes_arguments"], resume_apres["succes_arguments"]
        )
        delta_interdit = _delta(resume_avant["interdit"], resume_apres["interdit"])

        raisons: list[str] = []
        if delta_interdit is not None and delta_interdit > 1e-9:
            raisons.append("outil interdit proposé en plus")
        if delta_outil is not None and delta_outil < -1e-9:
            raisons.append("exactitude outil en baisse")
        if delta_arguments is not None and delta_arguments < -1e-9:
            raisons.append("exactitude arguments en baisse")

        entree = {
            "id": identifiant,
            "categorie": cas_apres[identifiant].get("categorie")
            or cas_avant[identifiant].get("categorie"),
            "avant": resume_avant,
            "apres": resume_apres,
            "delta_outil": delta_outil,
            "delta_arguments": delta_arguments,
            "delta_interdit": delta_interdit,
            "raisons": raisons,
        }

        if raisons:
            regressions.append(entree)
            continue

        amelioration = (
            (delta_interdit is not None and delta_interdit < -1e-9)
            or (delta_outil is not None and delta_outil > 1e-9)
            or (delta_arguments is not None and delta_arguments > 1e-9)
        )
        if amelioration:
            ameliorations.append(entree)
            continue

        if resume_avant["signature"] != resume_apres["signature"]:
            changements.append(entree)

    nouveaux = [
        {
            "id": identifiant,
            "categorie": cas_apres[identifiant].get("categorie"),
            **{
                cle: valeur
                for cle, valeur in _case_summary(cas_apres[identifiant]).items()
                if cle in {"outil", "etapes", "succes_outil"}
            },
        }
        for identifiant in sorted(set(cas_apres) - set(cas_avant))
    ]
    disparus = [
        {
            "id": identifiant,
            "categorie": cas_avant[identifiant].get("categorie"),
            **{
                cle: valeur
                for cle, valeur in _case_summary(cas_avant[identifiant]).items()
                if cle in {"outil", "etapes", "succes_outil"}
            },
        }
        for identifiant in sorted(set(cas_avant) - set(cas_apres))
    ]

    metriques = _comparer_metriques(
        (avant.get("metriques") or {}).get("global") or {},
        (apres.get("metriques") or {}).get("global") or {},
    )

    return {
        "meta": {"avant": avant.get("meta"), "apres": apres.get("meta")},
        "communs": len(communs),
        "regressions": regressions,
        "ameliorations": ameliorations,
        "changements": changements,
        "nouveaux": nouveaux,
        "disparus": disparus,
        "metriques": metriques,
    }


# -----------------------------------------------------------------------------
# Affichage
# -----------------------------------------------------------------------------

def _format_valeur(nom: str, valeur: Any) -> str:
    if valeur is None:
        return "-"
    if nom in METRIQUES_EN_TAUX:
        return f"{float(valeur) * 100:.1f}%"
    if "Latence" in nom:
        return f"{float(valeur):.0f}"
    if nom == "Stabilité":
        return f"{float(valeur):.2f}"
    return f"{valeur}"


def _format_delta(nom: str, delta: float | None) -> str:
    if delta is None:
        return "-"
    if nom in METRIQUES_EN_TAUX:
        return f"{delta * 100:+.1f} pts"
    if "Latence" in nom:
        return f"{delta:+.0f} ms"
    if nom == "Stabilité":
        return f"{delta:+.2f}"
    return f"{delta:+.2f}"


def _outil_lisible(resume: dict[str, Any]) -> str:
    etapes = resume.get("etapes") or []
    if etapes:
        return " -> ".join(str(etape) for etape in etapes)
    return str(resume.get("outil") or "-")


def _pourcentage(valeur: float | None) -> str:
    return "-" if valeur is None else f"{valeur * 100:.0f}%"


def _afficher_liste(
    titre: str, entrees: list[dict[str, Any]], maximum: int, afficher_evaluation: bool
) -> None:
    print()
    if not entrees:
        print(f"{titre} : aucune.")
        return
    print(f"{titre} ({len(entrees)}) :")
    for entree in entrees[:maximum]:
        avant = entree["avant"]
        apres = entree["apres"]
        print(
            f"  - {entree['id']} [{entree.get('categorie') or '-'}] : "
            f"{_outil_lisible(avant)} -> {_outil_lisible(apres)}"
        )
        if afficher_evaluation:
            details = (
                f"      outil {_pourcentage(avant['succes_outil'])} -> "
                f"{_pourcentage(apres['succes_outil'])} ; "
                f"arguments {_pourcentage(avant['succes_arguments'])} -> "
                f"{_pourcentage(apres['succes_arguments'])}"
            )
            if entree.get("raisons"):
                details += " ; " + ", ".join(entree["raisons"])
            print(details)
    if len(entrees) > maximum:
        print(f"  ... et {len(entrees) - maximum} autre(s).")


def render_text(comparaison: dict[str, Any], maximum: int = 20) -> None:
    meta_avant = comparaison["meta"]["avant"] or {}
    meta_apres = comparaison["meta"]["apres"] or {}

    print()
    print("Comparaison de deux campagnes d'évaluation")
    print(
        f"  A (référence) : {meta_avant.get('date', '-')} | "
        f"mode {meta_avant.get('mode', '-')} | "
        f"modèle {meta_avant.get('modele', '-')} | "
        f"commit {meta_avant.get('git_commit') or '-'}"
    )
    print(
        f"  B (nouveau)   : {meta_apres.get('date', '-')} | "
        f"mode {meta_apres.get('mode', '-')} | "
        f"modèle {meta_apres.get('modele', '-')} | "
        f"commit {meta_apres.get('git_commit') or '-'}"
    )
    print(f"  Cas communs : {comparaison['communs']}")

    print()
    entetes = ["Métrique", "A", "B", "Delta", "Sens", "Tendance"]
    largeurs = [28, 9, 9, 12, 6, 14]
    print("  ".join(h.ljust(w) for h, w in zip(entetes, largeurs)))
    print("  ".join("-" * w for w in largeurs))
    for metrique in comparaison["metriques"]:
        cellules = [
            metrique["nom"][:28].ljust(largeurs[0]),
            _format_valeur(metrique["nom"], metrique["avant"]).rjust(largeurs[1]),
            _format_valeur(metrique["nom"], metrique["apres"]).rjust(largeurs[2]),
            _format_delta(metrique["nom"], metrique["delta"]).rjust(largeurs[3]),
            metrique["sens"].rjust(largeurs[4]),
            (metrique["tendance"] or "").ljust(largeurs[5]),
        ]
        print("  ".join(cellules))

    _afficher_liste(
        "Régressions", comparaison["regressions"], maximum, afficher_evaluation=True
    )
    _afficher_liste(
        "Améliorations", comparaison["ameliorations"], maximum, afficher_evaluation=True
    )
    _afficher_liste(
        "Changements sans impact sur les métriques",
        comparaison["changements"],
        maximum,
        afficher_evaluation=False,
    )

    print()
    print(f"Nouveaux cas : {len(comparaison['nouveaux'])}")
    for entree in comparaison["nouveaux"][:maximum]:
        print(f"  + {entree['id']} [{entree.get('categorie') or '-'}]")
    print(f"Cas disparus : {len(comparaison['disparus'])}")
    for entree in comparaison["disparus"][:maximum]:
        print(f"  - {entree['id']} [{entree.get('categorie') or '-'}]")

    print()
    if comparaison["regressions"]:
        print(
            "Verdict : régression(s) détectée(s). "
            "Relancer avec --no-fail pour ne pas sortir en erreur."
        )
    else:
        print("Verdict : aucune régression détectée.")
    print()


# -----------------------------------------------------------------------------
# Entree du programme
# -----------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare deux fichiers de résultats d'évaluation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("avant", type=Path, help="Fichier de référence")
    parser.add_argument("apres", type=Path, help="Fichier de la nouvelle campagne")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche la comparaison en JSON au lieu du texte",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Sort en code 0 même si des régressions sont détectées",
    )
    parser.add_argument(
        "--max-affiche",
        type=int,
        default=20,
        help="Nombre maximal d'entrées affichées par section",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        avant = load_result(args.avant)
        apres = load_result(args.apres)
        comparaison = compare_results(avant, apres)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(comparaison, indent=2, ensure_ascii=False))
    else:
        render_text(comparaison, maximum=max(1, args.max_affiche))

    if comparaison["regressions"] and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
