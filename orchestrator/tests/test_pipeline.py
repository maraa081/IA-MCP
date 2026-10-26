"""Tests du pipeline complet, avec des doublures (fakes) a la place du reseau.

Methode : on garde tout le code reel (GPO, registre, pipeline, journalisation)
et on remplace uniquement les deux frontieres reseau - la gateway LiteLLM et les
serveurs MCP. On teste donc vraiment la logique d'orchestration, sans dependre
d'un modele ni d'un serveur.

Ce que l'on cherche a prouver :
  1. le modele ne recoit QUE les outils autorises par la GPO ;
  2. un appel propose hors droits est refuse a l'execution (deuxieme verrou) ;
  3. un appel hors quota de taille est refuse ;
  4. sans outil autorise, rien n'est execute mais la demande reste tracee ;
  5. le journal d'audit contient bien la decision complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.app.audit import AuditLogger
from orchestrator.app.config import Settings
from orchestrator.app.container import Services
from orchestrator.app.llm import GatewayReply, Synthesizer, ToolCallRequest, ToolPlanner
from orchestrator.app.llm.planner import RESPOND_DIRECTLY_TOOL
from orchestrator.app.mcp import ToolCallResult, ToolRegistry
from orchestrator.app.pipeline import ChatPipeline, PolicyViolation, _sanitize_arguments
from orchestrator.app.policy import PolicyEngine, RateLimiter
from orchestrator.app.security import AuthenticatedUser

POLICY = """
version: 3
roles:
  analyst:
    tools:
      summarize_text:
        max_input_chars: 5000
        max_calls_per_request: 2
      code_to_technical_doc:
        max_input_chars: 5000
        max_calls_per_request: 1
        allowed_frameworks: [arc42]
  reader:
    tools:
      summarize_text:
        max_input_chars: 50
        max_calls_per_request: 1
limits:
  max_prompt_chars: 20000
  max_tool_calls_per_request: 3
"""

# --- Doublures ---------------------------------------------------------------


class FakeGateway:
    """Remplace la gateway LiteLLM : renvoie des reponses preparees, dans l'ordre."""

    def __init__(self, replies: list[GatewayReply] | None = None) -> None:
        self.replies = replies or []
        self.received_tools: list[list[dict[str, Any]]] = []
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, *, model: str, messages, tools=None, **kwargs) -> GatewayReply:
        self.received_tools.append(tools or [])
        self.messages.append(messages)
        if not self.replies:
            return GatewayReply(model=model, content="reponse par defaut")
        return self.replies.pop(0)

    async def aclose(self) -> None:
        return None


class FakeMcpClient:
    """Remplace les serveurs MCP : catalogue fixe et appels enregistres."""

    def __init__(
        self,
        tools_by_server: dict[str, list[dict[str, Any]]],
        error_tools: dict[str, str] | None = None,
    ) -> None:
        self._tools = tools_by_server
        # Outils qui echouent : nom de l'outil -> message d'erreur.
        self._error_tools = error_tools or {}
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self, server: str, url: str) -> list[dict[str, Any]]:
        return self._tools.get(server, [])

    async def call_tool(self, *, server: str, url: str, tool: str, arguments: dict) -> ToolCallResult:
        self.calls.append({"server": server, "tool": tool, "arguments": arguments})

        if tool in self._error_tools:
            message = self._error_tools[tool]
            return ToolCallResult(
                tool=tool,
                server=server,
                ok=False,
                payload={"error": message},
                text="",
                error=f"l'outil '{tool}' a refuse les arguments : {message}",
                duration_ms=5,
            )

        return ToolCallResult(
            tool=tool,
            server=server,
            ok=True,
            payload={"tool": tool, "summary": "resume produit par l'outil", "model": "writer"},
            text="resume produit par l'outil",
            duration_ms=12,
        )


def test_les_arguments_none_sont_retires() -> None:
    """Le modele rend parfois "None" au lieu d'omettre un argument optionnel."""
    schema = {
        "properties": {
            "text": {"type": "string"},
            "max_sentences": {"type": "integer"},
        }
    }

    assert _sanitize_arguments(
        {"text": "x", "max_sentences": "None"}, schema
    ) == {"text": "x"}


def test_les_entiers_textuels_sont_convertis() -> None:
    schema = {
        "properties": {
            "max_sentences": {"type": "integer"},
            "limite": {"type": "integer"},
        }
    }

    assert _sanitize_arguments(
        {"max_sentences": "5", "limite": "10"}, schema
    ) == {"max_sentences": 5, "limite": 10}


def tool_schema(name: str, description: str = "outil de test") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }


CAMELOGUE = {
    "summarizer": [tool_schema("summarize_text", "resume un texte")],
    "codedoc": [tool_schema("code_to_technical_doc", "documente du code")],
}


# --- Montage -----------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    policy_path = tmp_path / "policies.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    return Settings(
        planner_mode="auto",
        policies_path=policy_path,
        audit_log_path=tmp_path / "audit.jsonl",
        revocation_path=tmp_path / "revoked.json",
        keys_dir=tmp_path / "keys",
        synthesis_enabled=True,
    )


async def build_pipeline(
    settings: Settings,
    gateway: FakeGateway,
    mcp: FakeMcpClient,
) -> ChatPipeline:
    registry = ToolRegistry(settings, mcp)                 # type: ignore[arg-type]
    await registry.refresh()
    policy = PolicyEngine(settings.policies_path, RateLimiter())
    return ChatPipeline(
        settings=settings,
        policy=policy,
        registry=registry,
        mcp_client=mcp,                                     # type: ignore[arg-type]
        gateway=gateway,                                    # type: ignore[arg-type]
        planner=ToolPlanner(gateway, settings),             # type: ignore[arg-type]
        synthesizer=Synthesizer(gateway, settings),         # type: ignore[arg-type]
        audit=AuditLogger(settings.audit_log_path),
    )


def user(sub: str, roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        sub=sub, roles=roles, scopes=[], jti=f"jti-{sub}", issued_at=0, expires_at=9_999_999_999
    )


def planning_reply(tool: str, arguments: dict[str, Any]) -> GatewayReply:
    return GatewayReply(
        model="planner",
        content="",
        tool_calls=[ToolCallRequest(name=tool, arguments=arguments, call_id="c1")],
    )


# --- Tests -------------------------------------------------------------------


async def test_parcours_nominal_appelle_l_outil_puis_synthetise(settings: Settings) -> None:
    gateway = FakeGateway(
        replies=[
            planning_reply("summarize_text", {"text": "un long document"}),
            GatewayReply(model="writer", content="Voici le resume demande."),
        ]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Resume ce document")

    assert outcome.tools_used == ["summarize_text"]
    assert outcome.answer == "Voici le resume demande."
    assert outcome.denials == []
    assert mcp.calls[0]["server"] == "summarizer"


async def test_le_modele_ne_voit_que_les_outils_autorises(settings: Settings) -> None:
    gateway = FakeGateway(replies=[planning_reply("summarize_text", {"text": "document"})])
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    await pipeline.run(user=user("lecteur", ["reader"]), message="Resume ce document")

    transmitted = gateway.received_tools[0]
    names = [schema["function"]["name"] for schema in transmitted]
    # Les outils MCP autorises, plus l'echappatoire de reponse directe. Cette
    # derniere n'est pas un outil MCP : c'est un signal de decision ajoute par le
    # planificateur. L'outil interdit 'code_to_technical_doc' reste masque.
    assert names == ["summarize_text", RESPOND_DIRECTLY_TOOL]


async def test_appel_hors_droits_est_refuse_a_l_execution(settings: Settings) -> None:
    """Deuxieme verrou : meme si le modele propose un outil interdit, on refuse."""
    gateway = FakeGateway(
        replies=[
            planning_reply("code_to_technical_doc", {"code": "print(1)", "framework": "arc42"}),
            # Second tour : le modele ne conclut pas (contenu vide), ce qui doit
            # faire retomber le pipeline sur l'explication de l'echec.
            GatewayReply(model="planner", content=""),
        ]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(
        user=user("lecteur", ["reader"]), message="Documente ce code"
    )

    assert outcome.tools_used == []
    assert mcp.calls == []                        # rien n'a ete execute
    assert len(outcome.denials) == 1
    assert "non accorde" in outcome.denials[0]
    assert outcome.tool_trace[0].decision == "denied"


async def test_entree_trop_longue_refusee_a_l_execution(settings: Settings) -> None:
    gateway = FakeGateway(
        replies=[planning_reply("summarize_text", {"text": "a" * 500})]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    # 'reader' : 50 caracteres maximum pour summarize_text.
    outcome = await pipeline.run(user=user("lecteur", ["reader"]), message="Resume ce document")

    assert mcp.calls == []
    assert "trop long" in outcome.denials[0]


async def test_framework_hors_liste_blanche_refuse(settings: Settings) -> None:
    gateway = FakeGateway(
        replies=[planning_reply("code_to_technical_doc", {"code": "print(1)", "framework": "c4"})]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Documente ce code")

    assert mcp.calls == []
    assert "non autorise" in outcome.denials[0]


async def test_reponse_directe_sans_outil(settings: Settings) -> None:
    gateway = FakeGateway(replies=[GatewayReply(model="planner", content="Bonjour, comment puis-je aider ?")])
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Bonjour")

    assert outcome.tools_used == []
    assert outcome.answer == "Bonjour, comment puis-je aider ?"
    assert mcp.calls == []


async def test_message_trop_long_refuse_avant_le_modele(settings: Settings) -> None:
    gateway = FakeGateway()
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    with pytest.raises(PolicyViolation):
        await pipeline.run(user=user("demo", ["analyst"]), message="a" * 20_001)

    assert gateway.received_tools == []           # le modele n'a meme pas ete appele


async def test_quota_d_appels_par_requete(settings: Settings) -> None:
    """Le modele propose trois fois le meme outil ; le quota n'en laisse passer qu'un."""
    gateway = FakeGateway(
        replies=[
            GatewayReply(
                model="planner",
                content="",
                tool_calls=[
                    ToolCallRequest(name="summarize_text", arguments={"text": "a"}),
                    ToolCallRequest(name="summarize_text", arguments={"text": "b"}),
                    ToolCallRequest(name="summarize_text", arguments={"text": "c"}),
                ],
            )
        ]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    # 'reader' : max_calls_per_request = 1
    outcome = await pipeline.run(user=user("lecteur", ["reader"]), message="Resume ce document")

    assert len(mcp.calls) == 1
    assert len(outcome.denials) == 2
    assert all("quota" in denial for denial in outcome.denials)


async def test_journal_d_audit_complet(settings: Settings) -> None:
    gateway = FakeGateway(
        replies=[
            planning_reply("summarize_text", {"text": "document"}),
            GatewayReply(model="writer", content="Resume final."),
        ]
    )
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Resume ce document")

    lines = settings.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    event = json.loads(lines[-1])

    assert event["event"] == "chat.completed"
    assert event["sub"] == "demo"
    assert event["tools_executed"] == ["summarize_text"]
    assert event["trace_id"] == outcome.trace_id
    assert event["allowed"] is True
    # La trace ne contient pas le document, seulement son empreinte.
    assert "prompt_fingerprint" in event
    assert "document" not in json.dumps(event)


async def test_sans_synthese_le_resultat_de_l_outil_est_rendu(settings: Settings) -> None:
    settings.synthesis_enabled = False
    gateway = FakeGateway(replies=[planning_reply("summarize_text", {"text": "document"})])
    mcp = FakeMcpClient(CAMELOGUE)
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Resume ce document")

    assert outcome.answer == "resume produit par l'outil"
    assert len(gateway.received_tools) == 1        # une seule requete au modele


async def test_echec_d_outil_n_est_pas_un_succes(settings: Settings) -> None:
    """Cas reel : l'outil refuse ses arguments. L'appel doit etre trace en erreur."""
    gateway = FakeGateway(
        replies=[
            planning_reply("code_to_technical_doc", {"code": "print(1)", "framework": "arc42"}),
            # Second tour : le modele ne conclut pas (contenu vide), ce qui doit
            # faire retomber le pipeline sur l'explication de l'echec.
            GatewayReply(model="planner", content=""),
        ]
    )
    mcp = FakeMcpClient(
        CAMELOGUE,
        error_tools={
            "code_to_technical_doc": "'audience' doit etre l'un de : developpeur, exploitant"
        },
    )
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Documente ce code")

    assert outcome.tools_used == []                      # un echec n'est pas un usage
    assert outcome.tool_trace[0].decision == "error"
    assert "refuse les arguments" in outcome.tool_trace[0].reason
    assert outcome.answer                                # l'utilisateur obtient une reponse

    # L'echec est trace distinctement d'un refus de droits.
    event = json.loads(settings.audit_log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert event["errors"] and not event["tools_executed"]
    assert event["denials"] == []


async def test_explication_d_echec_quand_le_modele_ne_conclut_pas(settings: Settings) -> None:
    """Sans conclusion du modele, l'utilisateur recoit une explication d'echec.

    On force le mode natif : sans repli JSON, une reponse vide du modele n'est
    pas remplacee par une seconde tentative, et le pipeline retombe donc sur
    l'explication construite a partir des erreurs.
    """
    settings.planner_mode = "native"
    gateway = FakeGateway(
        replies=[
            planning_reply("code_to_technical_doc", {"code": "print(1)", "framework": "arc42"}),
            GatewayReply(model="planner", content=""),
        ]
    )
    mcp = FakeMcpClient(
        CAMELOGUE,
        error_tools={"code_to_technical_doc": "'audience' doit etre l'un de : developpeur"},
    )
    pipeline = await build_pipeline(settings, gateway, mcp)

    outcome = await pipeline.run(user=user("demo", ["analyst"]), message="Documente ce code")

    assert "audience" in outcome.answer
    assert "Appels en erreur" in outcome.answer


async def test_services_startup_expose_les_services(settings: Settings) -> None:
    """Verifie le cablage reel du conteneur.

    Les URL des serveurs MCP sont volontairement pointees vers des ports fermes :
    un test ne doit pas dependre de services qui tournent sur la machine, sinon
    il passe ou echoue selon l'environnement.
    """
    settings = settings.model_copy(
        update={
            "mcp_summarizer_url": "http://127.0.0.1:19",
            "mcp_codedoc_url": "http://127.0.0.1:19",
            "mcp_codedb_url": "http://127.0.0.1:19",
        }
    )
    services = await Services.startup(settings)

    assert services.pipeline is not None
    # Un serveur injoignable ne doit pas empecher le demarrage : il est marque
    # indisponible et ses outils sortent du catalogue.
    assert services.registry.count() == 0
    assert services.registry.server_status() == {
        "summarizer": False,
        "codedoc": False,
        "codedb": False,
    }
    await services.shutdown()
