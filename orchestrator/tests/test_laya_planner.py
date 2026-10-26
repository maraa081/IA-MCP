"""Tests du mode Laya : decision par modele dedie, arguments par le LLM.

Ce que l'on prouve ici :
  1. Laya choisit l'outil, puis le LLM extrait les arguments avec un
     `tool_choice` force sur cet outil (il ne peut pas revenir sur la decision) ;
  2. une reponse directe (`respond_directly`) ne declenche aucun outil ;
  3. une quasi-egalite ou une indisponibilite de Laya renvoie la decision au
     planificateur qwen (mode `auto`), sans casser la requete ;
  4. le filtre de faisabilite ne montre pas `resumer_code` pour un texte colle
     sans reference de fichier (le mot "resume" est present dans les deux).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.app.config import Settings
from orchestrator.app.llm import (
    GatewayReply,
    LayaDecision,
    LayaUnavailable,
    ToolCallRequest,
    ToolPlanner,
)
from orchestrator.app.llm.planner import RESPOND_DIRECTLY_TOOL
from orchestrator.app.mcp import ToolDescriptor


class FakeLaya:
    """Doublure du sidecar Laya : rend une decision preparee et enregistre les criteres."""

    def __init__(
        self,
        decision: LayaDecision | None = None,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.error = error
        self.criteria_seen: dict[str, str] | None = None
        self.state_seen: str | None = None

    async def choose(
        self,
        *,
        state: str,
        criteria: dict[str, str],
        instructions: str,
        question_name: str = "tool",
    ) -> LayaDecision:
        self.state_seen = state
        self.criteria_seen = dict(criteria)
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


class RecordingGateway:
    """Doublure de la gateway : enregistre les arguments et rend des reponses preparees."""

    def __init__(self, replies: list[GatewayReply] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> GatewayReply:
        self.calls.append(kwargs)
        if not self.replies:
            return GatewayReply(model="planner", content="reponse par defaut")
        return self.replies.pop(0)


def descriptor(name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        server="serveur-test",
        description=f"description de {name}",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


TOOLS = [
    descriptor("summarize_text"),
    descriptor("code_to_technical_doc"),
    descriptor("code_disponible"),
    descriptor("chercher_code"),
    descriptor("resumer_code"),
    descriptor("documenter_code"),
]

LONG_TEXT = "Résume-moi ce document : " + ("Le projet orchestre des outils MCP. " * 10)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        planner_mode="laya",
        laya_min_confidence=0.0,
        laya_min_margin=0.02,
        policies_path=tmp_path / "policies.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
    )


def decision(choice: str, **probabilities: float) -> LayaDecision:
    values = probabilities or {choice: 0.8, RESPOND_DIRECTLY_TOOL: 0.1}
    return LayaDecision(
        choice=choice,
        confidence=max(values.values()),
        probabilities=values,
    )


async def test_laya_choisit_puis_le_llm_extrait_les_arguments(settings: Settings) -> None:
    laya = FakeLaya(decision("summarize_text", summarize_text=0.82, respond_directly=0.08))
    gateway = RecordingGateway(
        [
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[
                    ToolCallRequest(name="summarize_text", arguments={"text": "document"})
                ],
            )
        ]
    )
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": LONG_TEXT}], tools=TOOLS
    )

    assert plan.mode == "laya"
    assert [call.tool for call in plan.calls] == ["summarize_text"]
    assert plan.calls[0].arguments == {"text": "document"}
    # Le LLM ne recoit que l'outil choisi, et ne peut pas en changer.
    assert [schema["function"]["name"] for schema in gateway.calls[0]["tools"]] == [
        "summarize_text"
    ]
    assert gateway.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "summarize_text"},
    }
    # Le filtre de faisabilite a bien retire les outils de code : pas de
    # reference de fichier dans un texte colle.
    assert laya.criteria_seen is not None
    assert "resumer_code" not in laya.criteria_seen
    assert "documenter_code" not in laya.criteria_seen
    assert "summarize_text" in laya.criteria_seen


async def test_reponse_directe_ne_declenche_aucun_outil(settings: Settings) -> None:
    laya = FakeLaya(
        decision(RESPOND_DIRECTLY_TOOL, **{RESPOND_DIRECTLY_TOOL: 0.9, "summarize_text": 0.05})
    )
    gateway = RecordingGateway([GatewayReply(model="planner", content="Bonjour !")])
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": "Bonjour, comment vas-tu ?"}],
        tools=TOOLS,
    )

    assert plan.mode == "laya"
    assert plan.calls == []
    assert plan.content == "Bonjour !"
    # Aucun schema d'outil n'est transmis pour une reponse directe.
    assert "tools" not in gateway.calls[0]


async def test_quasi_egalite_renvoie_la_decision_a_qwen(settings: Settings) -> None:
    laya = FakeLaya(
        decision(RESPOND_DIRECTLY_TOOL, **{RESPOND_DIRECTLY_TOOL: 0.345, "code_disponible": 0.336})
    )
    gateway = RecordingGateway(
        [
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[ToolCallRequest(name="code_disponible", arguments={})],
            )
        ]
    )
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": "Peux-tu me donner une information ?"}],
        tools=TOOLS,
    )

    # Marge 0.009 < 0.02 : Laya n'a pas tranche, qwen reprend la main.
    assert plan.mode == "native"
    assert [call.tool for call in plan.calls] == ["code_disponible"]
    # Le mode natif voit tous les outils, pas seulement les options de Laya.
    assert len(gateway.calls[0]["tools"]) == len(TOOLS) + 1


async def test_laya_indisponible_retombe_sur_qwen(settings: Settings) -> None:
    laya = FakeLaya(error=LayaUnavailable("sidecar arrete"))
    gateway = RecordingGateway(
        [
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[
                    ToolCallRequest(name="summarize_text", arguments={"text": "doc"})
                ],
            )
        ]
    )
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": LONG_TEXT}], tools=TOOLS
    )

    assert plan.mode == "native"
    assert [call.tool for call in plan.calls] == ["summarize_text"]


async def test_une_reference_de_fichier_ouvre_les_outils_de_code(settings: Settings) -> None:
    laya = FakeLaya(
        decision("resumer_code", resumer_code=0.7, documenter_code=0.2)
    )
    gateway = RecordingGateway(
        [
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[
                    ToolCallRequest(
                        name="resumer_code",
                        arguments={"reference": "orchestrator/app/policy/engine.py"},
                    )
                ],
            )
        ]
    )
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": "Explique le fichier orchestrator/app/policy/engine.py"}],
        tools=TOOLS,
    )

    assert plan.mode == "laya"
    assert [call.tool for call in plan.calls] == ["resumer_code"]
    assert laya.criteria_seen is not None
    assert "resumer_code" in laya.criteria_seen
    assert "documenter_code" in laya.criteria_seen
    assert "summarize_text" not in laya.criteria_seen


async def test_question_inventaire_appelle_code_disponible_sans_modele(
    settings: Settings,
) -> None:
    """"Quels fichiers sont disponibles ?" ne doit jamais partir en reponse directe."""
    laya = FakeLaya(error=AssertionError("Laya ne doit pas etre appele"))
    gateway = RecordingGateway([])
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": "Quels fichiers sont disponibles ?"}],
        tools=TOOLS,
    )

    assert plan.mode == "direct_tool"
    assert [call.tool for call in plan.calls] == ["code_disponible"]
    assert plan.calls[0].arguments == {}
    assert gateway.calls == []
    assert laya.criteria_seen is None


async def test_choix_inconnu_retombe_sur_qwen(settings: Settings) -> None:
    laya = FakeLaya(decision("outil_inexistant", outil_inexistant=0.9, summarize_text=0.02))
    gateway = RecordingGateway(
        [
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[
                    ToolCallRequest(name="summarize_text", arguments={"text": "doc"})
                ],
            )
        ]
    )
    planner = ToolPlanner(gateway, settings, laya=laya)  # type: ignore[arg-type]

    plan = await planner.plan(
        messages=[{"role": "user", "content": LONG_TEXT}], tools=TOOLS
    )

    assert plan.mode == "native"
    assert [call.tool for call in plan.calls] == ["summarize_text"]
