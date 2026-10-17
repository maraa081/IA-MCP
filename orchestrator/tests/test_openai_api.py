"""Tests de la passerelle OpenAI (le chemin utilise par Open WebUI).

On teste le routeur isole : l'application complete demarre les serveurs MCP,
alors qu'ici on ne veut verifier que la traduction OpenAI <-> pipeline.
Le pipeline est remplace par une doublure qui enregistre ses arguments : c'est
ainsi qu'on prouve que le modele choisi dans Open WebUI restreint bien les
outils transmis, sans jamais elargir les droits.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.app.pipeline import PipelineOutcome, PolicyViolation
from orchestrator.app.routers import openai
from orchestrator.app.security import AuthenticatedUser
from orchestrator.app.security.dependencies import get_current_user, get_services


class FakePipeline:
    """Doublure du pipeline : enregistre les arguments, rend une reponse fixe."""

    def __init__(self, answer: str = "reponse de test", violation: PolicyViolation | None = None):
        self.answer = answer
        self.violation = violation
        self.appels: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> PipelineOutcome:
        self.appels.append(kwargs)
        if self.violation is not None:
            raise self.violation
        return PipelineOutcome(
            trace_id="trace-test",
            answer=self.answer,
            model_used="planner",
            tools_used=[],
            latency_ms=1,
        )


class FakeServices:
    def __init__(self, pipeline: FakePipeline) -> None:
        self.pipeline = pipeline


@pytest.fixture()
def utilisateur() -> AuthenticatedUser:
    return AuthenticatedUser(
        sub="openwebui", roles=["analyst"], scopes=[], jti="jti-test",
        issued_at=0, expires_at=9_999_999_999,
    )


def client(pipeline: FakePipeline, utilisateur: AuthenticatedUser) -> TestClient:
    app = FastAPI()
    app.include_router(openai.router)
    app.dependency_overrides[get_current_user] = lambda: utilisateur
    app.dependency_overrides[get_services] = lambda: FakeServices(pipeline)
    return TestClient(app)


def corps(message: str = "Resume ce texte", modele: str = "Assistant") -> dict[str, Any]:
    return {"model": modele, "messages": [{"role": "user", "content": message}], "stream": False}


# --- Catalogue ---------------------------------------------------------------


def test_la_liste_des_modeles_est_celle_du_catalogue(utilisateur: AuthenticatedUser) -> None:
    reponse = client(FakePipeline(), utilisateur).get("/v1/models")

    assert reponse.status_code == 200
    identifiants = [m["id"] for m in reponse.json()["data"]]
    assert identifiants == list(openai.MODELES)


def test_un_modele_inconnu_ne_donne_aucun_outil() -> None:
    nom, outils = openai._modele("modele-oublie")
    assert nom == "modele-oublie"
    assert outils == set()


def test_le_modele_par_defaut_autorise_tous_les_outils() -> None:
    nom, outils = openai._modele(None)
    assert nom == openai.MODELE_PAR_DEFAUT
    assert outils is None


# --- Dialogue ----------------------------------------------------------------


def test_la_reponse_non_stream_est_au_format_openai(utilisateur: AuthenticatedUser) -> None:
    pipeline = FakePipeline(answer="Voici le resume.")
    reponse = client(pipeline, utilisateur).post("/v1/chat/completions", json=corps())

    assert reponse.status_code == 200
    corps_reponse = reponse.json()
    assert corps_reponse["object"] == "chat.completion"
    assert corps_reponse["choices"][0]["message"]["content"] == "Voici le resume."
    assert corps_reponse["choices"][0]["finish_reason"] == "stop"


def test_le_modele_choisi_restreint_les_outils(utilisateur: AuthenticatedUser) -> None:
    pipeline = FakePipeline()
    client(pipeline, utilisateur).post(
        "/v1/chat/completions", json=corps(modele="Assistant resume")
    )

    assert pipeline.appels[0]["allowed_tools"] == {"summarize_text"}
    # Sans restriction (modele "Assistant"), le jeu est None : la GPO decide.
    client(pipeline, utilisateur).post("/v1/chat/completions", json=corps(modele="Assistant"))
    assert pipeline.appels[1]["allowed_tools"] is None


def test_l_historique_est_transmis_au_pipeline(utilisateur: AuthenticatedUser) -> None:
    pipeline = FakePipeline()
    client(pipeline, utilisateur).post(
        "/v1/chat/completions",
        json={
            "model": "Assistant",
            "messages": [
                {"role": "system", "content": "ignore-moi"},
                {"role": "user", "content": "resume ce fichier"},
                {"role": "assistant", "content": "voici le resume"},
                {"role": "user", "content": "et celui d'avant ?"},
            ],
        },
    )

    appel = pipeline.appels[0]
    assert appel["message"] == "et celui d'avant ?"
    assert appel["history"] == [
        {"role": "user", "content": "resume ce fichier"},
        {"role": "assistant", "content": "voici le resume"},
    ]


def test_le_flux_sse_se_termine_par_done(utilisateur: AuthenticatedUser) -> None:
    reponse = client(FakePipeline(answer="abc"), utilisateur).post(
        "/v1/chat/completions", json={**corps(), "stream": True}
    )

    assert reponse.status_code == 200
    assert "text/event-stream" in reponse.headers["content-type"]
    lignes = [l for l in reponse.text.splitlines() if l.startswith("data: ")]
    assert lignes[-1] == "data: [DONE]"
    contenu = "".join(
        json.loads(l[6:])["choices"][0]["delta"].get("content", "")
        for l in lignes
        if l.startswith("data: {")
    )
    assert contenu == "abc"


def test_un_refus_de_politique_devient_une_erreur_openai(utilisateur: AuthenticatedUser) -> None:
    pipeline = FakePipeline(
        violation=PolicyViolation("message trop long", "limits.max_prompt_chars")
    )
    reponse = client(pipeline, utilisateur).post("/v1/chat/completions", json=corps())

    assert reponse.status_code == 403
    erreur = reponse.json()["error"]
    assert erreur["type"] == "policy_violation"
    assert "limits.max_prompt_chars" in erreur["message"]


def test_un_message_vide_est_refuse(utilisateur: AuthenticatedUser) -> None:
    reponse = client(FakePipeline(), utilisateur).post("/v1/chat/completions", json=corps("   "))

    assert reponse.status_code == 400
    assert reponse.json()["error"]["type"] == "invalid_request_error"
