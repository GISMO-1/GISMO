"""Agent abstractions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from gismo.core.models import Task
from gismo.core.tools import ToolRegistry


@dataclass
class Agent:
    registry: ToolRegistry

    def execute(self, task: Task, tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class SimpleAgent(Agent):
    def execute(self, task: Task, tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.registry.get(tool_name)
        return tool.run(tool_input)
