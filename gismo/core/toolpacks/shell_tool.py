"""Restricted shell tool for safe local execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from gismo.core.execution import (
    build_execution_request,
    build_worker_command,
    run_isolated_process,
)
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

    def run(
        self,
        tool_input: Dict[str, Any],
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
        execution_context = context or {}
        request = build_execution_request(
            component="run_shell",
            action="execute",
            actor=str(execution_context.get("actor") or "worker"),
            resource="tool:run_shell",
            db_path=_optional_context_str(execution_context, "db_path"),
            related_run_id=_optional_context_str(execution_context, "related_run_id"),
            related_task_id=_optional_context_str(execution_context, "related_task_id"),
            related_plan_id=_optional_context_str(execution_context, "related_plan_id"),
        )
        result = run_isolated_process(
            request,
            worker_command=build_worker_command("shell"),
            worker_input={
                "command": list(command),
                "cwd": str(cwd),
                "timeout_seconds": self._config.timeout_seconds,
            },
            timeout_s=self._config.timeout_seconds,
            event_details={
                "cwd": str(cwd),
                "allowlisted": True,
            },
        )
        return {
            "command": command,
            "cwd": str(cwd),
            "stdout": str(result.get("stdout") or ""),
            "stderr": str(result.get("stderr") or ""),
            "exit_code": int(result.get("exit_code") or 0),
            "execution": dict(result.get("execution") or {}),
        }


def _optional_context_str(context: Dict[str, Any], field_name: str) -> str | None:
    value = context.get(field_name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
