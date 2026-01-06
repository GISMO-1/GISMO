"""Orchestrator tying state, tools, and agents together."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple, Type

from gismo.core.agent import Agent
from gismo.core.models import (
    FailureType,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    ToolReceipt,
    ToolReceiptStatus,
)
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import ToolRegistry
from gismo.core.tool_receipts import (
    build_policy_snapshot,
    canonical_json,
    redact_payload,
    sha256_payload,
    tool_kind_for_name,
)


@dataclass
class Orchestrator:
    state_store: StateStore
    registry: ToolRegistry
    policy: PermissionPolicy
    agent: Agent

    def run_tool(
        self,
        run_id: str,
        task: Task,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        max_attempts: int = 1,
        backoff_base_seconds: float = 0.25,
        backoff_multiplier: float = 2.0,
        retryable_exceptions: Optional[Tuple[Type[BaseException], ...]] = None,
    ) -> Task:
        normalized_input = _normalize_input(tool_input)
        task.input_hash = _stable_hash(normalized_input)
        run_context = _receipt_run_context(self.state_store, run_id)

        prior = self.state_store.find_succeeded_task_by_idempotency(
            task.idempotency_key,
            task.input_hash,
        )
        if prior is not None:
            output = prior.output_json or {}
            task.mark_succeeded(output)
            skip_message = (
                "Idempotent skip: task already succeeded for idempotency_key and input_hash."
            )
            tool_call = ToolCall(
                run_id=run_id,
                task_id=task.id,
                tool_name=tool_name,
                input_json=tool_input,
                status=ToolCallStatus.SKIPPED,
                error=skip_message,
                output_json=output,
            )
            tool_call.finished_at = _utc_now()
            tool_call.failure_type = FailureType.NONE
            receipt = _build_tool_receipt(
                tool_call=tool_call,
                run_context=run_context,
                tool_input=tool_input,
                response_payload={
                    "skipped": True,
                    "reason": skip_message,
                    "output": output,
                },
                status=ToolReceiptStatus.SUCCESS,
                policy_snapshot=None,
                error_type=None,
                error_message=None,
            )
            with self.state_store.transaction() as connection:
                self.state_store.record_tool_call(tool_call, connection=connection)
                self.state_store.record_tool_receipt(receipt, connection=connection)
                self.state_store.update_task(task, connection=connection)
            return task

        task.mark_running()
        with self.state_store.transaction() as connection:
            self.state_store.update_task(task, connection=connection)

        retryable = retryable_exceptions or (RuntimeError, sqlite3.OperationalError)
        max_attempts = max(1, max_attempts)

        for attempt in range(1, max_attempts + 1):
            tool_call = ToolCall(
                run_id=run_id,
                task_id=task.id,
                tool_name=tool_name,
                input_json=tool_input,
                attempt_number=attempt,
            )
            with self.state_store.transaction() as connection:
                self.state_store.record_tool_call(tool_call, connection=connection)

            try:
                self.policy.check_tool_allowed(tool_name)
                output = self.agent.execute(task, tool_name, tool_input)
            except Exception as exc:  # noqa: BLE001 - fail fast with explicit exception
                failure_type, can_retry = _classify_exception(exc, retryable)
                error_message = _safe_error_message(exc)
                policy_snapshot = build_policy_snapshot(
                    self.policy,
                    tool_name,
                    allowed=not isinstance(exc, PermissionError),
                )
                tool_call.mark_failed(error_message, failure_type)
                receipt = _build_tool_receipt(
                    tool_call=tool_call,
                    run_context=run_context,
                    tool_input=tool_input,
                    response_payload={
                        "error": error_message,
                        "error_type": exc.__class__.__name__,
                    },
                    status=ToolReceiptStatus.ERROR,
                    policy_snapshot=policy_snapshot,
                    error_type=exc.__class__.__name__,
                    error_message=error_message,
                )
                with self.state_store.transaction() as connection:
                    self.state_store.update_tool_call(tool_call, connection=connection)
                    self.state_store.record_tool_receipt(receipt, connection=connection)
                    task.error = error_message
                    task.failure_type = failure_type
                    task.updated_at = _utc_now()
                    self.state_store.update_task(task, connection=connection)

                if can_retry and attempt < max_attempts:
                    backoff = backoff_base_seconds * (backoff_multiplier ** (attempt - 1))
                    time.sleep(backoff)
                    continue

                task.mark_failed(error_message, failure_type)
                with self.state_store.transaction() as connection:
                    self.state_store.update_task(task, connection=connection)
                return task

            tool_call.mark_succeeded(output)
            receipt = _build_tool_receipt(
                tool_call=tool_call,
                run_context=run_context,
                tool_input=tool_input,
                response_payload=output,
                status=ToolReceiptStatus.SUCCESS,
                policy_snapshot=build_policy_snapshot(
                    self.policy,
                    tool_name,
                    allowed=True,
                ),
                error_type=None,
                error_message=None,
            )
            with self.state_store.transaction() as connection:
                self.state_store.update_tool_call(tool_call, connection=connection)
                self.state_store.record_tool_receipt(receipt, connection=connection)
                task.mark_succeeded(output)
                self.state_store.update_task(task, connection=connection)
            return task

        return task

    def run_task_graph(
        self,
        run_id: str,
        *,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Task]:
        tasks = {task.id: task for task in self.state_store.list_tasks(run_id)}
        if not tasks:
            return {}

        while True:
            runnable: list[Task] = []
            pending = [task for task in tasks.values() if task.status == TaskStatus.PENDING]
            if should_cancel and pending and should_cancel():
                error = "Cancellation requested."
                for task in pending:
                    task.mark_failed(error, FailureType.SYSTEM_ERROR, status_reason=error)
                    with self.state_store.transaction() as connection:
                        self.state_store.update_task(task, connection=connection)
                break

            for task in pending:
                if not task.depends_on:
                    runnable.append(task)
                    continue

                missing = [dep for dep in task.depends_on if dep not in tasks]
                if missing:
                    error = f"Dependency missing: {', '.join(missing)}"
                    task.mark_failed(error, FailureType.SYSTEM_ERROR, status_reason=error)
                    with self.state_store.transaction() as connection:
                        self.state_store.update_task(task, connection=connection)
                    continue

                failed_deps = [
                    dep_id
                    for dep_id in task.depends_on
                    if tasks[dep_id].status == TaskStatus.FAILED
                ]
                if failed_deps:
                    error = f"Dependency failed: {failed_deps[0]}"
                    task.mark_failed(error, FailureType.SYSTEM_ERROR, status_reason=error)
                    with self.state_store.transaction() as connection:
                        self.state_store.update_task(task, connection=connection)
                    continue

                if all(tasks[dep_id].status == TaskStatus.SUCCEEDED for dep_id in task.depends_on):
                    runnable.append(task)

            if runnable:
                for task in runnable:
                    tool_name, tool_input = _task_tool_spec(task)
                    if tool_name is None:
                        error = "Invalid task input: missing tool or payload"
                        task.mark_failed(error, FailureType.INVALID_INPUT, status_reason=error)
                        with self.state_store.transaction() as connection:
                            self.state_store.update_task(task, connection=connection)
                        continue
                    updated = self.run_tool(run_id, task, tool_name, tool_input)
                    tasks[task.id] = updated
                continue

            pending = [task for task in tasks.values() if task.status == TaskStatus.PENDING]
            if not pending:
                break

            diagnostics = "; ".join(
                f"{task.id} depends_on={task.depends_on}" for task in pending
            )
            error = f"Deadlock/cycle detected: {diagnostics}"
            for task in pending:
                task.mark_failed(error, FailureType.SYSTEM_ERROR, status_reason=error)
                with self.state_store.transaction() as connection:
                    self.state_store.update_task(task, connection=connection)
            break

        return tasks


def _normalize_input(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_hash(normalized_payload: str) -> str:
    return hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()


def _classify_exception(
    exc: BaseException,
    retryable: Tuple[Type[BaseException], ...],
) -> Tuple[FailureType, bool]:
    if isinstance(exc, PermissionError):
        return FailureType.PERMISSION_DENIED, False
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return FailureType.INVALID_INPUT, False
    if isinstance(exc, retryable):
        return FailureType.TOOL_ERROR, True
    return FailureType.SYSTEM_ERROR, False


def _safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    return message or exc.__class__.__name__


@dataclass(frozen=True)
class ToolReceiptContext:
    run_id: str
    session_id: Optional[str]
    role_id: Optional[str]
    role_name: Optional[str]
    plan_event_id: Optional[str]


def _receipt_run_context(state_store: StateStore, run_id: str) -> ToolReceiptContext:
    run = state_store.get_run(run_id)
    session_id = None
    role_id = None
    role_name = None
    plan_event_id = None
    if run and isinstance(run.metadata_json, dict):
        plan_event_id = run.metadata_json.get("plan_event_id")
        agent_session = run.metadata_json.get("agent_session")
        if isinstance(agent_session, dict):
            session_id = agent_session.get("session_id")
        agent_role = run.metadata_json.get("agent_role")
        if isinstance(agent_role, dict):
            role_id = agent_role.get("role_id")
            role_name = agent_role.get("role_name")
    return ToolReceiptContext(
        run_id=run_id,
        session_id=session_id,
        role_id=role_id,
        role_name=role_name,
        plan_event_id=plan_event_id,
    )


def _build_tool_receipt(
    *,
    tool_call: ToolCall,
    run_context: ToolReceiptContext,
    tool_input: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: ToolReceiptStatus,
    policy_snapshot: Optional[Dict[str, Any]],
    error_type: Optional[str],
    error_message: Optional[str],
) -> ToolReceipt:
    started_at = tool_call.started_at
    finished_at = tool_call.finished_at or _utc_now()
    duration_ms = max(
        0,
        int((finished_at - started_at).total_seconds() * 1000),
    )
    redacted_request = redact_payload(tool_input)
    redacted_response = redact_payload(response_payload)
    request_payload_json = canonical_json(redacted_request)
    response_payload_json = canonical_json(redacted_response)
    return ToolReceipt(
        run_id=run_context.run_id,
        session_id=run_context.session_id,
        role_id=run_context.role_id,
        role_name=run_context.role_name,
        plan_event_id=run_context.plan_event_id,
        tool_name=tool_call.tool_name,
        tool_kind=tool_kind_for_name(tool_call.tool_name),
        request_payload_json=request_payload_json,
        response_payload_json=response_payload_json,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        request_sha256=sha256_payload(request_payload_json),
        response_sha256=sha256_payload(response_payload_json),
        error_type=error_type,
        error_message=error_message,
        policy_snapshot=policy_snapshot,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _task_tool_spec(task: Task) -> Tuple[Optional[str], Dict[str, Any]]:
    if not isinstance(task.input_json, dict):
        return None, {}
    tool_name = task.input_json.get("tool")
    payload = task.input_json.get("payload")
    if not tool_name or not isinstance(payload, dict):
        return None, {}
    return tool_name, payload
