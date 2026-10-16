"""Couche MCP : annuaire des outils et client de dialogue."""

from .client import McpClient, McpServerError, ToolCallResult
from .registry import ToolDescriptor, ToolRegistry

__all__ = [
    "McpClient",
    "McpServerError",
    "ToolCallResult",
    "ToolDescriptor",
    "ToolRegistry",
]
