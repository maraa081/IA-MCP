"""Couche d'acces au modele de langage.

Un seul point de sortie vers la gateway LiteLLM (gateway_client.py), puis deux
usages specialises :
  - planner.py     : choisir les outils a appeler (le "cerveau" de l'orchestrateur) ;
  - synthesizer.py : rediger la reponse finale a partir des resultats d'outils.
"""

from .gateway_client import GatewayClient, GatewayReply, GatewayUnavailable, ToolCallRequest
from .planner import (
    RESPOND_DIRECTLY_TOOL,
    Plan,
    PlannedCall,
    ToolPlanner,
    assistant_tool_call_message,
    tool_result_message,
)
from .synthesizer import Synthesizer

__all__ = [
    "GatewayClient",
    "GatewayReply",
    "GatewayUnavailable",
    "ToolCallRequest",
    "Plan",
    "PlannedCall",
    "ToolPlanner",
    "RESPOND_DIRECTLY_TOOL",
    "assistant_tool_call_message",
    "tool_result_message",
    "Synthesizer",
]
