"""Tests de l'interface servie par l'API.

Ces tests verifient que l'application demarre et sert ses points d'entree sans
qu'aucun serveur MCP ne soit joignable : c'est le cas quand on veut simplement
ouvrir la page de prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.app.config import PROJECT_ROOT, Settings
from orchestrator.app.main import create_app


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Reglages isoles, sans dependance a un service qui tourne sur la machine.

    La politique utilisee est celle livree dans le depot : c'est celle que
    l'application chargera reellement au demarrage.
    """
    return Settings(
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


def test_la_page_de_prompt_est_servie(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/ui")

    assert reponse.status_code == 200
    assert "text/html" in reponse.headers["content-type"]
    # Contenu minimal attendu : le champ de saisie et l'appel a l'API.
    assert 'id="message"' in reponse.text
    assert "/v1/chat" in reponse.text


def test_la_racine_redirige_vers_la_page(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/", follow_redirects=False)

    assert reponse.status_code in (307, 308)
    assert reponse.headers["location"] == "/ui"


def test_la_documentation_interactive_reste_disponible(settings: Settings) -> None:
    """La documentation OpenAPI est le second moyen de tester depuis un navigateur."""
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/docs")

    assert reponse.status_code == 200


def test_la_page_n_expose_aucun_secret(settings: Settings) -> None:
    """Une page servie ne doit contenir aucun identifiant."""
    with TestClient(create_app(settings)) as client:
        contenu = client.get("/ui").text

    assert settings.dev_password not in contenu
    assert "GATEWAY_MASTER_KEY" not in contenu


def test_le_point_de_sante_signale_l_absence_de_serveurs_mcp(settings: Settings) -> None:
    """Sans serveur MCP joignable, l'etat est degrade mais l'API repond.

    C'est un choix delibere : l'application reste utilisable (page de prompt,
    documentation) meme si les outils sont momentanement indisponibles.
    """
    with TestClient(create_app(settings)) as client:
        reponse = client.get("/health")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "degraded"
    assert corps["mcp_servers"] == {"summarizer": False, "codedoc": False, "codedb": False}
    assert corps["tools_known"] == 0
