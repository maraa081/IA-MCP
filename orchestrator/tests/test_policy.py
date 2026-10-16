"""Tests du moteur de politiques (GPO).

Le cas le plus important est le refus : une politique qui autorise tout n'est pas
une politique. Chaque test verifie donc une maniere de dire non.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.app.policy import PolicyEngine, RateLimiter

POLICY = """
version: 7
roles:
  analyst:
    description: test
    tools:
      summarize_text:
        max_input_chars: 1000
        max_calls_per_request: 1
        rate_limit: { requests: 2, window_seconds: 60 }
      code_to_technical_doc:
        max_input_chars: 500
        max_calls_per_request: 1
        allowed_frameworks: [arc42, madr]
        rate_limit: { requests: 1, window_seconds: 60 }
  reader:
    tools:
      summarize_text:
        max_input_chars: 100
        max_calls_per_request: 1
limits:
  max_prompt_chars: 2000
  max_tool_calls_per_request: 2
"""


@pytest.fixture()
def engine(tmp_path: Path) -> PolicyEngine:
    path = tmp_path / "policies.yaml"
    path.write_text(POLICY, encoding="utf-8")
    return PolicyEngine(path, RateLimiter())


def test_role_inconnu_n_a_aucun_droit(engine: PolicyEngine) -> None:
    assert engine.grants_for(["role-inexistant"]) == {}
    decision = engine.check_tool_call(
        sub="x", roles=["role-inexistant"], tool="summarize_text", arguments={}, calls_so_far=0
    )
    assert decision.allowed is False
    assert decision.rule == "gpo.tool_not_granted"


def test_outil_non_accorde_est_refuse(engine: PolicyEngine) -> None:
    """'reader' n'a pas droit a la documentation de code."""
    decision = engine.check_tool_call(
        sub="lecteur",
        roles=["reader"],
        tool="code_to_technical_doc",
        arguments={"code": "print(1)"},
        calls_so_far=0,
    )
    assert decision.allowed is False
    assert "non accorde" in decision.reason


def test_appel_conforme_est_autorise(engine: PolicyEngine) -> None:
    decision = engine.check_tool_call(
        sub="demo",
        roles=["analyst"],
        tool="summarize_text",
        arguments={"text": "un texte court"},
        calls_so_far=0,
    )
    assert decision.allowed is True
    assert decision.rule == "gpo.allow"


def test_taille_d_entree_limitee(engine: PolicyEngine) -> None:
    decision = engine.check_tool_call(
        sub="demo",
        roles=["analyst"],
        tool="summarize_text",
        arguments={"text": "a" * 1001},
        calls_so_far=0,
    )
    assert decision.allowed is False
    assert decision.rule == "gpo.max_input_chars"


def test_framework_non_autorise_refuse(engine: PolicyEngine) -> None:
    decision = engine.check_tool_call(
        sub="demo",
        roles=["analyst"],
        tool="code_to_technical_doc",
        arguments={"code": "print(1)", "framework": "c4"},
        calls_so_far=0,
    )
    assert decision.allowed is False
    assert decision.rule == "gpo.allowed_frameworks"


def test_framework_autorise_accepte(engine: PolicyEngine) -> None:
    decision = engine.check_tool_call(
        sub="demo",
        roles=["analyst"],
        tool="code_to_technical_doc",
        arguments={"code": "print(1)", "framework": "arc42"},
        calls_so_far=0,
    )
    assert decision.allowed is True


def test_quota_par_requete(engine: PolicyEngine) -> None:
    decision = engine.check_tool_call(
        sub="demo",
        roles=["analyst"],
        tool="summarize_text",
        arguments={"text": "court"},
        calls_so_far=1,          # quota de 1 atteint
    )
    assert decision.allowed is False
    assert decision.rule == "gpo.max_calls_per_request"


def test_limite_de_debit(engine: PolicyEngine) -> None:
    """reader : 1 appel / 60 s (limite heritee de la configuration globale du role)."""
    arguments = {"text": "court"}

    first = engine.check_tool_call(
        sub="lecteur", roles=["reader"], tool="summarize_text",
        arguments=arguments, calls_so_far=0,
    )
    assert first.allowed is True

    # analyst a 2 appels / 60 s : on epuise le quota du meme utilisateur.
    engine.check_tool_call(
        sub="demo", roles=["analyst"], tool="summarize_text",
        arguments=arguments, calls_so_far=0,
    )
    engine.check_tool_call(
        sub="demo", roles=["analyst"], tool="summarize_text",
        arguments=arguments, calls_so_far=0,
    )
    third = engine.check_tool_call(
        sub="demo", roles=["analyst"], tool="summarize_text",
        arguments=arguments, calls_so_far=0,
    )

    assert third.allowed is False
    assert third.rule == "gpo.rate_limit"


def test_le_debit_est_compte_par_utilisateur(engine: PolicyEngine) -> None:
    """Les quotas de deux utilisateurs ne se melangent pas."""
    arguments = {"text": "court"}
    for _ in range(2):
        engine.check_tool_call(
            sub="demo", roles=["analyst"], tool="summarize_text",
            arguments=arguments, calls_so_far=0,
        )
    other = engine.check_tool_call(
        sub="autre", roles=["analyst"], tool="summarize_text",
        arguments=arguments, calls_so_far=0,
    )
    assert other.allowed is True


def test_fusion_des_roles_prend_la_limite_la_plus_large(engine: PolicyEngine) -> None:
    grants = engine.grants_for(["reader", "analyst"])
    assert grants["summarize_text"].max_input_chars == 1000   # max(100, 1000)


def test_union_des_frameworks_autorises(tmp_path: Path) -> None:
    policy = """
version: 1
roles:
  a:
    tools:
      code_to_technical_doc:
        allowed_frameworks: [arc42]
  b:
    tools:
      code_to_technical_doc:
        allowed_frameworks: [madr]
limits: {max_prompt_chars: 100}
"""
    path = tmp_path / "p.yaml"
    path.write_text(policy, encoding="utf-8")
    engine = PolicyEngine(path)

    grants = engine.grants_for(["a", "b"])
    assert set(grants["code_to_technical_doc"].allowed_frameworks) == {"arc42", "madr"}


def test_message_trop_long_refuse(engine: PolicyEngine) -> None:
    assert engine.check_prompt("a" * 2001).allowed is False
    assert engine.check_prompt("a" * 10).allowed is True


def test_rechargement_a_chaud(tmp_path: Path) -> None:
    path = tmp_path / "policies.yaml"
    path.write_text("version: 1\nroles: {}\nlimits: {max_prompt_chars: 10}\n", encoding="utf-8")
    engine = PolicyEngine(path)
    assert engine.version == 1
    assert engine.allowed_tool_names(["analyst"]) == []

    path.write_text(POLICY, encoding="utf-8")
    engine.reload()

    assert engine.version == 7
    assert "summarize_text" in engine.allowed_tool_names(["analyst"])
