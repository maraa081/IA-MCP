"""Fixtures et rapport lisible des tests adversariaux.

Le rapport est ecrit dans `var/adversarial/rapport.md` apres chaque execution de
`pytest tests/adversarial`. Il repond a la question qui compte en revue :
« pour chaque scenario, est-ce bloque, ou est-ce un risque residuel assume ? »

Un test `xfail(strict=True)` est un risque residuel : il doit echouer pour la
raison documentee. Un XPASS strict est un echec, pas un succes silencieux.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.app.config import Settings
from tests.adversarial.doubles import FakeGateway, FakeMcpClient
from tests.adversarial.scenarios import SCENARIOS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAPPORT = PROJECT_ROOT / "var" / "adversarial" / "rapport.md"

_RESULTATS: dict[str, dict[str, Any]] = {}
_A_DES_TESTS = False


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Reglages isoles, avec la vraie politique du depot."""
    return Settings(
        planner_mode="auto",
        policies_path=PROJECT_ROOT / "config" / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
    )


@pytest.fixture()
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture()
def mcp() -> FakeMcpClient:
    return FakeMcpClient()


# -----------------------------------------------------------------------------
# Collecte des resultats
# -----------------------------------------------------------------------------

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    global _A_DES_TESTS
    _A_DES_TESTS = any("tests/adversarial" in item.nodeid for item in items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "tests/adversarial" not in report.nodeid:
        return
    if report.when != "call" and not (report.when == "setup" and report.skipped):
        return
    nom = report.nodeid.split("::")[-1]
    _RESULTATS[nom] = {
        "outcome": report.outcome,
        "wasxfail": bool(getattr(report, "wasxfail", None)),
    }


def _statut(nom: str) -> str:
    resultat = _RESULTATS.get(nom)
    if resultat is None:
        return "non exécuté"
    if resultat["outcome"] == "failed":
        # Un XPASS strict arrive ici : il ne doit pas etre presente comme un
        # risque residuel « normal ».
        return "échec"
    if resultat["wasxfail"]:
        return "résiduel (xfail attendu)"
    if resultat["outcome"] == "passed":
        return "bloqué"
    if resultat["outcome"] == "skipped":
        return "non exécuté (skip)"
    return resultat["outcome"]


def _ecrire_rapport() -> None:
    RAPPORT.parent.mkdir(parents=True, exist_ok=True)

    lignes = [
        "# Rapport des tests adversariaux",
        "",
        f"Date : {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "Commande : `pytest tests/adversarial`",
        "",
        "| Scénario | Test | Attendu | Résultat |",
        "| --- | --- | --- | --- |",
    ]

    for scenario in SCENARIOS:
        statut = _statut(scenario["test"])
        lignes.append(
            f"| {scenario['scenario']}. {scenario['titre']} "
            f"| `{scenario['test']}` | {scenario['attendu']} | {statut} |"
        )

    compteurs = {
        "bloqué": sum(1 for s in SCENARIOS if _statut(s["test"]) == "bloqué"),
        "résiduel": sum(
            1 for s in SCENARIOS if _statut(s["test"]).startswith("résiduel")
        ),
        "échec": sum(1 for s in SCENARIOS if _statut(s["test"]) == "échec"),
        "non exécuté": sum(
            1 for s in SCENARIOS if _statut(s["test"]).startswith("non exécuté")
        ),
    }

    lignes.extend(
        [
            "",
            "## Synthèse",
            "",
            f"- Bloqués : {compteurs['bloqué']}",
            f"- Résiduels connus (xfail strict) : {compteurs['résiduel']}",
            f"- Échecs : {compteurs['échec']}",
            f"- Non exécutés : {compteurs['non exécuté']}",
            "",
            "Un test `xfail(strict=True)` documente un risque résiduel : il doit",
            "échouer pour la raison connue. Un XPASS strict est un échec, pas un",
            "succès silencieux.",
            "",
            "Les risques résiduels et les mitigations proposées sont détaillés dans",
            "`docs/menaces.md`.",
        ]
    )

    RAPPORT.write_text("\n".join(lignes) + "\n", encoding="utf-8")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _A_DES_TESTS:
        return
    try:
        _ecrire_rapport()
    except OSError as exc:  # disque plein, droits... on ne casse pas la session
        print(f"Impossible d'écrire le rapport adversarial : {exc}")


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    if not _A_DES_TESTS:
        return
    terminalreporter.write_sep("=", "Rapport adversarial")
    if RAPPORT.exists():
        terminalreporter.write_line(f"Rapport : {RAPPORT}")
    for scenario in SCENARIOS:
        terminalreporter.write_line(
            f"  {scenario['scenario']}. {scenario['titre']} : "
            f"{_statut(scenario['test'])}"
        )
