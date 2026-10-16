"""Tests du client MCP et des schemas d'outils exposes au modele.

Deux defauts reels ont ete trouves ici, en executant la demonstration :

  1. les schemas d'outils ne declaraient pas les valeurs fermees (pas de champ
     'enum'), donc le modele devinait et produisait des valeurs hors liste :
     l'appel echouait ;
  2. un outil qui refusait ses arguments renvoyait une erreur applicative dans
     son contenu structure, et l'appel etait comptabilise comme un succes.

Ces tests verrouillent les deux corrections.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_servers.codedoc.frameworks import available_keys
from orchestrator.app.mcp.client import _detect_error, _normalize_result


# --- Detection des erreurs ---------------------------------------------------


def test_erreur_de_protocole_est_detectee() -> None:
    message = _detect_error(
        tool="outil", payload=None, text="argument invalide", protocol_error=True
    )
    assert message is not None
    assert "argument invalide" in message


def test_erreur_applicative_dans_le_contenu_est_detectee() -> None:
    """Cas reel : l'outil repond {'error': ...} sans marquer d'erreur de protocole."""
    message = _detect_error(
        tool="code_to_technical_doc",
        payload={"error": "'audience' doit etre l'un de : developpeur, exploitant"},
        text="",
        protocol_error=False,
    )
    assert message is not None
    assert "refuse les arguments" in message
    assert "audience" in message


def test_resultat_normal_n_est_pas_une_erreur() -> None:
    assert (
        _detect_error(
            tool="summarize_text",
            payload={"tool": "summarize_text", "summary": "resume"},
            text="",
            protocol_error=False,
        )
        is None
    )


def test_erreur_de_protocole_sans_texte_a_un_message_par_defaut() -> None:
    message = _detect_error(tool="outil", payload=None, text="", protocol_error=True)
    assert message is not None
    assert "outil" in message


# --- Normalisation des resultats ---------------------------------------------


class _Bloc:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resultat:
    def __init__(self, content: list[Any], structured: dict | None = None, is_error: bool = False):
        self.content = content
        self.structured_content = structured
        self.is_error = is_error


def test_contenu_structure_est_utilise_tel_quel() -> None:
    payload, text, blocks = _normalize_result(
        _Resultat([_Bloc("ignored")], structured={"summary": "ok"})
    )
    assert payload == {"summary": "ok"}
    assert text == "ignored"


def test_contenu_structure_encapsule_est_deballe() -> None:
    """Certains serveurs encapsulent la valeur dans {'result': {...}}."""
    payload, _, _ = _normalize_result(_Resultat([], structured={"result": {"summary": "ok"}}))
    assert payload == {"summary": "ok"}


def test_texte_json_est_converti_en_payload() -> None:
    payload, text, _ = _normalize_result(_Resultat([_Bloc('{"summary": "ok"}')]))
    assert payload == {"summary": "ok"}
    assert text == '{"summary": "ok"}'


def test_texte_libre_reste_du_texte() -> None:
    payload, text, _ = _normalize_result(_Resultat([_Bloc("resume en prose")]))
    assert payload is None
    assert text == "resume en prose"


# --- Schemas exposes au modele ----------------------------------------------


async def test_le_schema_du_framework_liste_les_valeurs_autorisees() -> None:
    """Le modele doit VOIR les valeurs possibles, pas les deviner."""
    from mcp_servers.codedoc.server import mcp

    tools = await mcp.list_tools()
    schema = next(tool for tool in tools if tool.name == "code_to_technical_doc").input_schema

    framework = schema["properties"]["framework"]
    assert "enum" in framework, "le schema doit exposer une liste de valeurs"
    assert sorted(framework["enum"]) == available_keys()

    audience = schema["properties"]["audience"]
    assert sorted(audience["enum"]) == ["architecte", "auditeur", "developpeur", "exploitant"]


async def test_le_schema_du_style_liste_les_valeurs_autorisees() -> None:
    from mcp_servers.summarizer.server import mcp as summarizer

    tools = await summarizer.list_tools()
    schema = next(tool for tool in tools if tool.name == "summarize_text").input_schema

    style = schema["properties"]["style"]
    assert "enum" in style
    assert set(style["enum"]) == {"neutral", "bullets", "executive", "technical"}
