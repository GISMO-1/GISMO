"""Permission gating for tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Set


@dataclass
class PermissionPolicy:
    allowed_tools: Set[str] = field(default_factory=set)

    def allow(self, tool_name: str) -> None:
        self.allowed_tools.add(tool_name)

    def revoke(self, tool_name: str) -> None:
        self.allowed_tools.discard(tool_name)

    def check_tool_allowed(self, tool_name: str) -> None:
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"Tool '{tool_name}' is not allowed")
