"""Tool abstractions and registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from gismo.core.state import StateStore


@dataclass
class Tool:
    name: str
    description: str
    schema: Optional[Dict[str, Any]] = None

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool '{name}' is not registered") from exc


class EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="Echo back the provided input",
            schema={"type": "object"},
        )

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        return {"echo": tool_input}


class WriteNoteTool(Tool):
    def __init__(self, state_store: StateStore) -> None:
        super().__init__(
            name="write_note",
            description="Write a note to the state store as a task output",
            schema={"type": "object", "properties": {"note": {"type": "string"}}},
        )
        self._state_store = state_store

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        note = tool_input.get("note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("'note' must be a non-empty string")
        return {"note": note}
