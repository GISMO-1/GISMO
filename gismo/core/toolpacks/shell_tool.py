"""Restricted shell tool for safe local execution."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from gismo.core.toolpacks.path_utils import resolve_within_base
from gismo.core.tools import Tool


@dataclass
class ShellConfig:
    base_dir: Path
    allowlist: List[List[str]] = field(default_factory=list)
    timeout_seconds: float = 10.0


class ShellTool(Tool):
    def __init__(self, config: ShellConfig) -> None:
        super().__init__(
            name="run_shell",
            description="Run an allowlisted shell command with restricted working directory",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                },
            },
        )
        self._config = config

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        command = tool_input.get("command")
        cwd_input = tool_input.get("cwd")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError("command must be a non-empty list of strings")
        if command not in self._config.allowlist:
            raise PermissionError("Command is not in the allowlist")
        if cwd_input is None:
            cwd = self._config.base_dir.resolve()
        else:
            cwd = resolve_within_base(self._config.base_dir, cwd_input)

        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=self._config.timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "cwd": str(cwd),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
