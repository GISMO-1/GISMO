"""Orchestrator tying state, tools, and agents together."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from gismo.core.agent import Agent
from gismo.core.models import Task, ToolCall
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import ToolRegistry


@dataclass
class Orchestrator:
    state_store: StateStore
    registry: ToolRegistry
    policy: PermissionPolicy
    agent: Agent

    def run_tool(self, run_id: str, task: Task, tool_name: str, tool_input: Dict[str, Any]) -> Task:
        task.mark_running()
        self.state_store.update_task(task)

        tool_call = ToolCall(
            run_id=run_id,
            task_id=task.id,
            tool_name=tool_name,
            input_json=tool_input,
        )

        try:
            self.policy.check_tool_allowed(tool_name)
        except PermissionError as exc:
            tool_call.mark_failed(str(exc))
            self.state_store.record_tool_call(tool_call)
            task.mark_failed(str(exc))
            self.state_store.update_task(task)
            return task

        self.state_store.record_tool_call(tool_call)

        try:
            output = self.agent.execute(task, tool_name, tool_input)
        except Exception as exc:  # noqa: BLE001 - fail fast with explicit exception
            tool_call.mark_failed(str(exc))
            self.state_store.update_tool_call(tool_call)
            task.mark_failed(str(exc))
            self.state_store.update_task(task)
            return task

        tool_call.mark_succeeded(output)
        self.state_store.update_tool_call(tool_call)
        task.mark_succeeded(output)
        self.state_store.update_task(task)
        return task
