"""Narrow plugin runtime tool backed by verified manifests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gismo.core.plugin_runtime import execute_verified_plugin
from gismo.core.tools import Tool


class PluginRuntimeTool(Tool):
    def __init__(self, *, trust_store_path: str | None = None) -> None:
        super().__init__(
            name="plugin_runtime",
            description="Execute a verified plugin manifest with marshaled JSON input",
            schema={
                "type": "object",
                "properties": {
                    "manifest_path": {"type": "string"},
                    "payload": {"type": "object"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["manifest_path"],
            },
        )
        self._trust_store_path = trust_store_path

    def run(
        self,
        tool_input: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest_path = str(tool_input.get("manifest_path") or "").strip()
        if not manifest_path:
            raise ValueError("manifest_path is required")
        payload = tool_input.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        timeout_s = tool_input.get("timeout_s")
        if timeout_s is not None and (not isinstance(timeout_s, (int, float)) or float(timeout_s) <= 0):
            raise ValueError("timeout_s must be > 0")
        execution_context = context or {}
        trust_store_path = self._trust_store_path
        if trust_store_path is None and execution_context.get("db_path"):
            trust_store_path = str(Path(str(execution_context["db_path"])).resolve().parent / "plugin-trust.json")
        return execute_verified_plugin(
            manifest_path,
            payload=dict(payload or {}),
            trust_store_path=trust_store_path,
            db_path=_optional_context_str(execution_context, "db_path"),
            actor=_optional_context_str(execution_context, "actor") or "worker",
            related_run_id=_optional_context_str(execution_context, "related_run_id"),
            related_task_id=_optional_context_str(execution_context, "related_task_id"),
            related_plan_id=_optional_context_str(execution_context, "related_plan_id"),
            timeout_s=float(timeout_s) if timeout_s is not None else None,
        )


def _optional_context_str(context: dict[str, Any], field_name: str) -> str | None:
    value = context.get(field_name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
