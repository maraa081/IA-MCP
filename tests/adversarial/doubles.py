"""Doublures reseau et fabriques communes aux tests adversariaux.

Le principe est le meme que pour les tests du pipeline : on garde tout le code
reel (GPO, registre, pipeline, audit) et on remplace uniquement les deux
frontieres reseau, la gateway et les serveurs MCP. Un test adversarial ne doit
pas dependre d'un modele ni d'un service qui tourne sur la machine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mcp_servers.codedb.database import CodeDatabase
from orchestrator.app.audit import AuditLogger
from orchestrator.app.config import Settings
from orchestrator.app.llm import (
    GatewayReply,
    LayaUnavailable,
    Synthesizer,
    ToolCallRequest,
    ToolPlanner,
)
from orchestrator.app.mcp import ToolCallResult, ToolRegistry
from orchestrator.app.pipeline import ChatPipeline
from orchestrator.app.policy import PolicyEngine, RateLimiter
from orchestrator.app.security import AuthenticatedUser
from scripts.index_code import extraire_description

STYLES = ["neutral", "bullets", "executive", "technical"]
FRAMEWORKS = ["diataxis", "arc42", "c4", "madr", "sphinx"]
AUDIENCES = ["developpeur", "exploitant", "auditeur", "architecte"]


# -----------------------------------------------------------------------------
# Catalogue d'outils (schemas simplifiees, mais realistes)
# -----------------------------------------------------------------------------

def _outil(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return {"name": name, "description": description, "input_schema": schema}


CAMELOGUE: dict[str, list[dict[str, Any]]] = {
    "summarizer": [
        _outil(
            "summarize_text",
            "Resume un texte",
            {
                "text": {"type": "string"},
                "max_sentences": {"type": "integer"},
                "style": {"type": "string", "enum": STYLES},
            },
            ["text"],
        )
    ],
    "codedoc": [
        _outil(
            "code_to_technical_doc",
            "Documente du code colle",
            {
                "code": {"type": "string"},
                "framework": {"type": "string", "enum": FRAMEWORKS},
                "language": {"type": "string"},
                "audience": {"type": "string", "enum": AUDIENCES},
            },
            ["code"],
        )
    ],
    "codedb": [
        _outil("code_disponible", "Inventaire de l'index", {}, []),
        _outil(
            "chercher_code",
            "Cherche dans le code indexe",
            {
                "requete": {"type": "string"},
                "langage": {"type": "string"},
                "limite": {"type": "integer"},
            },
            ["requete"],
        ),
        _outil(
            "resumer_code",
            "Resume un fichier indexe",
            {
                "reference": {"type": "string"},
                "max_sentences": {"type": "integer"},
                "style": {"type": "string", "enum": ["fonctionnel", "technique", "revue"]},
            },
            ["reference"],
        ),
        _outil(
            "documenter_code",
            "Documente un fichier indexe",
            {
                "reference": {"type": "string"},
                "framework": {"type": "string", "enum": FRAMEWORKS},
                "audience": {"type": "string", "enum": AUDIENCES},
            },
            ["reference"],
        ),
    ],
}


# -----------------------------------------------------------------------------
# Doublures
# -----------------------------------------------------------------------------

class FakeGateway:
    """Remplace la gateway LiteLLM : reponses preparees, dans l'ordre."""

    def __init__(self, replies: list[GatewayReply] | None = None) -> None:
        self.replies = list(replies or [])
        self.received_tools: list[list[dict[str, Any]]] = []
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> GatewayReply:
        self.received_tools.append(tools or [])
        self.messages.append(messages)
        if not self.replies:
            return GatewayReply(model=model, content="reponse par defaut")
        return self.replies.pop(0)

    async def aclose(self) -> None:
        return None


class FakeMcpClient:
    """Remplace les serveurs MCP : catalogue fixe, resultats configurables."""

    def __init__(
        self,
        tools_by_server: dict[str, list[dict[str, Any]]] | None = None,
        payload_by_tool: dict[str, dict[str, Any]] | None = None,
        error_tools: dict[str, str] | None = None,
    ) -> None:
        self._tools = tools_by_server or CAMELOGUE
        # Resultat renvoye par outil. Le pipeline le reinjecte au tour suivant.
        self.payload_by_tool = dict(payload_by_tool or {})
        # Outils qui echouent : nom -> message d'erreur.
        self.error_tools = dict(error_tools or {})
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self, server: str, url: str) -> list[dict[str, Any]]:
        return self._tools.get(server, [])

    async def call_tool(
        self, *, server: str, url: str, tool: str, arguments: dict[str, Any]
    ) -> ToolCallResult:
        self.calls.append(
            {"server": server, "tool": tool, "arguments": dict(arguments)}
        )

        if tool in self.error_tools:
            message = self.error_tools[tool]
            return ToolCallResult(
                tool=tool,
                server=server,
                ok=False,
                payload={"error": message},
                text="",
                error=f"l'outil '{tool}' a refuse les arguments : {message}",
                duration_ms=5,
            )

        payload = self.payload_by_tool.get(
            tool,
            {
                "tool": tool,
                "summary": "resume produit par l'outil",
                "model": "writer",
            },
        )
        return ToolCallResult(
            tool=tool,
            server=server,
            ok=True,
            payload=payload,
            text=json.dumps(payload, ensure_ascii=False),
            duration_ms=5,
        )


class _NoLaya:
    """Laya n'est pas utilisee par ces tests."""

    async def choose(self, **_: Any) -> Any:
        raise LayaUnavailable("Laya n'est pas utilisée par les tests adversariaux")

    async def aclose(self) -> None:
        return None


# -----------------------------------------------------------------------------
# Fabriques
# -----------------------------------------------------------------------------

async def build_pipeline(
    settings: Settings, gateway: FakeGateway, mcp: FakeMcpClient
) -> ChatPipeline:
    """Assemble le pipeline reel avec les deux frontieres reseau remplacees."""
    registry = ToolRegistry(settings, mcp)  # type: ignore[arg-type]
    await registry.refresh()
    policy = PolicyEngine(settings.policies_path, RateLimiter())
    return ChatPipeline(
        settings=settings,
        policy=policy,
        registry=registry,
        mcp_client=mcp,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        planner=ToolPlanner(gateway, settings, laya=_NoLaya()),  # type: ignore[arg-type]
        synthesizer=Synthesizer(gateway, settings),  # type: ignore[arg-type]
        audit=AuditLogger(settings.audit_log_path),
    )


def utilisateur(sub: str, roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        sub=sub,
        roles=roles,
        scopes=[],
        jti=f"jti-{sub}",
        issued_at=0,
        expires_at=9_999_999_999,
    )


def planning_reply(tool: str, arguments: dict[str, Any]) -> GatewayReply:
    return GatewayReply(
        model="planner",
        content="",
        tool_calls=[ToolCallRequest(name=tool, arguments=arguments, call_id="c1")],
    )


def dernier_evenement(settings: Settings) -> dict[str, Any]:
    """Dernier evenement du journal d'audit, ou erreur explicite."""
    lignes = (
        settings.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    )
    if not lignes:
        raise AssertionError("aucun evenement d'audit ecrit")
    return json.loads(lignes[-1])


def resultat_recherche_piege(
    tmp_path: Path, description: str
) -> dict[str, Any]:
    """Construit un vrai resultat de recherche codedb sur un fichier piege.

    Le fichier est reellement indexe dans une base SQLite temporaire : on teste
    la vraie extraction de description et la vraie forme du resultat, pas une
    chaine inventee.
    """
    base = CodeDatabase(Path(tmp_path) / "codedb.sqlite")
    base.initialiser()
    contenu = f'"""{description}"""\nprint(1)\n'
    base.indexer(
        chemin="piege.py",
        racine="/projet",
        contenu=contenu,
        langage="python",
        description=extraire_description(contenu, "python"),
        empreinte=hashlib.sha256(contenu.encode("utf-8")).hexdigest(),
        horodatage="2026-10-26T00:00:00+00:00",
    )
    entrees = base.chercher(requete="piege")
    return {
        "tool": "chercher_code",
        "requete": "piege",
        "langage": None,
        "resultats": [entree.as_dict() for entree in entrees],
        "nombre": len(entrees),
        "note": "",
    }


def settings_limites(
    tmp_path: Path,
    *,
    max_prompt_chars: int = 1000,
    max_input_chars: int = 1000,
    max_calls: int = 1,
) -> Settings:
    """Settings avec une politique minimale, pour tester les plafonds."""
    politique = Path(tmp_path) / "policies.yaml"
    politique.write_text(
        f"""version: 1
roles:
  reader:
    tools:
      summarize_text:
        max_input_chars: {max_input_chars}
        max_calls_per_request: {max_calls}
      chercher_code:
        max_calls_per_request: {max_calls}
limits:
  max_prompt_chars: {max_prompt_chars}
  max_tool_calls_per_request: 4
""",
        encoding="utf-8",
    )
    return Settings(
        planner_mode="auto",
        policies_path=politique,
        audit_log_path=Path(tmp_path) / "audit.jsonl",
        revocation_path=Path(tmp_path) / "revoked.json",
        keys_dir=Path(tmp_path) / "keys",
    )
