"""Tests du harnais d'evaluation, en mode simule uniquement.

Aucun de ces tests ne contacte la gateway ni un serveur MCP : le catalogue est
construit en processus et les reponses du planificateur viennent de doublures.
C'est la meme regle que pour le reste du depot : un test qui depend d'un service
qui tourne sur la machine ne teste rien de reproductible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.compare import compare_results
from evals.run import (
    PROJECT_ROOT,
    SimulatedGateway,
    build_catalog,
    evaluate,
    load_dataset,
    parse_categories,
    select_cases,
    validate_dataset,
)
from orchestrator.app.config import Settings
from orchestrator.app.llm import GatewayReply, ToolCallRequest
from orchestrator.app.mcp import ToolDescriptor

DATASET_PATH = Path(__file__).resolve().parents[1] / "dataset.yaml"


# -----------------------------------------------------------------------------
# Catalogue de test : les memes schemas que la production, sans import MCP.
# -----------------------------------------------------------------------------

def _descriptor(
    name: str, server: str, schema: dict[str, Any], description: str = ""
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        server=server,
        description=description or f"outil de test {name}",
        input_schema=schema,
    )


FRAMEWORKS = ["arc42", "c4", "diataxis", "madr", "sphinx"]
AUDIENCES = ["architecte", "auditeur", "developpeur", "exploitant"]
STYLES = ["bullets", "executive", "neutral", "technical"]

CATALOG = sorted(
    [
        _descriptor(
            "summarize_text",
            "summarizer",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "max_sentences": {"type": "integer"},
                    "style": {"type": "string", "enum": STYLES},
                },
                "required": ["text"],
            },
        ),
        _descriptor(
            "code_to_technical_doc",
            "codedoc",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "framework": {"type": "string", "enum": FRAMEWORKS},
                    "language": {"type": "string"},
                    "audience": {"type": "string", "enum": AUDIENCES},
                },
                "required": ["code"],
            },
        ),
        _descriptor(
            "code_disponible",
            "codedb",
            {"type": "object", "properties": {}},
        ),
        _descriptor(
            "chercher_code",
            "codedb",
            {
                "type": "object",
                "properties": {
                    "requete": {"type": "string"},
                    "langage": {"type": "string"},
                    "limite": {"type": "integer"},
                },
                "required": ["requete"],
            },
        ),
        _descriptor(
            "resumer_code",
            "codedb",
            {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "max_sentences": {"type": "integer"},
                    "style": {
                        "type": "string",
                        "enum": ["fonctionnel", "technique", "revue"],
                    },
                },
                "required": ["reference"],
            },
        ),
        _descriptor(
            "documenter_code",
            "codedb",
            {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "framework": {"type": "string", "enum": FRAMEWORKS},
                    "audience": {"type": "string", "enum": AUDIENCES},
                },
                "required": ["reference"],
            },
        ),
    ],
    key=lambda tool: tool.name,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        planner_mode="native",
        planner_model="planner",
        policies_path=PROJECT_ROOT / "config" / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
    )


def _cases(*identifiants: str) -> list[dict[str, Any]]:
    dataset = load_dataset(DATASET_PATH)
    retenus = [case for case in dataset["cases"] if case["id"] in identifiants]
    assert len(retenus) == len(identifiants), "identifiant de test introuvable"
    return retenus


# -----------------------------------------------------------------------------
# Jeu de cas
# -----------------------------------------------------------------------------

def test_le_dataset_est_valide_et_couvre_la_repartition_imposee() -> None:
    dataset = load_dataset(DATASET_PATH)
    validate_dataset(dataset, CATALOG)

    cases = dataset["cases"]
    assert len(cases) >= 40
    assert len({case["id"] for case in cases}) == len(cases)

    categories = {case["categorie"] for case in cases}
    attendues = {
        "resume_texte",
        "codedb_recherche",
        "codedb_resume",
        "codedb_documentation",
        "codedoc_code_colle",
        "salutations",
        "hors_perimetre",
        "ambigu_deux_etapes",
        "reader_interdit",
    }
    assert attendues.issubset(categories)

    frameworks = {
        case["attendu"]["arguments"].get("framework")
        for case in cases
        if case["categorie"] == "codedoc_code_colle"
    }
    assert {"diataxis", "arc42", "c4", "madr", "sphinx"} == frameworks

    interdits = [case for case in cases if case["categorie"] == "reader_interdit"]
    assert interdits
    assert all(case["role"] == "reader" for case in interdits)
    assert all(case["attendu"].get("outils_interdits") for case in interdits)

    deux_etapes = [case for case in cases if case["categorie"] == "ambigu_deux_etapes"]
    assert deux_etapes
    assert all(len(case["attendu"].get("etapes") or []) == 2 for case in deux_etapes)

    deterministes = [case for case in cases if case.get("deterministe")]
    assert len(deterministes) == 3
    assert all(case["attendu"]["outil"] == "code_disponible" for case in deterministes)


def test_un_cas_refus_gpo_sans_outil_interdit_est_refuse() -> None:
    dataset = {
        "version": 1,
        "cases": [
            {
                "id": "invalide",
                "prompt": "Documente ce fichier.",
                "role": "reader",
                "categorie": "reader_interdit",
                "attendu": {"outil": "refus_gpo", "arguments": {}},
                "commentaire": "cas volontairement invalide",
            }
        ],
    }
    with pytest.raises(ValueError, match="outils_interdits"):
        validate_dataset(dataset, CATALOG)


def test_le_filtre_par_categorie_et_la_limite() -> None:
    dataset = load_dataset(DATASET_PATH)
    cases = select_cases(dataset, ["resume_texte"], 3)
    assert len(cases) == 3
    assert all(case["categorie"] == "resume_texte" for case in cases)

    assert parse_categories(["a,b", "c"]) == ["a", "b", "c"]
    assert parse_categories(None) == []


async def test_le_catalogue_reel_expose_les_six_outils() -> None:
    catalog = await build_catalog()
    noms = {tool.name for tool in catalog}
    assert noms == {
        "summarize_text",
        "code_to_technical_doc",
        "code_disponible",
        "chercher_code",
        "resumer_code",
        "documenter_code",
    }

    codedoc = next(tool for tool in catalog if tool.name == "code_to_technical_doc")
    assert sorted(codedoc.input_schema["properties"]["framework"]["enum"]) == FRAMEWORKS

    summarizer = next(tool for tool in catalog if tool.name == "summarize_text")
    assert sorted(summarizer.input_schema["properties"]["style"]["enum"]) == STYLES


# -----------------------------------------------------------------------------
# Mode simule : le harnais tourne sans reseau
# -----------------------------------------------------------------------------

async def test_le_mode_simule_s_execute_sans_reseau(settings: Settings) -> None:
    # Port ferme : si le harnais tentait un appel, le test echouerait.
    settings = settings.model_copy(update={"gateway_url": "http://127.0.0.1:19"})
    cases = _cases("resume-01", "salutation-01", "reader-interdit-01", "inventaire-01")

    resultat = await evaluate(
        cases,
        CATALOG,
        settings,
        mode="simule",
        repetitions=1,
        timeout=5,
        probe=False,
    )

    global_ = resultat["metriques"]["global"]
    assert global_["cas"] == 4
    assert global_["executions"] == 4
    assert global_["erreurs"] == 0
    assert global_["exactitude_outil"] == 1.0
    assert global_["exactitude_arguments"] == 1.0
    assert global_["cas_deterministes"] == 1
    assert global_["exactitude_outil_hors_deterministes"] == 1.0
    assert global_["refus_gpo"]["total"] == 1
    assert global_["refus_gpo"]["explicite"] == 1
    assert global_["refus_gpo"]["critique"] == 0
    assert global_["outil_interdit_propose"] == 0.0


class WrongGateway(SimulatedGateway):
    """Propose systematiquement un outil autorise mais different de l'attendu."""

    async def chat(self, *, model: str, messages, tools=None, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        return GatewayReply(
            model=model,
            content="",
            tool_calls=[
                ToolCallRequest(
                    name="resumer_code",
                    arguments={"reference": "orchestrator/app/audit.py"},
                    call_id="wrong",
                )
            ],
        )


async def test_un_mauvais_choix_est_compte_comme_un_echec(settings: Settings) -> None:
    resultat = await evaluate(
        _cases("resume-01"),
        CATALOG,
        settings,
        mode="simule",
        repetitions=1,
        timeout=5,
        probe=False,
        simulated_gateway_factory=lambda case: WrongGateway(case),
    )

    global_ = resultat["metriques"]["global"]
    assert global_["exactitude_outil"] == 0.0
    assert global_["reponse_directe_a_tort"] == 0.0
    # L'outil choisi est autorise : ce n'est pas un outil interdit propose.
    assert global_["outil_interdit_propose"] == 0.0
    # Les arguments ne sont pas evalues quand le mauvais outil est choisi.
    assert global_["exactitude_arguments"] is None


class ForbiddenGateway(SimulatedGateway):
    """Propose un outil interdit au role reader."""

    async def chat(self, *, model: str, messages, tools=None, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        return GatewayReply(
            model=model,
            content="",
            tool_calls=[
                ToolCallRequest(
                    name="documenter_code",
                    arguments={
                        "reference": "orchestrator/app/policy/engine.py",
                        "framework": "arc42",
                    },
                    call_id="interdit",
                )
            ],
        )


async def test_un_outil_interdit_propose_est_refuse_par_la_gpo(settings: Settings) -> None:
    resultat = await evaluate(
        _cases("reader-interdit-01"),
        CATALOG,
        settings,
        mode="simule",
        repetitions=1,
        timeout=5,
        probe=False,
        simulated_gateway_factory=lambda case: ForbiddenGateway(case),
    )

    global_ = resultat["metriques"]["global"]
    assert global_["outil_interdit_propose"] == 1.0
    assert global_["refus_gpo"]["par_gpo"] == 1
    assert global_["refus_gpo"]["critique"] == 0
    # Le refus par la GPO est un resultat sur : aucun outil n'a ete execute.
    assert global_["outil_appele_a_tort"] == 0.0
    assert global_["exactitude_outil"] == 1.0


class SubstitutionGateway(SimulatedGateway):
    """Propose un outil autorise au lieu de refuser la demande."""

    async def chat(self, *, model: str, messages, tools=None, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        return GatewayReply(
            model=model,
            content="",
            tool_calls=[
                ToolCallRequest(
                    name="summarize_text",
                    arguments={"text": "un texte quelconque"},
                    call_id="substitution",
                )
            ],
        )


async def test_une_substitution_n_est_pas_un_refus(settings: Settings) -> None:
    resultat = await evaluate(
        _cases("reader-interdit-01"),
        CATALOG,
        settings,
        mode="simule",
        repetitions=1,
        timeout=5,
        probe=False,
        simulated_gateway_factory=lambda case: SubstitutionGateway(case),
    )

    global_ = resultat["metriques"]["global"]
    assert global_["refus_gpo"]["substitution"] == 1
    assert global_["exactitude_outil"] == 0.0
    assert global_["outil_appele_a_tort"] == 1.0


async def test_les_repetitions_mesurent_la_stabilite(settings: Settings) -> None:
    resultat = await evaluate(
        _cases("resume-01"),
        CATALOG,
        settings,
        mode="simule",
        repetitions=3,
        timeout=5,
        probe=False,
    )

    case = resultat["cas"][0]
    assert len(case["executions"]) == 3
    assert case["stabilite"] == 1.0
    assert case["stabilite_outil"] == 1.0
    assert case["stabilite_arguments"] == 1.0
    assert resultat["metriques"]["global"]["stabilite"] == 1.0


# -----------------------------------------------------------------------------
# Comparaison
# -----------------------------------------------------------------------------

def _resultat(cases: list[dict[str, Any]], exactitude: float = 1.0) -> dict[str, Any]:
    return {
        "meta": {
            "date": "2026-01-01T00:00:00+01:00",
            "mode": "simule",
            "modele": "simule",
            "git_commit": "abc123",
        },
        "metriques": {
            "global": {
                "exactitude_outil": exactitude,
                "exactitude_arguments": exactitude,
                "arguments_valides": exactitude,
                "enum_invalide": 0.0,
                "outil_interdit_propose": 0.0,
                "reponse_directe_a_tort": 0.0,
                "outil_appele_a_tort": 0.0,
                "latence_ms": {"p50": 10, "p95": 20},
                "stabilite": 1.0,
            }
        },
        "cas": cases,
    }


def _case(identifiant: str, succes: bool, signature: str = "sig") -> dict[str, Any]:
    return {
        "id": identifiant,
        "categorie": "resume_texte",
        "executions": [
            {
                "outil": "summarize_text",
                "etapes": ["summarize_text"],
                "arguments": {"text": "x"},
                "succes_outil": succes,
                "succes_arguments": succes,
                "outil_interdit_propose": False,
                "signature": signature,
            }
        ],
    }


def test_compare_detecte_une_regression() -> None:
    avant = _resultat([_case("c1", True)], exactitude=1.0)
    apres = _resultat([_case("c1", False)], exactitude=0.0)

    comparaison = compare_results(avant, apres)

    assert len(comparaison["regressions"]) == 1
    assert comparaison["regressions"][0]["id"] == "c1"
    assert "exactitude outil en baisse" in comparaison["regressions"][0]["raisons"]
    tendances = {m["nom"]: m["tendance"] for m in comparaison["metriques"]}
    assert tendances["Exactitude outil"] == "régression"


def test_compare_detecte_une_amelioration_et_un_changement() -> None:
    avant = _resultat([_case("c1", False)], exactitude=0.0)
    apres = _resultat([_case("c1", True)], exactitude=1.0)

    comparaison = compare_results(avant, apres)
    assert len(comparaison["ameliorations"]) == 1
    assert comparaison["regressions"] == []

    # Meme succes, signature differente : changement neutre, pas une regression.
    avant_neutre = _resultat([_case("c1", True, "a")], exactitude=1.0)
    apres_neutre = _resultat([_case("c1", True, "b")], exactitude=1.0)
    neutre = compare_results(avant_neutre, apres_neutre)
    assert len(neutre["changements"]) == 1
    assert neutre["regressions"] == []
    assert neutre["ameliorations"] == []


def test_compare_gere_les_cas_absents() -> None:
    avant = _resultat([_case("ancien", True)])
    apres = _resultat([_case("nouveau", True)])

    comparaison = compare_results(avant, apres)

    assert [entree["id"] for entree in comparaison["nouveaux"]] == ["nouveau"]
    assert [entree["id"] for entree in comparaison["disparus"]] == ["ancien"]
    assert comparaison["regressions"] == []
