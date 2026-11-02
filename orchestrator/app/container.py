"""Construction et cycle de vie des services.

Un seul endroit qui assemble les briques (configuration, securite, politiques,
registre MCP, modele, pipeline) et un seul endroit qui les arrete proprement.

Interet : les routes HTTP ne construisent rien. Elles recoivent un objet
`Services` deja pret, ce qui les rend lisibles et testables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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

        await _refresh_registry_until_ready(registry)
        return services

    async def shutdown(self) -> None:
        """Ferme ce qui doit l'etre."""
        await self.gateway.aclose()


async def _refresh_registry_until_ready(registry: ToolRegistry) -> None:
    """Interroge les serveurs MCP jusqu'a ce qu'ils repondent.

    Au demarrage de Windows, le conteneur de l'orchestrateur repart souvent
    AVANT les serveurs MCP de l'hote (il porte ``restart: unless-stopped``).
    Une seule tentative laissait alors un registre vide pour toute la duree de
    vie du conteneur : les serveurs MCP etaient sains une seconde plus tard,
    mais l'orchestrateur ne les revoyait jamais et repondait
    ``tools_known: 0`` jusqu'au prochain redemarrage manuel.

    On reessaie donc avec un backoff borne, puis on abandonne proprement :
    un serveur reellement absent ne doit pas empecher l'orchestrateur de servir
    les requetes sans outils.
    """
    budget = float(os.environ.get("MCP_REGISTRY_WAIT_SECONDS", "90"))
    deadline = time.monotonic() + budget
    delay = 1.0
    tentative = 0

    while True:
        tentative += 1
        status = await registry.refresh()
        if registry.count() > 0 and all(status.values()):
            if tentative > 1:
                logger.info(
                    "Registre MCP pret apres %s tentative(s) : %s outils",
                    tentative,
                    registry.count(),
                )
            return
        if time.monotonic() >= deadline:
            logger.warning(
                "Registre MCP incomplet apres %.0fs (%s outils) : "
                "l'orchestrateur demarre quand meme, sans ces outils",
                budget,
                registry.count(),
            )
            return
        logger.info(
            "Serveurs MCP pas encore prets (%s outils, essai %s) : nouvelle tentative dans %.0fs",
            registry.count(),
            tentative,
            delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5.0)
