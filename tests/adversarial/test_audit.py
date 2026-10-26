"""Scenario 9 : le journal d'audit ne contient jamais le contenu en clair.

Le journal doit permettre de reconstituer une decision (trace_id, outils,
refus, regles), pas de lire les documents traites. On verifie donc deux choses :

  - le contenu piege et le resultat d'outil n'apparaissent pas dans
    `audit.jsonl` ;
  - l'empreinte et la taille du message, elles, sont bien presentes.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.app.llm import GatewayReply
from tests.adversarial.doubles import (
    FakeGateway,
    FakeMcpClient,
    build_pipeline,
    dernier_evenement,
    planning_reply,
    utilisateur,
)


async def test_le_contenu_piege_n_est_pas_dans_l_audit(
    settings: Any,
    gateway: FakeGateway,
    mcp: FakeMcpClient,
    caplog: Any,
) -> None:
    """Le prompt, le resultat d'outil et la reponse finale restent hors audit."""
    caplog.set_level(logging.INFO)

    canary_prompt = "CANARY_PROMPT_9f3a"
    canary_tool = "CANARY_TOOL_9f3a"
    canary_answer = "CANARY_ANSWER_9f3a"

    mcp.payload_by_tool["summarize_text"] = {
        "tool": "summarize_text",
        "summary": canary_tool,
    }
    gateway.replies = [
        planning_reply("summarize_text", {"text": "un document"}),
        GatewayReply(model="planner", content=canary_answer),
    ]
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(
        user=utilisateur("demo", ["analyst"]),
        message=f"Résume ce document. {canary_prompt}",
    )

    audit = settings.audit_log_path.read_text(encoding="utf-8")

    # Aucun des trois canaris n'est ecrit en clair.
    assert canary_prompt not in audit
    assert canary_tool not in audit
    assert canary_answer not in audit

    # Les journaux applicatifs ne les contiennent pas non plus.
    assert canary_prompt not in caplog.text
    assert canary_tool not in caplog.text
    assert canary_answer not in caplog.text

    # Ce qui doit rester : une empreinte, une taille, un identifiant de trace.
    event = dernier_evenement(settings)
    assert event["prompt_fingerprint"]
    assert event["prompt_chars"] > 0
    assert event["trace_id"] == outcome.trace_id


async def test_la_reponse_finale_n_est_pas_dans_l_audit(
    settings: Any, gateway: FakeGateway, mcp: FakeMcpClient
) -> None:
    """La reponse rendue a l'utilisateur n'est pas recopiee dans l'audit."""
    canary = "CANARY_REPONSE_9f3a"
    gateway.replies = [GatewayReply(model="planner", content=canary)]
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(
        user=utilisateur("demo", ["analyst"]), message="Bonjour."
    )

    assert outcome.answer == canary
    audit = settings.audit_log_path.read_text(encoding="utf-8")
    assert canary not in audit
