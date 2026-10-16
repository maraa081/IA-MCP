"""Construction et cycle de vie des services.

Un seul endroit qui assemble les briques (configuration, securite, politiques,
registre MCP, modele, pipeline) et un seul endroit qui les arrete proprement.

Interet : les routes HTTP ne construisent rien. Elles recoivent un objet
`Services` deja pret, ce qui les rend lisibles et testables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .audit import AuditLogger
from .config import Settings
from .llm import GatewayClient, Synthesizer, ToolPlanner
from .mcp import McpClient, ToolRegistry
from .pipeline import ChatPipeline
from .policy import PolicyEngine, RateLimiter
from .security import JwtService, RevocationStore, ensure_keypair

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Conteneur de services, attache a app.state au demarrage."""

    settings: Settings
    jwt_service: JwtService
    revocation: RevocationStore
    policy: PolicyEngine
    registry: ToolRegistry
    mcp_client: McpClient
    gateway: GatewayClient
    planner: ToolPlanner
    synthesizer: Synthesizer
    audit: AuditLogger
    pipeline: ChatPipeline

    @classmethod
    async def startup(cls, settings: Settings) -> "Services":
        """Prepare tous les services et interroge les serveurs MCP."""
        if ensure_keypair(settings.private_key_path, settings.public_key_path):
            logger.warning(
                "Paire de cles JWT generee dans %s (a conserver hors du depot)",
                settings.keys_dir,
            )

        revocation = RevocationStore(settings.revocation_path)
        revocation.purge_expired()

        jwt_service = JwtService(settings, revocation)
        policy = PolicyEngine(settings.policies_path, RateLimiter())
        gateway = GatewayClient(settings)
        mcp_client = McpClient(settings)
        registry = ToolRegistry(settings, mcp_client)
        planner = ToolPlanner(gateway, settings)
        synthesizer = Synthesizer(gateway, settings)
        audit = AuditLogger(settings.audit_log_path)

        pipeline = ChatPipeline(
            settings=settings,
            policy=policy,
            registry=registry,
            mcp_client=mcp_client,
            gateway=gateway,
            planner=planner,
            synthesizer=synthesizer,
            audit=audit,
        )

        services = cls(
            settings=settings,
            jwt_service=jwt_service,
            revocation=revocation,
            policy=policy,
            registry=registry,
            mcp_client=mcp_client,
            gateway=gateway,
            planner=planner,
            synthesizer=synthesizer,
            audit=audit,
            pipeline=pipeline,
        )

        await registry.refresh()
        return services

    async def shutdown(self) -> None:
        """Ferme ce qui doit l'etre."""
        await self.gateway.aclose()
