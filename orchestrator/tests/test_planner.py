"""Tests du planificateur : choix des outils et echappatoire de reponse directe.

Cas couvert ici, decouvert en execution : avec un seul outil disponible et une
demande qui correspond exactement a cet outil, le modele redigeait la reponse
lui-meme sans rien appeler. Durcir le prompt n'a pas suffi. La correction est
architecturale : rendre le choix explicite en ajoutant un outil virtuel
"respond_directly" et en imposant un choix (tool_choice = required).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.app.config import Settings
from orchestrator.app.llm import GatewayReply, ToolCallRequest, ToolPlanner
from orchestrator.app.llm.planner import RESPOND_DIRECTLY_TOOL
from orchestrator.app.mcp import ToolDescriptor


class RecordingGateway:
    """Doublure de la gateway : enregistre les arguments recus."""

    def __init__(self, reply: GatewayReply) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> GatewayReply:
        self.calls.append(kwargs)
        return self.reply


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        policies_path=tmp_path / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
    )


def descriptor(name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        server="summarizer",
        description="Resume un texte",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


async def test_l_echappatoire_est_transmise_au_modele(settings: Settings) -> None:
    """Le modele doit voir trois choix : resumer, documenter... ou repondre."""
    gateway = RecordingGateway(
        GatewayReply(model="planner", content="", tool_calls=[
            ToolCallRequest(name="summarize_text", arguments={"text": "x"})
        ])
    )
    planner = ToolPlanner(gateway, settings)          # type: ignore[arg-type]

    await planner.plan(messages=[{"role": "user", "content": "resume ceci"}], tools=[descriptor("summarize_text")])

    schemas = gateway.calls[0]["tools"]
    noms = [schema["function"]["name"] for schema in schemas]
    assert noms == ["summarize_text", RESPOND_DIRECTLY_TOOL]
    # Et le choix est impose : ne rien appeler n'est plus une option implicite.
    assert gateway.calls[0]["tool_choice"] == "required"


async def test_le_choix_est_impose_par_defaut(settings: Settings) -> None:
    assert settings.planner_tool_choice == "required"


async def test_appel_d_outil_reel_est_transmis(settings: Settings) -> None:
    gateway = RecordingGateway(
        GatewayReply(model="planner", content="", tool_calls=[
            ToolCallRequest(name="summarize_text", arguments={"text": "document"})
        ])
    )
    planner = ToolPlanner(gateway, settings)          # type: ignore[arg-type]

    plan = await planner.plan(messages=[{"role": "user", "content": "resume"}], tools=[descriptor("summarize_text")])

    assert len(plan.calls) == 1
    assert plan.calls[0].tool == "summarize_text"
    assert plan.calls[0].arguments == {"text": "document"}
    assert plan.content == ""


async def test_reponse_directe_via_l_echappatoire(settings: Settings) -> None:
    """Le modele choisit 'respond_directly' : aucune action, juste une reponse."""
    gateway = RecordingGateway(
        GatewayReply(model="planner", content="", tool_calls=[
            ToolCallRequest(
                name=RESPOND_DIRECTLY_TOOL,
                arguments={"answer": "Bonjour, comment puis-je aider ?"},
            )
        ])
    )
    planner = ToolPlanner(gateway, settings)          # type: ignore[arg-type]

    plan = await planner.plan(messages=[{"role": "user", "content": "bonjour"}], tools=[descriptor("summarize_text")])

    assert plan.calls == []                            # aucune action declenchee
    assert plan.content == "Bonjour, comment puis-je aider ?"


async def test_l_echappatoire_n_est_jamais_executee_comme_outil(settings: Settings) -> None:
    gateway = RecordingGateway(
        GatewayReply(model="planner", content="", tool_calls=[
            ToolCallRequest(name=RESPOND_DIRECTLY_TOOL, arguments={"answer": "ok"}),
            ToolCallRequest(name="summarize_text", arguments={"text": "x"}),
        ])
    )
    planner = ToolPlanner(gateway, settings)          # type: ignore[arg-type]

    plan = await planner.plan(messages=[{"role": "user", "content": "mixte"}], tools=[descriptor("summarize_text")])

    noms = [call.tool for call in plan.calls]
    assert RESPOND_DIRECTLY_TOOL not in noms
    assert noms == ["summarize_text"]
    assert plan.content == "ok"


async def test_aucun_outil_autorise_repond_sans_planification(settings: Settings) -> None:
    """Sans outil, le modele est appele en conversation simple.

    Le prompt de planification impose un appel d'outil : il n'a plus de sens ici.
    On attend donc une reponse directe, sans schema d'outil transmis.
    """
    gateway = RecordingGateway(GatewayReply(model="planner", content="bonjour"))
    planner = ToolPlanner(gateway, settings)          # type: ignore[arg-type]

    plan = await planner.plan(messages=[{"role": "user", "content": "resume"}], tools=[])

    assert plan.mode == "direct"
    assert plan.calls == []
    assert plan.content == "bonjour"
    assert len(gateway.calls) == 1
    assert "tools" not in gateway.calls[0]
