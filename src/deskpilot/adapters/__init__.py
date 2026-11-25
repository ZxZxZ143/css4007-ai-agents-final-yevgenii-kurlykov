"""Adapter SDK: tool specs, registry and the system adapter."""

from deskpilot.adapters.base import (
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolSpec,
    VerifyResult,
)
from deskpilot.adapters.registry import ConformanceError, ToolRegistry
from deskpilot.adapters.system import register_system_tools

__all__ = [
    "ConformanceError",
    "ToolContext",
    "ToolError",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "VerifyResult",
    "register_system_tools",
]
