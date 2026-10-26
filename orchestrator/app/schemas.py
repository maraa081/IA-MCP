"""Modeles d'entree et de sortie de l'API HTTP.

Ils servent de contrat : c'est ce que voit un client, et c'est ce que la
documentation OpenAPI (/docs) expose automatiquement.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Authentification --------------------------------------------------------

class TokenRequest(BaseModel):
    """Demande de jeton (mode demonstration uniquement)."""

    username: str = Field(..., examples=["demo"])
    password: str = Field(..., examples=["changeme-lab"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    roles: list[str]


class UserInfo(BaseModel):
    """Identite resolue depuis le jeton, et droits effectifs."""

    sub: str
    roles: list[str]
    tools: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Outils autorises et leurs contraintes"
    )
    limits: dict[str, Any] = Field(default_factory=dict)
    token_expires_at: int


# --- Dialogue ----------------------------------------------------------------

class ChatRequest(BaseModel):
    """Message de l'utilisateur."""

    message: str = Field(..., min_length=1, max_length=200_000)
    model: str | None = Field(
        default=None, description="Alias de modele impose ; sinon choix par defaut"
    )
    synthesize: bool | None = Field(
        default=None,
        description="Force ou desactive la synthese finale par le modele redacteur",
    )


class ToolCallTrace(BaseModel):
    """Trace d'un appel d'outil, autorise ou refuse."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision: Literal["allowed", "denied", "error"]
    reason: str | None = None
    rule: str | None = None
    duration_ms: int | None = None
    output_preview: str | None = None


class ChatResponse(BaseModel):
    trace_id: str
    answer: str
    model_used: str
    tools_used: list[str] = Field(default_factory=list)
    tool_trace: list[ToolCallTrace] = Field(default_factory=list)
    planner_mode: str
    policy_denials: list[str] = Field(default_factory=list)
    latency_ms: int


# --- Meta / introspection ----------------------------------------------------

class ToolInfo(BaseModel):
    name: str
    server: str
    description: str
    input_schema: dict[str, Any]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    gateway: bool
    mcp_servers: dict[str, bool]
    tools_known: int
