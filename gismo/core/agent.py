"""Agent abstractions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from gismo.core.models import Task
from gismo.core.tools import ToolRegistry, tool_accepts_context


@dataclass
class Agent:
    registry: ToolRegistry

    def execute(
        self,
        task: Task,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class SimpleAgent(Agent):
    def execute(
        self,
        task: Task,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        tool = self.registry.get(tool_name)
        if context is not None and tool_accepts_context(tool):
            return tool.run(tool_input, context=context)
        return tool.run(tool_input)
