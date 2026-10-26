"""Tests des points d'entree servis par l'orchestrateur.

Depuis que Open WebUI est la seule interface, l'orchestrateur ne sert plus de
page HTML : il n'expose que son API. Ces tests verifient ce contrat (l'ancienne
page /ui a bien disparu) et que l'application reste utilisable sans serveur MCP.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.app.config import PROJECT_ROOT, Settings
from orchestrator.app.main import create_app


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Reglages isoles, sans dependance a un service qui tourne sur la machine."""
    return Settings(
        planner_mode="auto",
        policies_path=PROJECT_ROOT / "config" / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
        orchestrator_host="127.0.0.1",
        # Ports fermes : le demarrage doit rester possible sans serveur MCP.
        mcp_summarizer_url="http://127.0.0.1:19",
        mcp_codedoc_url="http://127.0.0.1:19",
        mcp_codedb_url="http://127.0.0.1:19",
    )


def test_l_ancienne_page_de_prompt_a_disparu(settings: Settings) -> None:
    """La page /ui a ete retiree : le navigateur passe uniquement par Open WebUI."""
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/ui")

    assert reponse.status_code == 404


def test_la_documentation_interactive_reste_disponible(settings: Settings) -> None:
    """La documentation OpenAPI reste utile pour explorer l'API en developpement."""
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/docs")

    assert reponse.status_code == 200


def test_les_modeles_exigent_un_jeton(settings: Settings) -> None:
    """Le chemin OpenAI n'est pas anonyme : sans jeton, pas de catalogue."""
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/v1/models")

    assert reponse.status_code == 401


def test_le_point_de_sante_signale_l_absence_de_serveurs_mcp(settings: Settings) -> None:
    """Sans serveur MCP joignable, l'etat est degrade mais l'API repond."""
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/health")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "degraded"
    assert corps["mcp_servers"] == {"summarizer": False, "codedoc": False, "codedb": False}
    assert corps["tools_known"] == 0
