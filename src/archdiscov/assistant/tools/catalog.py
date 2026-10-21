"""The fixed assistant tool set, in the order providers see it."""

from .analysis_tools import ANALYSIS_TOOLS
from .query_tools import QUERY_TOOLS
from .registry import ToolDefinition, ToolRegistry

ALL_TOOLS: tuple[ToolDefinition, ...] = QUERY_TOOLS + ANALYSIS_TOOLS


def default_registry() -> ToolRegistry:
    return ToolRegistry(ALL_TOOLS)
