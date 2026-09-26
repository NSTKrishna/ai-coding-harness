"""Tool substrate: repository-confined file, search, patch, command and git tools."""

from __future__ import annotations

from typing import Optional

from harness.tools import commands, files, git, patch, search
from harness.tools.base import Tool, ToolContext, ToolError, ToolFailure, ToolLimits, ToolResult
from harness.tools.registry import Redactor, ToolRegistry

ALL_TOOLS = files.TOOLS + search.TOOLS + patch.TOOLS + commands.TOOLS + git.TOOLS


def build_registry(ctx: ToolContext, *, redactor: Optional[Redactor] = None) -> ToolRegistry:
    """A registry with every built-in tool registered."""
    registry = ToolRegistry(ctx, redactor=redactor)
    registry.register_all(ALL_TOOLS)
    return registry


__all__ = [
    "ALL_TOOLS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolFailure",
    "ToolLimits",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]
