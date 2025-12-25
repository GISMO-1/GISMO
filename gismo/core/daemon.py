"""Daemon loop for executing queued operator commands."""
from __future__ import annotations

import signal
import sqlite3
import time
import threading
from pathlib import Path
from typing import Callable, Optional

from gismo.cli.operator import make_idempotency_key, normalize_command, parse_command, required_tools
from gismo.core.agent import SimpleAgent
from gismo.core.models import FailureType, QueueItem, Task, TaskStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool
from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool


RegistryFactory = Callable[[StateStore, PermissionPolicy], ToolRegistry]


def run_daemon_loop(
    state: StateStore,
    policy_path: str | None,
    sleep_seconds: float,
    once: bool,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    stop_event = threading.Event()
    _register_shutdown_handlers(stop_event)
    while not stop_event.is_set():
        if state.get_daemon_paused():
            stop_event.wait(sleep_seconds)
            continue
        item = state.claim_next_queue_item()
        if item is None:
            if once:
                return
            stop_event.wait(sleep_seconds)
            continue
        _execute_queue_item(state, item, policy_path, repo_root)


def _execute_queue_item(
    state: StateStore,
    item: QueueItem,
    policy_path: str | None,
    repo_root: Path,
    *,
    registry_factory: Optional[RegistryFactory] = None,
) -> None:
    try:
        policy, plan, normalized = _load_policy_and_plan(policy_path, repo_root, item.command_text)
        registry = (registry_factory or build_registry)(state, policy)
        agent = SimpleAgent(registry=registry)
        orchestrator = Orchestrator(
            state_store=state,
            registry=registry,
            policy=policy,
            agent=agent,
        )

        run_id = _resolve_run_id(state, item, normalized)
        created_tasks = []
        previous_task_id = None
        for index, step in enumerate(plan["steps"]):
            tool_name = step["tool_name"]
            tool_input = step["input_json"]
            idempotency_key = make_idempotency_key(step, normalized, index)
            depends_on = [previous_task_id] if plan["mode"] == "graph" and previous_task_id else None
            task = state.create_task(
                run_id=run_id,
                title=step["title"],
                description="Daemon queue step",
                input_json={"tool": tool_name, "payload": tool_input},
                depends_on=depends_on,
                idempotency_key=idempotency_key,
            )
            created_tasks.append(task)
            previous_task_id = task.id

        if plan["mode"] == "single":
            task = created_tasks[0]
            result = orchestrator.run_tool(
                run_id,
                task,
                task.input_json["tool"],
                task.input_json["payload"],
            )
            _raise_on_failed_tasks([result])
        else:
            results = orchestrator.run_task_graph(run_id)
            _raise_on_failed_tasks(list(results.values()))

        state.mark_queue_item_succeeded(item.id)
    except Exception as exc:  # noqa: BLE001
        retryable = _is_retryable_queue_error(exc)
        state.mark_queue_item_failed(item.id, _safe_error_message(exc), retryable=retryable)


def _resolve_run_id(state: StateStore, item: QueueItem, normalized_command: str) -> str:
    metadata = {
        "command": normalized_command,
        "queue_item_id": item.id,
        "source": "daemon",
    }
    if item.run_id:
        existing = state.get_run(item.run_id)
        if existing is not None:
            return existing.id
        metadata["requested_run_id"] = item.run_id
    run = state.create_run(label="daemon", metadata=metadata)
    return run.id


def _load_policy_and_plan(
    policy_path: str | None,
    repo_root: Path,
    command_text: str,
) -> tuple[PermissionPolicy, dict, str]:
    plan = parse_command(command_text)
    normalized = normalize_command(command_text)
    default_tools = required_tools(plan) if policy_path is None else ()
    resolved_policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(
        resolved_policy_path,
        repo_root=repo_root,
        default_allowed_tools=default_tools,
    )
    return policy, plan, normalized


def _raise_on_failed_tasks(tasks: list[Task]) -> None:
    failed = [task for task in tasks if task.status == TaskStatus.FAILED]
    if not failed:
        return
    task = failed[0]
    error = task.error or "Task failed"
    if task.failure_type == FailureType.PERMISSION_DENIED:
        raise PermissionError(error)
    if task.failure_type == FailureType.INVALID_INPUT:
        raise ValueError(error)
    raise RuntimeError(error)


def _is_retryable_queue_error(exc: BaseException) -> bool:
    if isinstance(exc, (PermissionError, ValueError)):
        return False
    return isinstance(exc, (RuntimeError, sqlite3.OperationalError))


def _safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    return message or exc.__class__.__name__


def _resolve_default_policy_path(policy_path: str | None, repo_root: Path) -> tuple[str | None, bool]:
    if policy_path:
        return policy_path, False
    readonly_path = repo_root / "policy" / "readonly.json"
    if readonly_path.exists():
        return str(readonly_path), False
    return None, True


def _warn_missing_default_policy() -> None:
    print(
        "Warning: no policy provided and no policy/readonly.json found; "
        "continuing with existing default tool allowances.",
        flush=True,
    )


def build_registry(state_store: StateStore, policy: PermissionPolicy) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(WriteNoteTool(state_store))
    fs_config = FileSystemConfig(base_dir=policy.fs.base_dir)
    registry.register(ReadFileTool(fs_config))
    registry.register(WriteFileTool(fs_config))
    registry.register(ListDirTool(fs_config))
    shell_config = ShellConfig(
        base_dir=policy.shell.base_dir,
        allowlist=policy.shell.allowlist,
        timeout_seconds=policy.shell.timeout_seconds,
    )
    registry.register(ShellTool(shell_config))
    return registry


def _register_shutdown_handlers(stop_event: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def _handle_signal(signum: int, _frame: object) -> None:
        stop_event.set()
        signal_name = getattr(signal.Signals(signum), "name", str(signum))
        print(f"Daemon shutdown requested ({signal_name}).", flush=True)

    for signal_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        signal.signal(sig, _handle_signal)
