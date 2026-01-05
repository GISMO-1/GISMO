"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from gismo.cli.operator import (
    make_idempotency_key,
    normalize_command,
    parse_command,
    required_tools,
)
from gismo.cli import ipc as ipc_cli
from gismo.cli import supervise as supervise_cli
from gismo.cli.windows_startup import (
    install_windows_startup_launcher,
    uninstall_windows_startup_launcher,
)
from gismo.cli.windows_tasks import WindowsTaskConfig, install_windows_task, uninstall_windows_task
from gismo.cli.windows_utils import quote_windows_arg
from gismo.core.agent import SimpleAgent
from gismo.core.daemon import run_daemon_loop
from gismo.core.export import export_latest_run_jsonl, export_run_jsonl
from gismo.core.models import EVENT_TYPE_ASK_FAILED, EVENT_TYPE_LLM_PLAN, QueueStatus, TaskStatus
from gismo.core.maintenance import run_maintenance_iteration
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.plan_assess import PlanAssessment, assess_plan, expanded_explanation
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool
from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool
from gismo.llm.ollama import OllamaError, ollama_chat, resolve_ollama_config
from gismo.llm.prompts import build_system_prompt, build_user_prompt
from gismo.memory.store import (
    MemoryItem,
    get_item as memory_get_item,
    list_prompt_items as memory_list_prompt_items,
    policy_hash_for_path,
    put_item as memory_put_item,
    record_event as memory_record_event,
    search_items as memory_search_items,
    tombstone_item as memory_tombstone_item,
)


def _fmt_dt(dt) -> str:
    return dt.isoformat(timespec="seconds") if dt else "-"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


def _summarize_value(value: object, max_len: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _truncate(text, max_len)


def _serialize_memory_item(item: MemoryItem) -> dict[str, object]:
    return {
        "id": item.id,
        "namespace": item.namespace,
        "key": item.key,
        "kind": item.kind,
        "value": item.value,
        "tags": item.tags,
        "confidence": item.confidence,
        "source": item.source,
        "ttl_seconds": item.ttl_seconds,
        "is_tombstoned": item.is_tombstoned,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _print_memory_item_summary(item: MemoryItem) -> None:
    print(f"Namespace:  {item.namespace}")
    print(f"Key:        {item.key}")
    print(f"Kind:       {item.kind}")
    print(f"Updated:    {item.updated_at}")
    if item.is_tombstoned:
        print("Status:     tombstoned")


def _print_memory_search_results(items: list[MemoryItem]) -> None:
    if not items:
        print("(no matches)")
        return
    for item in items:
        print(
            f"- {item.namespace}/{item.key} kind={item.kind} "
            f"updated={item.updated_at} tombstoned={item.is_tombstoned}"
        )


def _coerce_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _is_grounded_assumption(text: str) -> bool:
    lowered = text.strip().lower()
    return (
        lowered.startswith("operator requested")
        or lowered.startswith("user requested")
        or lowered.startswith("user asked")
        or lowered.startswith("operator asked")
    )


def _coerce_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced < minimum:
        return default
    return coerced


def _coerce_action_type_to_command(action_type_text: str) -> str | None:
    if not action_type_text:
        return None
    candidate = action_type_text.strip()
    if ":" not in candidate:
        return None
    lowered = candidate.lower()
    if not (
        lowered.startswith("echo:")
        or lowered.startswith("note:")
        or lowered.startswith("graph:")
        or lowered.startswith("shell:")
        or lowered.startswith("run_shell:")
    ):
        return None
    try:
        parse_command(candidate)
    except ValueError:
        return None
    return candidate


def _first_non_option_token(argv: list[str]) -> str | None:
    skip_next = False
    force_positional = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if force_positional:
            return token
        if token == "--":
            force_positional = True
            continue
        if token in {"--db", "--db-path"}:
            skip_next = True
            continue
        if token.startswith("-") and token != "-":
            continue
        return token
    return None


def _is_shell_prompt_token(token: str) -> bool:
    candidate = token.strip()
    if not candidate:
        return False
    if candidate.startswith("PS"):
        return True
    if candidate.startswith("(.venv)"):
        return True
    if candidate.startswith(">"):
        return True
    if re.match(r"^[A-Za-z]:\\\\", candidate):
        return True
    return False


def _has_shell_prompt_paste(argv: list[str]) -> bool:
    token = _first_non_option_token(argv)
    if token is None:
        return False
    return _is_shell_prompt_token(token)


def _is_valid_run_id_format(run_id: str) -> bool:
    try:
        UUID(run_id)
    except (TypeError, ValueError):
        return False
    return True


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if "```" not in cleaned:
        return cleaned
    cleaned = re.sub(r"```[a-zA-Z0-9_-]*", "", cleaned)
    return cleaned.strip()


def extract_json_object(text: str) -> str | None:
    cleaned = _strip_code_fences(text).strip()
    if not cleaned:
        return None
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return cleaned[start : end + 1]


def _normalize_llm_plan(plan: dict, max_actions: int) -> dict:
    allowed_fields = {"intent", "assumptions", "actions", "notes"}
    unknown_fields = set(plan.keys()) - allowed_fields
    if unknown_fields:
        raise ValueError(
            "Plan contains unsupported fields: " + ", ".join(sorted(unknown_fields)) + "."
        )
    intent = plan.get("intent")
    intent_text = intent if isinstance(intent, str) else str(intent) if intent is not None else ""
    assumptions = [
        item for item in _coerce_str_list(plan.get("assumptions")) if _is_grounded_assumption(item)
    ]
    notes = _coerce_str_list(plan.get("notes"))
    raw_actions = plan.get("actions")
    actions: list[dict[str, object]] = []
    if isinstance(raw_actions, list):
        for action in raw_actions:
            if not isinstance(action, dict):
                continue
            allowed_action_fields = {
                "type",
                "command",
                "timeout_seconds",
                "retries",
                "why",
                "risk",
            }
            unknown_action_fields = set(action.keys()) - allowed_action_fields
            if unknown_action_fields:
                raise ValueError(
                    "Action contains unsupported fields: "
                    + ", ".join(sorted(unknown_action_fields))
                    + "."
                )
            action_type = action.get("type")
            action_type_text = (
                action_type.strip()
                if isinstance(action_type, str)
                else str(action_type).strip()
                if action_type is not None
                else ""
            )
            command = action.get("command")
            command_text = (
                command.strip()
                if isinstance(command, str)
                else str(command).strip()
                if command is not None
                else ""
            )
            timeout_seconds = _coerce_int(action.get("timeout_seconds"), 30, minimum=1)
            retries = _coerce_int(action.get("retries"), 0, minimum=0)
            why = action.get("why")
            why_text = why if isinstance(why, str) else str(why) if why is not None else ""
            risk = action.get("risk")
            risk_text = risk.strip().lower() if isinstance(risk, str) else ""
            if risk_text not in {"low", "medium", "high"}:
                risk_text = "medium"
            if action_type_text != "enqueue":
                coerced_command = _coerce_action_type_to_command(action_type_text)
                if coerced_command:
                    action_type_text = "enqueue"
                    command_text = coerced_command
                    timeout_seconds = 30
                    retries = 0
                    risk_text = "medium"
            actions.append(
                {
                    "type": action_type_text,
                    "command": command_text,
                    "timeout_seconds": timeout_seconds,
                    "retries": retries,
                    "why": why_text,
                    "risk": risk_text,
                }
            )
    if max_actions <= 0:
        raise ValueError("max_actions must be > 0")
    original_action_count = len(actions)
    if original_action_count > 12:
        notes.append(
            "Too many actions "
            f"({original_action_count}). This plan is high risk and requires confirmation to "
            "enqueue; consider batching into 12 or fewer steps."
        )
    if original_action_count > max_actions:
        notes.append(
            f"Truncated actions from {original_action_count} to {max_actions} based on --max-actions."
        )
        actions = actions[:max_actions]
    unknown_types = sorted({a["type"] for a in actions if a["type"] and a["type"] != "enqueue"})
    if unknown_types:
        notes.append(f"Ignored unsupported action types: {', '.join(unknown_types)}.")
    return {
        "intent": intent_text,
        "assumptions": assumptions,
        "actions": actions,
        "notes": notes,
    }


def _print_llm_plan(plan: dict) -> None:
    print("=== GISMO LLM Plan ===")
    intent = plan.get("intent") or "unspecified"
    print(f"Intent: {intent}")
    assumptions = plan.get("assumptions") or []
    if assumptions:
        print("Assumptions:")
        for item in assumptions:
            print(f"- {item}")
    else:
        print("Assumptions: none")
    actions = plan.get("actions") or []
    print("Actions:")
    if not actions:
        print("  (none)")
    else:
        for index, action in enumerate(actions, start=1):
            action_type = action.get("type") or "unknown"
            command = action.get("command") or "-"
            print(f"{index}. {action_type}: {command}")
            print(
                "   "
                f"timeout_seconds={action.get('timeout_seconds')} "
                f"retries={action.get('retries')} "
                f"risk={action.get('risk')}"
            )
            why = action.get("why")
            if why:
                print(f"   why: {why}")
    notes = plan.get("notes") or []
    if notes:
        print("Notes:")
        for note in notes:
            print(f"- {note}")


def _print_plan_assessment(assessment: PlanAssessment, *, explain: bool) -> None:
    confidence_label = assessment.confidence.upper()
    print(f"Confidence: {confidence_label}")
    if assessment.risk_flags:
        print(f"Risk flags: {', '.join(assessment.risk_flags)}")
    else:
        print("Risk flags: none")
    print(f"Explanation: {assessment.explanation}")
    if explain:
        details = expanded_explanation(assessment)
        if details:
            print("Explanation details:")
            for detail in details:
                print(f"- {detail}")


def _print_agent_summary(
    *,
    goal: str,
    assessment: PlanAssessment,
    actions_count: int,
    run_ids: list[str],
    final_status: str,
    error_reason: str | None,
) -> None:
    print("=== Agent Summary ===")
    print(f"Goal: {goal}")
    print(f"Plan confidence: {assessment.confidence.upper()}")
    risk_flags = assessment.risk_flags
    if risk_flags:
        print(f"Risk flags: {', '.join(risk_flags)}")
    else:
        print("Risk flags: none")
    print(f"Actions count: {actions_count}")
    print(f"Run ID(s): {', '.join(run_ids) if run_ids else '-'}")
    print(f"Final status: {final_status}")
    if error_reason:
        print(f"Error reason: {error_reason}")


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_assessment(assessment: PlanAssessment, *, yes: bool) -> None:
    if not assessment.requires_confirmation or yes:
        return
    if _is_interactive_tty():
        response = input("This plan requires confirmation. Proceed? [y/N]:")
        if response.strip().lower() not in {"y", "yes"}:
            print("Confirmation declined; plan not enqueued.", file=sys.stderr)
            raise SystemExit(2)
        return
    print(
        "Refusing to enqueue without confirmation in non-interactive mode. Use --yes to override.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _agent_requires_confirmation(assessment: PlanAssessment, actions: list[dict[str, object]]) -> bool:
    if assessment.requires_confirmation:
        return True
    if assessment.confidence == "low":
        return True
    if any(flag in {"shell", "writes"} for flag in assessment.risk_flags):
        return True
    for action in actions:
        risk = action.get("risk")
        if isinstance(risk, str) and risk.strip().lower() == "high":
            return True
    return False


def _confirm_agent_assessment(
    assessment: PlanAssessment,
    actions: list[dict[str, object]],
    *,
    yes: bool,
) -> None:
    if not _agent_requires_confirmation(assessment, actions) or yes:
        return
    if _is_interactive_tty():
        response = input("This plan requires confirmation. Proceed? [y/N]:")
        if response.strip().lower() not in {"y", "yes"}:
            print("Confirmation declined; plan not enqueued.", file=sys.stderr)
            raise SystemExit(2)
        return
    print(
        "Refusing to enqueue without confirmation in non-interactive mode. Use --yes to override.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _load_assessment_policy(policy_path: str | None) -> PermissionPolicy | None:
    repo_root = Path(__file__).resolve().parents[2]
    resolved_path, _ = _resolve_default_policy_path(policy_path, repo_root)
    if resolved_path is None:
        return None
    try:
        return load_policy(resolved_path, repo_root=repo_root)
    except (OSError, ValueError, PermissionError):
        return None


def _run_status(tasks: list) -> str:
    if not tasks:
        return "pending"
    statuses = {task.status for task in tasks}
    if TaskStatus.FAILED in statuses:
        return "failed"
    if TaskStatus.RUNNING in statuses:
        return "running"
    if statuses.issubset({TaskStatus.SUCCEEDED}):
        return "succeeded"
    return "pending"


def _run_time_bounds(
    run,
    tasks,
    tool_calls,
) -> tuple[datetime | None, datetime | None]:
    start_candidates = [run.created_at]
    start_candidates.extend(task.created_at for task in tasks)
    start_candidates.extend(call.started_at for call in tool_calls)
    start_time = min(start_candidates) if start_candidates else None
    end_candidates = [task.updated_at for task in tasks if task.updated_at]
    end_candidates.extend(call.finished_at for call in tool_calls if call.finished_at)
    end_time = max(end_candidates) if end_candidates else None
    return start_time, end_time


def _task_status_counts(tasks: list) -> dict[str, int]:
    counts = {
        "total": len(tasks),
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    for task in tasks:
        if task.status == TaskStatus.PENDING:
            counts["pending"] += 1
        elif task.status == TaskStatus.RUNNING:
            counts["running"] += 1
        elif task.status == TaskStatus.SUCCEEDED:
            counts["succeeded"] += 1
        elif task.status == TaskStatus.FAILED:
            counts["failed"] += 1
    return counts


def _run_last_error(tasks: list, tool_calls: list) -> str | None:
    entries: list[tuple[datetime, str]] = []
    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    for task in tasks:
        if task.error:
            entries.append((task.updated_at or min_dt, str(task.error)))
    for call in tool_calls:
        if call.error:
            entries.append(((call.finished_at or call.started_at or min_dt), str(call.error)))
    if not entries:
        return None
    entries.sort(key=lambda item: item[0])
    return entries[-1][1]


def _tool_output_metadata(output: object) -> str:
    if output is None:
        return "-"
    if isinstance(output, dict):
        keys = ", ".join(sorted(str(k) for k in output.keys()))
        serialized = json.dumps(output, ensure_ascii=False, sort_keys=True)
        return f"keys=[{_truncate(keys, 120)}], chars={len(serialized)}"
    if isinstance(output, list):
        return f"items={len(output)}"
    if isinstance(output, str):
        return f"chars={len(output)}"
    return f"type={type(output).__name__}"


def run_demo(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(policy_path, repo_root=repo_root, default_allowed_tools={"echo"})
    registry = _build_registry(state_store, policy)

    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="demo", metadata={"purpose": "quickstart"})

    echo_task = state_store.create_task(
        run_id=run.id,
        title="Echo input",
        description="Echo the provided payload",
        input_json={"tool": "echo", "payload": {"message": "hello"}},
    )
    orchestrator.run_tool(run.id, echo_task, "echo", {"message": "hello"})

    note_task = state_store.create_task(
        run_id=run.id,
        title="Write note",
        description="Attempt to write a note",
        input_json={"tool": "write_note", "payload": {"note": "Hello, GISMO."}},
    )
    orchestrator.run_tool(run.id, note_task, "write_note", {"note": "Hello, GISMO."})

    policy.allow("write_note")
    orchestrator.run_tool(run.id, note_task, "write_note", {"note": "Hello, GISMO."})

    print("=== GISMO Demo Summary ===")
    print(f"Run: {run.id} ({run.label})")
    print("Tasks:")
    for task in state_store.list_tasks(run.id):
        print(f"- {task.id} {task.title} [{task.status}]")
        if task.error:
            print(f"  error: {task.error}")
        if task.output_json:
            print(f"  output: {task.output_json}")

    print("Tool Calls:")
    for call in state_store.list_tool_calls(run.id):
        print(
            f"- {call.id} tool={call.tool_name} status={call.status} "
            f"started={call.started_at.isoformat()}"
        )
        if call.error:
            print(f"  error: {call.error}")
        if call.output_json:
            print(f"  output: {call.output_json}")


def run_demo_graph(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(
        policy_path,
        repo_root=repo_root,
        default_allowed_tools={"echo", "write_note"},
    )
    registry = _build_registry(state_store, policy)

    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="demo-graph", metadata={"purpose": "dag-demo"})

    task_a = state_store.create_task(
        run_id=run.id,
        title="Echo A",
        description="Echo A",
        input_json={"tool": "echo", "payload": {"message": "A"}},
    )
    task_b = state_store.create_task(
        run_id=run.id,
        title="Note B",
        description="Write note B",
        input_json={"tool": "write_note", "payload": {"note": "B"}},
        depends_on=[task_a.id],
    )
    task_c = state_store.create_task(
        run_id=run.id,
        title="Echo C",
        description="Echo C",
        input_json={"tool": "echo", "payload": {"message": "C"}},
        depends_on=[task_b.id],
    )

    orchestrator.run_task_graph(run.id)

    print("=== GISMO Demo Graph Summary ===")
    print(f"Run: {run.id} ({run.label})")
    print("Tasks:")
    for task in state_store.list_tasks(run.id):
        deps = ", ".join(task.depends_on) if task.depends_on else "none"
        print(f"- {task.id} {task.title} [{task.status}] depends_on={deps}")
        if task.error:
            print(f"  error: {task.error}")
        if task.output_json:
            print(f"  output: {task.output_json}")


def run_operator(db_path: str, command_parts: list[str], policy_path: str | None) -> None:
    command_text = " ".join(command_parts).strip()
    if not command_text:
        raise ValueError("Operator run requires a command string.")

    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    plan = parse_command(command_text)
    normalized = normalize_command(command_text)
    default_tools = required_tools(plan) if policy_path is None else set()
    default_tools.discard("run_shell")
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(policy_path, repo_root=repo_root, default_allowed_tools=default_tools)
    registry = _build_registry(state_store, policy)
    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="operator-run", metadata={"command": normalized})

    created_tasks = []
    previous_task_id = None
    for index, step in enumerate(plan["steps"]):
        tool_name = step["tool_name"]
        tool_input = step["input_json"]
        idempotency_key = make_idempotency_key(step, normalized, index)
        depends_on = [previous_task_id] if plan["mode"] == "graph" and previous_task_id else None
        task = state_store.create_task(
            run_id=run.id,
            title=step["title"],
            description="Operator command step",
            input_json={"tool": tool_name, "payload": tool_input},
            depends_on=depends_on,
            idempotency_key=idempotency_key,
        )
        created_tasks.append(task)
        previous_task_id = task.id

    if plan["mode"] == "single":
        task = created_tasks[0]
        orchestrator.run_tool(run.id, task, task.input_json["tool"], task.input_json["payload"])
    else:
        orchestrator.run_task_graph(run.id)

    _print_operator_summary(state_store, run.id)


def run_show(db_path: str, run_id: str) -> None:
    state_store = StateStore(db_path)
    run = state_store.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        raise SystemExit(2)

    tasks = list(state_store.list_tasks(run.id))
    tool_calls = list(state_store.list_tool_calls(run.id))
    status = _run_status(tasks)
    start_time, end_time = _run_time_bounds(run, tasks, tool_calls)
    counts = _task_status_counts(tasks)

    print("=== GISMO Run Summary ===")
    print(f"Run ID:     {run.id}")
    print(f"Status:     {status}")
    print(f"Started:    {_fmt_dt(start_time)}")
    print(f"Finished:   {_fmt_dt(end_time)}")
    print(
        "Tasks:      "
        f"{counts['total']} "
        f"(pending={counts['pending']} running={counts['running']} "
        f"succeeded={counts['succeeded']} failed={counts['failed']})"
    )
    print("Tasks:")
    if not tasks:
        print("  (no tasks)")
        return

    for task in tasks:
        print(f"- {task.id} {task.title} [{task.status.value}]")
        if task.failure_type and task.failure_type.value != "NONE":
            print(f"  failure_type: {task.failure_type.value}")
        if task.status_reason:
            print(f"  status_reason: {_summarize_value(task.status_reason, 200)}")
        if task.error:
            print(f"  error: {_summarize_value(task.error, 200)}")
        if task.output_json:
            print(f"  output: {_summarize_value(task.output_json, 200)}")
        task_calls = list(state_store.list_tool_calls_for_task(task.id))
        if not task_calls:
            print("  Tool Calls: none")
            continue
        print("  Tool Calls:")
        for call in task_calls:
            print(
                f"    - {call.id} tool={call.tool_name} status={call.status.value} "
                f"started={_fmt_dt(call.started_at)} finished={_fmt_dt(call.finished_at)}"
            )
            if call.failure_type and call.failure_type.value != "NONE":
                print(f"      failure_type: {call.failure_type.value}")
            if call.output_json is not None:
                print(f"      output_meta: {_tool_output_metadata(call.output_json)}")
            if call.output_json:
                print(f"      output: {_summarize_value(call.output_json, 200)}")
            if call.error:
                print(f"      error: {_summarize_value(call.error, 200)}")


def run_list(db_path: str, limit: int, newest_first: bool) -> None:
    state_store = StateStore(db_path)
    runs = list(state_store.list_runs(limit=limit, newest_first=newest_first))

    print(f"DB: {db_path}")
    print(f"Runs: {len(runs)} (limit={limit})")
    header = (
        f"{'RUN ID':8}  {'STATUS':10}  {'CREATED':20}  {'UPDATED':20}  "
        f"{'TASKS':24}  {'LAST ERROR':40}"
    )
    print(header)
    print("-" * len(header))
    for run in runs:
        tasks = list(state_store.list_tasks(run.id))
        tool_calls = list(state_store.list_tool_calls(run.id))
        status = _run_status(tasks)
        _, end_time = _run_time_bounds(run, tasks, tool_calls)
        updated_at = end_time or run.created_at
        counts = _task_status_counts(tasks)
        tasks_summary = (
            f"{counts['total']} "
            f"p{counts['pending']} r{counts['running']} "
            f"s{counts['succeeded']} f{counts['failed']}"
        )
        last_error = _run_last_error(tasks, tool_calls)
        print(
            f"{run.id[:8]:8}  {status:10}  {_fmt_dt(run.created_at):20}  "
            f"{_fmt_dt(updated_at):20}  "
            f"{tasks_summary:24}  {_summarize_value(last_error, 40)}"
        )


@dataclass
class MemoryDecision:
    action: str
    allowed: bool
    confirmation_required: bool
    confirmation_provided: bool
    confirmation_mode: str | None
    reason: str | None


def _memory_policy_hash(policy_path: str | None) -> str:
    try:
        return policy_hash_for_path(policy_path)
    except FileNotFoundError as exc:
        print(f"Policy file not found: {policy_path}")
        raise SystemExit(2) from exc


def _load_memory_policy(policy_path: str | None) -> tuple[PermissionPolicy, str | None]:
    repo_root = Path(__file__).resolve().parents[2]
    resolved_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    try:
        policy = load_policy(resolved_path, repo_root=repo_root)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Policy file not valid: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return policy, resolved_path


def _memory_policy_result_meta(decision: MemoryDecision) -> dict[str, object]:
    return {
        "policy_action": decision.action,
        "policy_decision": "allowed" if decision.allowed else "denied",
        "policy_reason": decision.reason,
        "confirmation": {
            "required": decision.confirmation_required,
            "provided": decision.confirmation_provided,
            "mode": decision.confirmation_mode,
        },
    }


def _evaluate_memory_policy(policy: PermissionPolicy, action: str, namespace: str) -> MemoryDecision:
    try:
        policy.check_tool_allowed(action)
    except PermissionError:
        return MemoryDecision(
            action=action,
            allowed=False,
            confirmation_required=False,
            confirmation_provided=False,
            confirmation_mode=None,
            reason="policy_denied",
        )
    if not policy.memory.is_allowed(action, namespace):
        return MemoryDecision(
            action=action,
            allowed=False,
            confirmation_required=False,
            confirmation_provided=False,
            confirmation_mode=None,
            reason="policy_denied",
        )
    return MemoryDecision(
        action=action,
        allowed=True,
        confirmation_required=policy.memory.requires_confirmation(action, namespace),
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None,
    )


def _parse_memory_value(value_text: str | None, value_json: str | None) -> object:
    if value_text and value_json:
        print("Provide either --value or --value-text, not both.")
        raise SystemExit(2)
    if value_text is not None:
        return value_text
    if value_json is None:
        print("Provide --value or --value-text for memory put.")
        raise SystemExit(2)
    try:
        return json.loads(value_json)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON for --value: {exc}")
        raise SystemExit(2) from exc


def run_memory_put(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    value = _parse_memory_value(args.value_text, args.value)
    tags = args.tag or []
    action = "memory.put"
    decision = _evaluate_memory_policy(policy, action, args.namespace)
    request = {
        "namespace": args.namespace,
        "key": args.key,
        "kind": args.kind,
        "value_json": json.dumps(value, ensure_ascii=False, sort_keys=True),
        "tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True) if tags else None,
        "confidence": args.confidence,
        "source": args.source,
        "ttl_seconds": args.ttl_seconds,
    }
    if not decision.allowed:
        memory_record_event(
            args.db_path,
            operation="put",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=_memory_policy_result_meta(decision),
        )
        print("Memory put blocked by policy.", file=sys.stderr)
        raise SystemExit(2)
    if decision.confirmation_required:
        if args.yes:
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
        elif args.non_interactive or not _is_interactive_tty():
            denied = MemoryDecision(
                action=decision.action,
                allowed=False,
                confirmation_required=True,
                confirmation_provided=False,
                confirmation_mode=None,
                reason="confirmation_required",
            )
            memory_record_event(
                args.db_path,
                operation="put",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=_memory_policy_result_meta(denied),
            )
            print(
                "Confirmation required for memory put. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                "This memory write requires confirmation. Proceed? [y/N]:"
            )
            if response.strip().lower() not in {"y", "yes"}:
                denied = MemoryDecision(
                    action=decision.action,
                    allowed=False,
                    confirmation_required=True,
                    confirmation_provided=False,
                    confirmation_mode=None,
                    reason="confirmation_declined",
                )
                memory_record_event(
                    args.db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=_memory_policy_result_meta(denied),
                )
                print("Confirmation declined; memory not written.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    item = memory_put_item(
        args.db_path,
        namespace=args.namespace,
        key=args.key,
        kind=args.kind,
        value=value,
        tags=tags,
        confidence=args.confidence,
        source=args.source,
        ttl_seconds=args.ttl_seconds,
        actor=actor,
        policy_hash=policy_hash,
        result_meta_extra=_memory_policy_result_meta(decision),
    )
    print(f"DB: {args.db_path}")
    print("Stored memory item:")
    _print_memory_item_summary(item)


def run_memory_get(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    item = memory_get_item(
        args.db_path,
        args.namespace,
        args.key,
        include_tombstoned=args.include_tombstoned,
        actor=actor,
        policy_hash=policy_hash,
    )
    if item is None:
        print(f"Memory item not found: {args.namespace}/{args.key}")
        raise SystemExit(2)
    if args.json:
        print(json.dumps(_serialize_memory_item(item), ensure_ascii=False, sort_keys=True))
        return
    print(f"DB: {args.db_path}")
    _print_memory_item_summary(item)


def run_memory_search(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    items = memory_search_items(
        args.db_path,
        args.query or "",
        namespace=args.namespace,
        kind=args.kind,
        tag=args.tag,
        source=args.source,
        confidence_min=args.confidence_min,
        include_tombstoned=args.include_tombstoned,
        limit=args.limit,
        actor=actor,
        policy_hash=policy_hash,
    )
    if args.json:
        payload = [_serialize_memory_item(item) for item in items]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"DB: {args.db_path}")
    print(f"Matches: {len(items)}")
    _print_memory_search_results(items)


def run_memory_delete(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    action = "memory.delete"
    decision = _evaluate_memory_policy(policy, action, args.namespace)
    request = {
        "namespace": args.namespace,
        "key": args.key,
    }
    if not decision.allowed:
        memory_record_event(
            args.db_path,
            operation="delete",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=_memory_policy_result_meta(decision),
        )
        print("Memory delete blocked by policy.", file=sys.stderr)
        raise SystemExit(2)
    if decision.confirmation_required:
        if args.yes:
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
        elif args.non_interactive or not _is_interactive_tty():
            denied = MemoryDecision(
                action=decision.action,
                allowed=False,
                confirmation_required=True,
                confirmation_provided=False,
                confirmation_mode=None,
                reason="confirmation_required",
            )
            memory_record_event(
                args.db_path,
                operation="delete",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=_memory_policy_result_meta(denied),
            )
            print(
                "Confirmation required for memory delete. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                "This memory delete requires confirmation. Proceed? [y/N]:"
            )
            if response.strip().lower() not in {"y", "yes"}:
                denied = MemoryDecision(
                    action=decision.action,
                    allowed=False,
                    confirmation_required=True,
                    confirmation_provided=False,
                    confirmation_mode=None,
                    reason="confirmation_declined",
                )
                memory_record_event(
                    args.db_path,
                    operation="delete",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=_memory_policy_result_meta(denied),
                )
                print("Confirmation declined; memory not deleted.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    item = memory_tombstone_item(
        args.db_path,
        args.namespace,
        args.key,
        actor=actor,
        policy_hash=policy_hash,
        result_meta_extra=_memory_policy_result_meta(decision),
    )
    if item is None:
        print(f"Memory item not found: {args.namespace}/{args.key}")
        raise SystemExit(2)
    print(f"DB: {args.db_path}")
    print("Tombstoned memory item:")
    _print_memory_item_summary(item)


def run_export(
    db_path: str,
    *,
    run_id: str | None,
    use_latest: bool,
    export_format: str,
    out_path: str | None,
    redact: bool,
    policy_path: str | None,
) -> None:
    if export_format != "jsonl":
        raise ValueError("Only jsonl export is supported")
    if run_id and use_latest:
        raise ValueError("Provide either --run or --latest, not both")
    if not run_id and not use_latest:
        raise ValueError("Export requires --run or --latest")

    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    load_policy(policy_path, repo_root=repo_root)
    if use_latest:
        export_path = export_latest_run_jsonl(
            state_store,
            out_path=out_path,
            redact=redact,
        )
    else:
        export_path = export_run_jsonl(
            state_store,
            run_id,
            out_path=out_path,
            redact=redact,
        )
    print(f"Exported run audit to {export_path}")


def run_enqueue(
    db_path: str,
    command_text: str,
    *,
    run_id: str | None,
    max_retries: int,
    timeout_seconds: int,
) -> None:
    state_store = StateStore(db_path)
    item = state_store.enqueue_command(
        command_text=command_text,
        run_id=run_id,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
    )
    print(f"Enqueued {item.id} status={item.status.value}")


@dataclass(frozen=True)
class MemoryInjection:
    block: str
    count: int
    bytes: int
    keys: list[dict[str, str]]


def _serialize_memory_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _memory_entries_for_prompt(items: list[MemoryItem]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in items:
        entries.append(
            {
                "namespace": item.namespace,
                "key": item.key,
                "kind": item.kind,
                "confidence": item.confidence,
                "source": item.source,
                "updated_at": item.updated_at,
                "value_json": _serialize_memory_value(item.value),
            }
        )
    return entries


def _build_memory_injection(db_path: str) -> MemoryInjection:
    items = memory_list_prompt_items(db_path, limit=20)
    entries = _memory_entries_for_prompt(items)
    capped_entries: list[dict[str, str]] = []
    total_bytes = 0
    for entry in entries:
        serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entry_bytes = len(serialized.encode("utf-8"))
        if len(capped_entries) >= 20:
            break
        if total_bytes + entry_bytes > 8192:
            break
        capped_entries.append(entry)
        total_bytes += entry_bytes
    keys = [{"namespace": entry["namespace"], "key": entry["key"]} for entry in capped_entries]
    payload_json = json.dumps(capped_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    block = (
        "READ-ONLY MEMORY CONTEXT (do not modify):\n"
        "<<<< MEMORY READ ONLY >>>>\n"
        f"{payload_json}\n"
        "<<<< END MEMORY >>>>"
    )
    return MemoryInjection(
        block=block,
        count=len(capped_entries),
        bytes=total_bytes,
        keys=keys,
    )


def _apply_memory_injection_payload(
    payload: dict[str, object],
    memory_injection: MemoryInjection | None,
) -> None:
    if not memory_injection:
        return
    payload.update(
        {
            "memory_injection_enabled": True,
            "memory_injected_count": memory_injection.count,
            "memory_injected_keys": memory_injection.keys,
            "memory_injected_bytes": memory_injection.bytes,
        }
    )


def _request_llm_plan(
    db_path: str,
    user_text: str,
    *,
    model: str | None,
    host: str | None,
    timeout_s: int | None,
    enqueue: bool,
    dry_run: bool,
    max_actions: int,
    explain: bool,
    debug: bool,
    actor: str,
    memory_injection: MemoryInjection | None = None,
    assessment_policy_path: str | None = None,
) -> tuple[dict, PlanAssessment, StateStore]:
    if not user_text or not user_text.strip():
        raise ValueError(f"{actor} requires a natural language request.")
    config = resolve_ollama_config(url=host, model=model, timeout_s=timeout_s)
    state_store = StateStore(db_path)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        user_text,
        memory_block=memory_injection.block if memory_injection else None,
    )
    print(f"LLM: {config.model} url={config.url} timeout={config.timeout_s}s")
    try:
        raw_response = ollama_chat(
            user_prompt,
            system_prompt,
            model=config.model,
            host=config.url,
            timeout_s=config.timeout_s,
        )
    except OllamaError as exc:
        payload = {
            "model": config.model,
            "host": config.url,
            "timeout_s": config.timeout_s,
            "user_text": user_text,
            "error": _truncate(str(exc), 200),
            "enqueue": enqueue,
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _apply_memory_injection_payload(payload, memory_injection)
        state_store.record_event(
            actor=actor,
            event_type=EVENT_TYPE_ASK_FAILED,
            message="LLM request failed.",
            json_payload=payload,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        if debug:
            raise
        raise SystemExit(1)
    parsed: dict | None = None
    parse_error: str | None = None
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
        extracted = extract_json_object(raw_response)
        if extracted:
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError as exc_extracted:
                parse_error = str(exc_extracted)
        if parsed is None:
            payload = {
                "model": config.model,
                "host": config.url,
                "timeout_s": config.timeout_s,
                "user_text": user_text,
                "plan": None,
                "raw_response": raw_response,
                "parse_error": parse_error,
                "enqueue": enqueue,
                "dry_run": dry_run,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _apply_memory_injection_payload(payload, memory_injection)
            state_store.record_event(
                actor=actor,
                event_type=EVENT_TYPE_LLM_PLAN,
                message="LLM plan parsing failed.",
                json_payload=payload,
            )
            raw_preview = raw_response[:200]
            message = (
                "LLM response was not valid JSON. "
                f"model={config.model} timeout={config.timeout_s}s "
                f"raw_response={raw_preview} "
                "Model violated JSON-only contract; try another model or transport=curl"
            )
            raise ValueError(message) from exc

    if not isinstance(parsed, dict):
        payload = {
            "model": config.model,
            "host": config.url,
            "timeout_s": config.timeout_s,
            "user_text": user_text,
            "plan": None,
            "raw_response": raw_response,
            "parse_error": "Response JSON was not an object.",
            "enqueue": enqueue,
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _apply_memory_injection_payload(payload, memory_injection)
        state_store.record_event(
            actor=actor,
            event_type=EVENT_TYPE_LLM_PLAN,
            message="LLM plan parsing failed.",
            json_payload=payload,
        )
        message = (
            "LLM response was not a JSON object. "
            f"model={config.model} endpoint={config.url} timeout={config.timeout_s}s."
        )
        print(f"ERROR: {message}", file=sys.stderr)
        if debug:
            raise ValueError(message)
        raise SystemExit(1)
    try:
        plan = _normalize_llm_plan(parsed, max_actions=max_actions)
    except ValueError as exc:
        payload = {
            "model": config.model,
            "host": config.url,
            "timeout_s": config.timeout_s,
            "user_text": user_text,
            "plan": None,
            "raw_response": raw_response,
            "parse_error": str(exc),
            "enqueue": enqueue,
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _apply_memory_injection_payload(payload, memory_injection)
        state_store.record_event(
            actor=actor,
            event_type=EVENT_TYPE_LLM_PLAN,
            message="LLM plan parsing failed.",
            json_payload=payload,
        )
        raise
    _print_llm_plan(plan)
    policy = _load_assessment_policy(assessment_policy_path)
    assessment = assess_plan(plan.get("actions", []), policy=policy)
    _print_plan_assessment(assessment, explain=explain)
    payload = {
        "model": config.model,
        "host": config.url,
        "timeout_s": config.timeout_s,
        "user_text": user_text,
        "plan": plan,
        "assessment": assessment.to_dict(),
        "enqueue": enqueue,
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _apply_memory_injection_payload(payload, memory_injection)
    state_store.record_event(
        actor=actor,
        event_type=EVENT_TYPE_LLM_PLAN,
        message="LLM plan generated.",
        json_payload=payload,
    )
    return plan, assessment, state_store


def _enqueue_plan_actions(
    state_store: StateStore,
    plan: dict,
    *,
    run_id: str | None = None,
) -> tuple[list[str], list[str]]:
    enqueued_ids: list[str] = []
    skipped: list[str] = []
    for action in plan.get("actions", []):
        if action.get("type") != "enqueue":
            continue
        command_text = action.get("command") or ""
        if not command_text.strip():
            skipped.append("Skipped enqueue action with empty command.")
            continue
        try:
            parse_command(command_text)
        except ValueError as exc:
            skipped.append(f"Skipped invalid command '{command_text}': {exc}")
            continue
        item = state_store.enqueue_command(
            command_text=command_text,
            run_id=run_id,
            max_retries=int(action.get("retries") or 0),
            timeout_seconds=int(action.get("timeout_seconds") or 30),
        )
        enqueued_ids.append(item.id)
    return enqueued_ids, skipped


def run_ask(
    db_path: str,
    user_text: str,
    *,
    model: str | None,
    host: str | None,
    timeout_s: int | None,
    enqueue: bool,
    dry_run: bool,
    max_actions: int,
    yes: bool,
    explain: bool,
    debug: bool = False,
    use_memory: bool = False,
) -> None:
    memory_injection = _build_memory_injection(db_path) if use_memory else None
    plan, assessment, state_store = _request_llm_plan(
        db_path,
        user_text,
        model=model,
        host=host,
        timeout_s=timeout_s,
        enqueue=enqueue,
        dry_run=dry_run,
        max_actions=max_actions,
        explain=explain,
        debug=debug,
        actor="ask",
        memory_injection=memory_injection,
        assessment_policy_path=None,
    )

    if not enqueue:
        return
    if dry_run:
        print("Dry run: enqueue requested but no items were enqueued.")
        return
    _confirm_assessment(assessment, yes=yes)

    enqueued_ids, skipped = _enqueue_plan_actions(state_store, plan)
    if skipped:
        print("Enqueue notes:")
        for note in skipped:
            print(f"- {note}")
    if enqueued_ids:
        print("Enqueued items:")
        for item_id in enqueued_ids:
            print(f"- {item_id}")
    else:
        print("No items enqueued.")


def _run_daemon_once(db_path: str, policy_path: str | None) -> None:
    run_daemon(
        db_path,
        policy_path,
        sleep_seconds=0.2,
        once=True,
        requeue_stale_seconds=600,
    )


def _drain_queue_items(
    db_path: str,
    policy_path: str | None,
    item_ids: list[str],
    *,
    max_passes: int = 5,
) -> list[QueueStatus]:
    if not item_ids:
        return []
    for _ in range(max_passes):
        state_store = StateStore(db_path)
        items = [state_store.get_queue_item(item_id) for item_id in item_ids]
        pending = [
            item
            for item in items
            if item and item.status in {QueueStatus.QUEUED, QueueStatus.IN_PROGRESS}
        ]
        if not pending:
            break
        now = datetime.now(timezone.utc)
        if all(
            item.status == QueueStatus.QUEUED
            and item.next_attempt_at
            and item.next_attempt_at > now
            for item in pending
        ):
            break
        _run_daemon_once(db_path, policy_path)
    state_store = StateStore(db_path)
    final_items = [state_store.get_queue_item(item_id) for item_id in item_ids]
    return [item.status for item in final_items if item]


def _queue_status_summary(statuses: list[QueueStatus]) -> tuple[str, QueueStatus | None]:
    if not statuses:
        return "empty", None
    if any(status == QueueStatus.FAILED for status in statuses):
        return "failed", QueueStatus.FAILED
    if any(status == QueueStatus.CANCELLED for status in statuses):
        return "failed", QueueStatus.CANCELLED
    if any(status == QueueStatus.IN_PROGRESS for status in statuses):
        return "in_progress", QueueStatus.IN_PROGRESS
    if any(status == QueueStatus.QUEUED for status in statuses):
        return "queued", QueueStatus.QUEUED
    return "succeeded", QueueStatus.SUCCEEDED


def run_agent(
    db_path: str,
    goal_text: str,
    *,
    policy_path: str | None,
    once: bool,
    max_cycles: int,
    yes: bool,
    dry_run: bool,
) -> None:
    if not goal_text or not goal_text.strip():
        raise ValueError("agent requires a goal description.")
    cycles_limit = 1 if once else max(1, max_cycles)
    run_ids: list[str] = []
    final_status = "unknown"
    final_error: str | None = None
    last_assessment: PlanAssessment | None = None
    last_actions_count = 0
    for cycle in range(1, cycles_limit + 1):
        print(f"=== Agent Cycle {cycle} ===")
        plan, assessment, state_store = _request_llm_plan(
            db_path,
            goal_text,
            model=None,
            host=None,
            timeout_s=None,
            enqueue=not dry_run,
            dry_run=dry_run,
            max_actions=10,
            explain=False,
            debug=False,
            actor="agent",
            assessment_policy_path=policy_path,
        )
        actions = plan.get("actions", [])
        last_actions_count = len(actions)
        last_assessment = assessment

        if dry_run:
            final_status = "dry-run"
            break

        _confirm_agent_assessment(assessment, actions, yes=yes)

        run = state_store.create_run(
            label="agent-cycle",
            metadata={"goal": goal_text, "cycle": cycle, "source": "agent"},
        )
        run_ids.append(run.id)

        enqueued_ids, skipped = _enqueue_plan_actions(state_store, plan, run_id=run.id)
        if skipped:
            print("Enqueue notes:")
            for note in skipped:
                print(f"- {note}")
        if enqueued_ids:
            print("Enqueued items:")
            for item_id in enqueued_ids:
                print(f"- {item_id}")
        else:
            final_status = "no-actions"
            final_error = "No enqueue actions were generated."
            break

        statuses = _drain_queue_items(db_path, policy_path, enqueued_ids)
        status_label, _ = _queue_status_summary(statuses)
        if status_label == "succeeded":
            final_status = "succeeded"
            if cycle >= cycles_limit:
                break
            continue
        if status_label == "failed":
            final_status = "failed"
            last_error = None
            for item_id in enqueued_ids:
                item = state_store.get_queue_item(item_id)
                if item and item.last_error:
                    last_error = item.last_error
                    break
            final_error = last_error or "One or more queue items failed."
            if cycle >= cycles_limit:
                break
            continue
        final_status = status_label
        final_error = "Queue items did not complete within the agent loop."
        break

    if last_assessment is None:
        last_assessment = PlanAssessment(
            confidence="low",
            risk_flags=[],
            explanation="No plan was generated.",
            requires_confirmation=True,
        )
    _print_agent_summary(
        goal=goal_text,
        assessment=last_assessment,
        actions_count=last_actions_count,
        run_ids=run_ids,
        final_status=final_status,
        error_reason=final_error,
    )


def run_daemon(
    db_path: str,
    policy_path: str | None,
    *,
    sleep_seconds: float,
    once: bool,
    requeue_stale_seconds: int,
) -> None:
    state_store = StateStore(db_path)
    state_store.requeue_stale_in_progress(older_than_seconds=requeue_stale_seconds)
    run_daemon_loop(
        state_store,
        policy_path=policy_path,
        sleep_seconds=sleep_seconds,
        once=once,
    )


def run_maintain(
    db_path: str,
    *,
    interval_seconds: float,
    stale_minutes: int,
    once: bool,
    dry_run: bool,
) -> None:
    if stale_minutes < 0:
        raise ValueError("stale_minutes must be >= 0")
    if interval_seconds <= 0 and not once:
        raise ValueError("interval_seconds must be > 0")
    state_store = StateStore(db_path)

    def _run_iteration() -> None:
        summary = run_maintenance_iteration(
            state_store,
            stale_minutes=stale_minutes,
            dry_run=dry_run,
        )
        if dry_run:
            if summary.requeued_ids:
                print(
                    "maintain: dry-run would requeue "
                    f"{len(summary.requeued_ids)} stale items (stale_minutes={stale_minutes})"
                )
            else:
                print(f"maintain: dry-run no stale items (stale_minutes={stale_minutes})")
        elif summary.requeued_count:
            print(
                "maintain: requeued "
                f"{summary.requeued_count} stale items (stale_minutes={stale_minutes})"
            )
        else:
            print(f"maintain: no stale items (stale_minutes={stale_minutes})")

    if once:
        _run_iteration()
        return

    try:
        while True:
            _run_iteration()
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("maintain: stopped")


def run_daemon_install_windows_task(
    name: str,
    db_path: str,
    python_exe: str,
    user: str | None,
    force: bool,
    on_startup: bool,
) -> None:
    config = WindowsTaskConfig(
        name=name,
        db_path=db_path,
        python_exe=python_exe,
        user=user,
        force=force,
        on_startup=on_startup,
    )
    install_windows_task(config)


def run_daemon_uninstall_windows_task(name: str, *, yes: bool) -> None:
    if not yes:
        print(f"Dry run: would remove task \"{name}\".")
        print("Re-run with --yes to confirm removal.")
        return
    uninstall_windows_task(name)


def run_daemon_install_windows_startup(
    name: str,
    db_path: str,
    python_exe: str,
    *,
    force: bool,
) -> None:
    launcher_path = install_windows_startup_launcher(
        name=name,
        db_path=db_path,
        python_exe=python_exe,
        force=force,
    )
    print(f"Startup launcher: {launcher_path}")
    python_arg = quote_windows_arg(python_exe)
    print(
        "Remove with: "
        f"{python_arg} -m gismo.cli.main daemon uninstall-windows-startup --name \"{name}\" --yes"
    )


def run_daemon_uninstall_windows_startup(name: str, *, yes: bool) -> None:
    launcher_path = uninstall_windows_startup_launcher(name, yes=yes)
    if yes:
        print(f"Removed startup launcher: {launcher_path}")


def _print_operator_summary(state_store: StateStore, run_id: str) -> None:
    print("=== GISMO Operator Summary ===")
    print(f"Run: {run_id}")
    print("Tasks:")
    for task in state_store.list_tasks(run_id):
        tool_calls = list(state_store.list_tool_calls_for_task(task.id))
        skipped = sum(1 for call in tool_calls if call.status.value == "SKIPPED")
        failure_type = task.failure_type.value if task.failure_type else "NONE"
        print(
            f"- {task.id} {task.title} [{task.status.value}] "
            f"failure_type={failure_type} tool_calls={len(tool_calls)} skipped={skipped}"
        )


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
        file=sys.stderr,
    )


def _handle_demo(args: argparse.Namespace) -> None:
    run_demo(args.db_path, args.policy)


def _handle_demo_graph(args: argparse.Namespace) -> None:
    run_demo_graph(args.db_path, args.policy)


def _handle_run(args: argparse.Namespace) -> None:
    if args.operator_command and args.operator_command[0] == "show":
        if len(args.operator_command) != 2:
            raise ValueError("run show requires a run id")
        run_show(args.db_path, args.operator_command[1])
        return
    run_operator(args.db_path, args.operator_command, args.policy)


def _handle_runs_list(args: argparse.Namespace) -> None:
    run_list(args.db_path, limit=args.limit, newest_first=not args.oldest)


def _handle_runs_show(args: argparse.Namespace) -> None:
    run_show(args.db_path, args.run_id)


def _handle_memory_put(args: argparse.Namespace) -> None:
    run_memory_put(args)


def _handle_memory_get(args: argparse.Namespace) -> None:
    run_memory_get(args)


def _handle_memory_search(args: argparse.Namespace) -> None:
    run_memory_search(args)


def _handle_memory_delete(args: argparse.Namespace) -> None:
    run_memory_delete(args)


def _handle_export(args: argparse.Namespace) -> None:
    run_id = args.run_id
    if getattr(args, "run_id_arg", None):
        if run_id:
            print("Provide either --run or a positional run id, not both.")
            raise SystemExit(2)
        if not _is_valid_run_id_format(args.run_id_arg):
            print(
                f"Invalid run id format: {args.run_id_arg}. "
                "Provide a full run UUID or use --latest."
            )
            raise SystemExit(2)
        run_id = args.run_id_arg
    run_export(
        args.db_path,
        run_id=run_id,
        use_latest=args.latest,
        export_format=args.format,
        out_path=args.out,
        redact=args.redact,
        policy_path=args.policy,
    )


def _handle_enqueue(args: argparse.Namespace) -> None:
    command_text = " ".join(args.operator_command).strip()
    if not command_text:
        raise ValueError("enqueue requires a command string")
    run_enqueue(
        args.db_path,
        command_text,
        run_id=args.run_id,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout_seconds,
    )


def _handle_ask(args: argparse.Namespace) -> None:
    user_text = " ".join(args.text).strip()
    dry_run = True if args.dry_run is None else args.dry_run
    if args.enqueue and args.dry_run is None:
        dry_run = False
    run_ask(
        args.db_path,
        user_text,
        model=args.model,
        host=args.ollama_url,
        timeout_s=args.timeout_s,
        enqueue=args.enqueue,
        dry_run=dry_run,
        max_actions=args.max_actions,
        yes=args.yes,
        explain=args.explain,
        debug=args.debug,
        use_memory=args.use_memory,
    )


def _handle_agent(args: argparse.Namespace) -> None:
    goal_text = " ".join(args.goal).strip()
    max_cycles = args.max_cycles if args.max_cycles is not None else 1
    run_agent(
        args.db_path,
        goal_text,
        policy_path=args.policy,
        once=args.once,
        max_cycles=max_cycles,
        yes=args.yes,
        dry_run=args.dry_run,
    )


def _handle_daemon(args: argparse.Namespace) -> None:
    run_daemon(
        args.db_path,
        args.policy,
        sleep_seconds=args.sleep,
        once=args.once,
        requeue_stale_seconds=args.requeue_stale_seconds,
    )


def _handle_maintain(args: argparse.Namespace) -> None:
    run_maintain(
        args.db_path,
        interval_seconds=args.interval_seconds,
        stale_minutes=args.stale_minutes,
        once=args.once,
        dry_run=args.dry_run,
    )


def _handle_daemon_install_windows_task(args: argparse.Namespace) -> None:
    run_daemon_install_windows_task(
        name=args.name,
        db_path=args.db_path,
        python_exe=args.python,
        user=args.user,
        force=args.force,
        on_startup=args.on_startup,
    )


def _handle_daemon_uninstall_windows_task(args: argparse.Namespace) -> None:
    run_daemon_uninstall_windows_task(args.name, yes=args.yes)


def _handle_daemon_install_windows_startup(args: argparse.Namespace) -> None:
    run_daemon_install_windows_startup(
        name=args.name,
        db_path=args.db_path,
        python_exe=args.python,
        force=args.force,
    )


def _handle_daemon_uninstall_windows_startup(args: argparse.Namespace) -> None:
    run_daemon_uninstall_windows_startup(args.name, yes=args.yes)


def _handle_queue_stats(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    stats = state_store.queue_stats()

    if args.json:
        def _dt(v):
            return v.isoformat() if v else None
        out = {
            "db_path": args.db_path,
            "total": stats["total"],
            "by_status": stats["by_status"],
            "created_at": {
                "oldest": _dt(stats["created_at"]["oldest"]),
                "newest": _dt(stats["created_at"]["newest"]),
            },
            "updated_at": {
                "oldest": _dt(stats["updated_at"]["oldest"]),
                "newest": _dt(stats["updated_at"]["newest"]),
            },
            "attempts": stats["attempts"],
        }
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"Total: {stats['total']}")
    print("By status:")
    for status in QueueStatus:
        print(f"  {status.value:12} {stats['by_status'].get(status.value, 0)}")
    print(
        f"Created: oldest={_fmt_dt(stats['created_at']['oldest'])} "
        f"newest={_fmt_dt(stats['created_at']['newest'])}"
    )
    print(
        f"Updated: oldest={_fmt_dt(stats['updated_at']['oldest'])} "
        f"newest={_fmt_dt(stats['updated_at']['newest'])}"
    )
    print(
        f"Attempts: items_with_attempts={stats['attempts']['items_with_attempts']} "
        f"max_attempt_count={stats['attempts']['max_attempt_count']}"
    )


def _handle_queue_list(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    status = QueueStatus(args.status) if args.status else None
    items = state_store.list_queue_items(
        status=status,
        limit=args.limit,
        newest_first=not args.oldest,
    )

    if args.json:
        out = []
        for it in items:
            out.append(
                {
                    "id": it.id,
                    "run_id": it.run_id,
                    "status": it.status.value,
                    "created_at": it.created_at.isoformat(),
                    "updated_at": it.updated_at.isoformat(),
                    "started_at": it.started_at.isoformat() if it.started_at else None,
                    "finished_at": it.finished_at.isoformat() if it.finished_at else None,
                    "attempt_count": it.attempt_count,
                    "max_attempts": it.max_retries,
                    "max_retries": it.max_retries,
                    "next_attempt_at": it.next_attempt_at.isoformat()
                    if it.next_attempt_at
                    else None,
                    "timeout_seconds": it.timeout_seconds,
                    "cancel_requested": it.cancel_requested,
                    "last_error": it.last_error,
                    "command_text": it.command_text,
                }
            )
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"Items: {len(items)} (limit={args.limit})")
    header = (
        f"{'ID':8}  {'STATUS':12}  {'ATT':7}  {'CREATED':20}  "
        f"{'UPDATED':20}  {'LAST ERROR':30}  COMMAND"
    )
    print(header)
    print("-" * len(header))
    cmd_width = 200 if args.full else 60
    error_width = 80 if args.full else 30
    for it in items:
        att = f"{it.attempt_count}/{it.max_retries}"
        last_error = _summarize_value(it.last_error, error_width)
        cmd = it.command_text if args.full else _truncate(it.command_text, cmd_width)
        print(
            f"{it.id[:8]:8}  {it.status.value:12}  {att:7}  "
            f"{_fmt_dt(it.created_at):20}  {_fmt_dt(it.updated_at):20}  "
            f"{last_error:{error_width}}  {cmd}"
        )


def _handle_queue_show(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)

    matches = state_store.resolve_queue_item_id(args.id)
    if not matches:
        if state_store.get_run(args.id) is not None:
            print(
                "That looks like a RUN id; use `runs show <id>` or `export --run <id>`."
            )
            raise SystemExit(2)
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)

    if len(matches) > 1:
        print(f"Ambiguous id prefix: {args.id}")
        print("Matches:")
        for mid in matches[:10]:
            print(f"  {mid}")
        if len(matches) > 10:
            print(f"  ... ({len(matches) - 10} more)")
        print("Provide a longer prefix.")
        raise SystemExit(2)

    item = state_store.get_queue_item(matches[0])
    if item is None:
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)

    if args.json:
        out = {
            "id": item.id,
            "run_id": item.run_id,
            "status": item.status.value,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_retries,
            "max_retries": item.max_retries,
            "next_attempt_at": item.next_attempt_at.isoformat()
            if item.next_attempt_at
            else None,
            "timeout_seconds": item.timeout_seconds,
            "cancel_requested": item.cancel_requested,
            "last_error": item.last_error,
            "command_text": item.command_text,
        }
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"ID:         {item.id}")
    print(f"Run ID:     {item.run_id or '-'}")
    print(f"Status:     {item.status.value}")
    print(f"Created:    {_fmt_dt(item.created_at)}")
    print(f"Updated:    {_fmt_dt(item.updated_at)}")
    print(f"Started:    {_fmt_dt(item.started_at)}")
    print(f"Finished:   {_fmt_dt(item.finished_at)}")
    print(f"Attempts:   {item.attempt_count}/{item.max_retries}")
    if item.last_error:
        print("Last error:")
        print(item.last_error)
    print("Command:")
    print(item.command_text)


def _handle_queue_purge_failed(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    failed_items = state_store.list_queue_items_by_status(QueueStatus.FAILED)
    if args.yes:
        deleted = state_store.delete_queue_items_by_status(QueueStatus.FAILED)
        print(f"Deleted {deleted} failed queue item(s).")
        return

    print(f"Dry run: would delete {len(failed_items)} failed queue item(s).")
    if not failed_items:
        return
    header = f"{'ID':8}  {'CREATED':20}  {'ATT':7}  {'LAST ERROR':30}  COMMAND"
    print(header)
    print("-" * len(header))
    cmd_width = 80
    for item in failed_items:
        att = f"{item.attempt_count}/{item.max_retries}"
        last_error = _summarize_value(item.last_error, 30)
        cmd = _truncate(item.command_text, cmd_width)
        print(
            f"{item.id[:8]:8}  {_fmt_dt(item.created_at):20}  {att:7}  "
            f"{last_error:30}  {cmd}"
        )


def _handle_ipc_serve(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None) or str(ipc_cli.DEFAULT_DB_PATH)
    ipc_cli.serve_ipc(db_path, token)


def _print_ipc_connection_error() -> None:
    print(
        "IPC server unreachable. Start it with: "
        "python -m gismo.cli.main ipc serve --db .gismo/state.db "
        "or run: python -m gismo.cli.main supervise up --db .gismo/state.db"
    )
    print("Ensure GISMO_IPC_TOKEN matches on server and client.")


def _handle_ipc_enqueue(args: argparse.Namespace) -> None:
    command_text = " ".join(args.operator_command).strip()
    if not command_text:
        raise ValueError("ipc enqueue requires a command string")
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "enqueue",
                {
                    "command": command_text,
                    "run_id": args.run_id,
                    "max_retries": args.max_retries,
                    "timeout_seconds": args.timeout_seconds,
                },
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_enqueue_output(response.data or {}))


def _handle_queue_cancel(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    item = state_store.request_queue_item_cancel(args.id)
    if item is None:
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)
    if item.status == QueueStatus.CANCELLED:
        print(f"Cancelled queue item {item.id}.")
        return
    if item.status == QueueStatus.IN_PROGRESS:
        print(f"Cancel requested for in-progress queue item {item.id}.")
        return
    print(f"Queue item already completed: {item.id} status={item.status.value}.")


def _handle_ipc_queue_cancel(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_cancel",
                {"queue_item_id": args.id},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        elif response.error == "not_found":
            print(f"Queue item not found: {args.id}")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_cancel_output(response.data or {}))


def _handle_ipc_ping(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("ping", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_ping_output(response.data or {}))


def _handle_ipc_queue_stats(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("queue_stats", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_stats_output(response.data or {}))


def _handle_ipc_run_show(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "run_show",
                {"run_id": args.run_id},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        elif response.error == "not_found":
            print(f"Run not found: {args.run_id}")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_run_show_output(response.data or {}))


def _handle_ipc_daemon_status(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_status", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_status_output(response.data or {}))


def _handle_ipc_daemon_pause(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_pause", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_pause_output(response.data or {}))


def _handle_ipc_daemon_resume(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_resume", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_resume_output(response.data or {}))


def _handle_ipc_purge_failed(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_purge_failed",
                {},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_purge_failed_output(response.data or {}))


def _handle_ipc_requeue_stale(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    payload = {"older_than_minutes": args.older_than_minutes, "limit": args.limit}
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_requeue_stale",
                payload,
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_requeue_stale_output(response.data or {}))


def _handle_supervise_up(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None) or str(ipc_cli.DEFAULT_DB_PATH)
    supervise_cli.run_supervise_up(db_path, token)


def _handle_supervise_status(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None)
    supervise_cli.run_supervise_status(token, db_path=db_path)


def _handle_supervise_down(_args: argparse.Namespace) -> None:
    supervise_cli.run_supervise_down()


def _handle_recover(args: argparse.Namespace) -> None:
    try:
        ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    supervise_cli.run_supervise_recover()


def build_parser() -> argparse.ArgumentParser:
    default_db_path = str(Path(".gismo") / "state.db")
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db",
        "--db-path",
        dest="db_path",
        default=default_db_path,
        help="Path to SQLite state database",
    )
    db_parent_optional = argparse.ArgumentParser(add_help=False)
    db_parent_optional.add_argument(
        "--db",
        "--db-path",
        dest="db_path",
        default=argparse.SUPPRESS,
        help="Path to SQLite state database",
    )
    parser = argparse.ArgumentParser(description="GISMO CLI", parents=[db_parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the demo workflow",
        parents=[db_parent_optional],
    )
    demo_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    demo_parser.set_defaults(handler=_handle_demo)

    demo_graph_parser = subparsers.add_parser(
        "demo-graph",
        help="Run the task graph demo",
        parents=[db_parent_optional],
    )
    demo_graph_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    demo_graph_parser.set_defaults(handler=_handle_demo_graph)

    run_parser = subparsers.add_parser(
        "run",
        help="Run an operator command",
        parents=[db_parent_optional],
    )
    run_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    run_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string (echo:, note:, shell:, or graph:)",
    )
    run_parser.set_defaults(handler=_handle_run)

    runs_parser = subparsers.add_parser(
        "runs",
        help="Inspect runs (list, show)",
        parents=[db_parent_optional],
    )
    runs_subparsers = runs_parser.add_subparsers(dest="runs_command", required=True)

    runs_list_parser = runs_subparsers.add_parser(
        "list",
        help="List recent runs",
        parents=[db_parent_optional],
    )
    runs_list_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of runs to list (default: 25)",
    )
    runs_list_parser.add_argument(
        "--oldest",
        action="store_true",
        help="Sort oldest-first (default: newest-first)",
    )
    runs_list_parser.set_defaults(handler=_handle_runs_list)

    runs_show_parser = runs_subparsers.add_parser(
        "show",
        help="Show a run summary",
        parents=[db_parent_optional],
    )
    runs_show_parser.add_argument(
        "run_id",
        help="Run ID to show",
    )
    runs_show_parser.set_defaults(handler=_handle_runs_show)

    export_parser = subparsers.add_parser(
        "export",
        help="Export run audit trail",
        parents=[db_parent_optional],
    )
    export_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    export_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Run ID to export",
    )
    export_parser.add_argument(
        "run_id_arg",
        nargs="?",
        help="Run ID to export (positional alias for --run)",
    )
    export_parser.add_argument(
        "--latest",
        action="store_true",
        help="Export the most recent run",
    )
    export_parser.add_argument(
        "--format",
        default="jsonl",
        help="Export format (jsonl only)",
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="Output file path (defaults to exports/RUN_ID.jsonl next to --db)",
    )
    export_parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact file contents, shell output, and large tool outputs",
    )
    export_parser.set_defaults(handler=_handle_export)

    memory_parser = subparsers.add_parser(
        "memory",
        help="Manage persistent memory items",
        parents=[db_parent_optional],
    )
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)

    memory_put_parser = memory_subparsers.add_parser(
        "put",
        help="Create or update a memory item",
        parents=[db_parent_optional],
    )
    memory_put_parser.add_argument(
        "--namespace",
        required=True,
        help="Memory namespace (e.g., global, project:<name>, run:<id>)",
    )
    memory_put_parser.add_argument(
        "--key",
        required=True,
        help="Memory key",
    )
    memory_put_parser.add_argument(
        "--kind",
        required=True,
        choices=["fact", "preference", "constraint", "procedure", "note", "summary"],
        help="Memory kind",
    )
    memory_put_parser.add_argument(
        "--value",
        help="JSON value to store",
    )
    memory_put_parser.add_argument(
        "--value-text",
        dest="value_text",
        help="Shortcut for string values (stored as JSON string)",
    )
    memory_put_parser.add_argument(
        "--confidence",
        required=True,
        choices=["high", "medium", "low"],
        help="Confidence level",
    )
    memory_put_parser.add_argument(
        "--source",
        required=True,
        choices=["operator", "system", "llm"],
        help="Source actor",
    )
    memory_put_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Tag (repeatable)",
    )
    memory_put_parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Optional TTL in seconds",
    )
    memory_put_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for high-risk memory writes",
    )
    memory_put_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_put_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_put_parser.set_defaults(handler=_handle_memory_put)

    memory_get_parser = memory_subparsers.add_parser(
        "get",
        help="Fetch a memory item by namespace/key",
        parents=[db_parent_optional],
    )
    memory_get_parser.add_argument(
        "--namespace",
        required=True,
        help="Memory namespace",
    )
    memory_get_parser.add_argument("key", help="Memory key")
    memory_get_parser.add_argument(
        "--include-tombstoned",
        action="store_true",
        help="Include tombstoned items",
    )
    memory_get_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_get_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_get_parser.set_defaults(handler=_handle_memory_get)

    memory_search_parser = memory_subparsers.add_parser(
        "search",
        help="Search memory items",
        parents=[db_parent_optional],
    )
    memory_search_parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="Search query (matches key/value)",
    )
    memory_search_parser.add_argument(
        "--namespace",
        default=None,
        help="Filter by namespace",
    )
    memory_search_parser.add_argument(
        "--kind",
        choices=["fact", "preference", "constraint", "procedure", "note", "summary"],
        help="Filter by kind",
    )
    memory_search_parser.add_argument(
        "--tag",
        default=None,
        help="Filter by tag",
    )
    memory_search_parser.add_argument(
        "--source",
        choices=["operator", "system", "llm"],
        help="Filter by source",
    )
    memory_search_parser.add_argument(
        "--confidence-min",
        dest="confidence_min",
        choices=["high", "medium", "low"],
        help="Minimum confidence filter",
    )
    memory_search_parser.add_argument(
        "--include-tombstoned",
        action="store_true",
        help="Include tombstoned items",
    )
    memory_search_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of results",
    )
    memory_search_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_search_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_search_parser.set_defaults(handler=_handle_memory_search)

    memory_delete_parser = memory_subparsers.add_parser(
        "delete",
        help="Tombstone a memory item",
        parents=[db_parent_optional],
    )
    memory_delete_parser.add_argument(
        "--namespace",
        required=True,
        help="Memory namespace",
    )
    memory_delete_parser.add_argument("key", help="Memory key")
    memory_delete_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for high-risk memory deletes",
    )
    memory_delete_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_delete_parser.set_defaults(handler=_handle_memory_delete)

    enqueue_parser = subparsers.add_parser(
        "enqueue",
        help="Enqueue an operator command",
        parents=[db_parent_optional],
    )
    enqueue_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Optional existing run ID to attach tasks to",
    )
    enqueue_parser.add_argument(
        "--retries",
        type=int,
        default=3,
        dest="max_retries",
        help="Maximum retries for this queue item",
    )
    enqueue_parser.add_argument(
        "--max-attempts",
        type=int,
        dest="max_retries",
        help="Alias for --retries (maximum attempts for this queue item)",
    )
    enqueue_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        dest="timeout_seconds",
        help="Timeout in seconds for this queue item (default: 300)",
    )
    enqueue_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string to enqueue",
    )
    enqueue_parser.set_defaults(handler=_handle_enqueue)

    ask_parser = subparsers.add_parser(
        "ask",
        help="Request an LLM plan (local Ollama only)",
        parents=[db_parent_optional],
    )
    ask_parser.add_argument(
        "--model",
        default=None,
        help="Override the local LLM model (default: phi3:mini or GISMO_OLLAMA_MODEL)",
    )
    ask_parser.add_argument(
        "--ollama-url",
        dest="ollama_url",
        default=None,
        help="Override the Ollama URL (default: http://127.0.0.1:11434 or GISMO_OLLAMA_URL)",
    )
    ask_parser.add_argument(
        "--host",
        dest="ollama_url",
        default=None,
        help="Alias for --ollama-url",
    )
    ask_parser.add_argument(
        "--timeout-s",
        type=int,
        dest="timeout_s",
        default=None,
        help="Timeout in seconds for the LLM call (default: 120 or GISMO_OLLAMA_TIMEOUT_S)",
    )
    ask_parser.add_argument(
        "--timeout",
        type=int,
        dest="timeout_s",
        default=None,
        help="Alias for --timeout-s",
    )
    ask_parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Enqueue validated actions for the daemon to execute",
    )
    ask_parser.add_argument(
        "--memory",
        dest="use_memory",
        action="store_true",
        help="Inject eligible memory items into the planner prompt (read-only)",
    )
    ask_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for enqueue actions",
    )
    ask_parser.add_argument(
        "--explain",
        action="store_true",
        help="Print expanded assessment explanation details",
    )
    ask_parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug tracebacks on LLM request errors",
    )
    ask_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Show the plan without enqueueing (default unless --enqueue is set)",
    )
    ask_parser.add_argument(
        "--max-actions",
        type=int,
        default=10,
        help="Maximum number of actions to accept from the LLM (default: 10)",
    )
    ask_parser.add_argument(
        "text",
        nargs="+",
        help="Natural language request for the planner",
    )
    ask_parser.set_defaults(handler=_handle_ask)

    agent_parser = subparsers.add_parser(
        "agent",
        help="Run the leashed agent loop from a goal",
        parents=[db_parent_optional],
    )
    agent_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    agent_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single plan/enqueue/execute cycle and exit",
    )
    agent_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum planning cycles before stopping (default: 1)",
    )
    agent_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for high-risk plans",
    )
    agent_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan and assessment without enqueueing",
    )
    agent_parser.add_argument(
        "goal",
        nargs="+",
        help="Goal statement for the agent loop",
    )
    agent_parser.set_defaults(handler=_handle_agent)

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run the GISMO daemon loop",
        parents=[db_parent_optional],
    )
    daemon_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    daemon_parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="Sleep interval between queue polls",
    )
    daemon_parser.add_argument(
        "--once",
        action="store_true",
        help="Process queued items once and exit when the queue is empty",
    )
    daemon_parser.add_argument(
        "--requeue-stale-seconds",
        type=int,
        default=600,
        help="Requeue IN_PROGRESS items older than this many seconds",
    )
    daemon_parser.set_defaults(handler=_handle_daemon)
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_install_parser = daemon_subparsers.add_parser(
        "install-windows-task",
        help="Install a Windows Task Scheduler entry for the daemon",
        parents=[db_parent_optional],
    )
    daemon_install_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Task Scheduler task name",
    )
    daemon_install_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run the daemon",
    )
    daemon_install_parser.add_argument(
        "--user",
        default=None,
        help="Optional Windows username for the task (defaults to current user)",
    )
    daemon_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite task if it already exists",
    )
    daemon_install_parser.add_argument(
        "--on-startup",
        action="store_true",
        help="Also trigger at system startup (may require Administrator)",
    )
    daemon_install_parser.set_defaults(handler=_handle_daemon_install_windows_task)
    daemon_uninstall_parser = daemon_subparsers.add_parser(
        "uninstall-windows-task",
        help="Remove the Windows Task Scheduler entry for the daemon",
    )
    daemon_uninstall_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Task Scheduler task name",
    )
    daemon_uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal (required to delete the task)",
    )
    daemon_uninstall_parser.set_defaults(handler=_handle_daemon_uninstall_windows_task)
    daemon_install_startup_parser = daemon_subparsers.add_parser(
        "install-windows-startup",
        help="Install a Windows Startup folder entry for the daemon",
        parents=[db_parent_optional],
    )
    daemon_install_startup_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Startup launcher base name",
    )
    daemon_install_startup_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run the daemon",
    )
    daemon_install_startup_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite launcher if it already exists",
    )
    daemon_install_startup_parser.set_defaults(handler=_handle_daemon_install_windows_startup)
    daemon_uninstall_startup_parser = daemon_subparsers.add_parser(
        "uninstall-windows-startup",
        help="Remove the Windows Startup folder entry for the daemon",
    )
    daemon_uninstall_startup_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Startup launcher base name",
    )
    daemon_uninstall_startup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal (required to delete the launcher)",
    )
    daemon_uninstall_startup_parser.set_defaults(handler=_handle_daemon_uninstall_windows_startup)

    maintain_parser = subparsers.add_parser(
        "maintain",
        help="Run the queue maintenance loop",
        parents=[db_parent_optional],
    )
    maintain_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single maintenance iteration and exit",
    )
    maintain_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report stale items without requeueing",
    )
    maintain_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=30.0,
        help="Sleep interval between maintenance iterations",
    )
    maintain_parser.add_argument(
        "--stale-minutes",
        type=int,
        default=10,
        help="Requeue IN_PROGRESS items older than this many minutes (0 = immediate)",
    )
    maintain_parser.set_defaults(handler=_handle_maintain)

    supervise_parser = subparsers.add_parser(
        "supervise",
        aliases=["svc"],
        help="Run IPC + daemon together",
        parents=[db_parent_optional],
    )
    supervise_subparsers = supervise_parser.add_subparsers(
        dest="supervise_command",
        required=True,
    )

    supervise_up_parser = supervise_subparsers.add_parser(
        "up",
        help="Start IPC server and daemon worker",
        parents=[db_parent_optional],
    )
    supervise_up_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    supervise_up_parser.set_defaults(handler=_handle_supervise_up)

    supervise_status_parser = supervise_subparsers.add_parser(
        "status",
        help="Show supervisor status",
        parents=[db_parent_optional],
    )
    supervise_status_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    supervise_status_parser.set_defaults(handler=_handle_supervise_status)

    supervise_down_parser = supervise_subparsers.add_parser(
        "down",
        help="Stop supervisor-managed processes",
        parents=[db_parent_optional],
    )
    supervise_down_parser.set_defaults(handler=_handle_supervise_down)

    up_alias_parser = subparsers.add_parser(
        "up",
        help="Alias for supervise up",
        parents=[db_parent_optional],
    )
    up_alias_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    up_alias_parser.set_defaults(handler=_handle_supervise_up)

    status_alias_parser = subparsers.add_parser(
        "status",
        help="Alias for supervise status",
        parents=[db_parent_optional],
    )
    status_alias_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    status_alias_parser.set_defaults(handler=_handle_supervise_status)

    down_alias_parser = subparsers.add_parser(
        "down",
        help="Alias for supervise down",
        parents=[db_parent_optional],
    )
    down_alias_parser.set_defaults(handler=_handle_supervise_down)

    recover_parser = subparsers.add_parser(
        "recover",
        help="Stop supervised processes and remove stale supervisor state",
        parents=[db_parent_optional],
    )
    recover_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    recover_parser.set_defaults(handler=_handle_recover)

    queue_parser = subparsers.add_parser(
        "queue",
        help="Inspect the queue (stats, list, show)",
        parents=[db_parent_optional],
    )
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command", required=True)

    queue_stats_parser = queue_subparsers.add_parser(
        "stats",
        help="Show queue summary statistics",
        parents=[db_parent_optional],
    )
    queue_stats_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_stats_parser.set_defaults(handler=_handle_queue_stats)

    queue_list_parser = queue_subparsers.add_parser(
        "list",
        help="List queue items",
        parents=[db_parent_optional],
    )
    queue_list_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of items to list (default: 25)",
    )
    queue_list_parser.add_argument(
        "--status",
        choices=[s.value for s in QueueStatus],
        help="Filter by status",
    )
    queue_list_parser.add_argument(
        "--oldest",
        action="store_true",
        help="Sort oldest-first (default: newest-first)",
    )
    queue_list_parser.add_argument(
        "--full",
        action="store_true",
        help="Do not truncate command text",
    )
    queue_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_list_parser.set_defaults(handler=_handle_queue_list)

    queue_show_parser = queue_subparsers.add_parser(
        "show",
        help="Show a single queue item by id",
        parents=[db_parent_optional],
    )
    queue_show_parser.add_argument("id", help="Queue item id")
    queue_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_show_parser.set_defaults(handler=_handle_queue_show)

    queue_purge_failed_parser = queue_subparsers.add_parser(
        "purge-failed",
        help="Delete FAILED queue items",
        parents=[db_parent_optional],
    )
    queue_purge_failed_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion (omit for dry-run)",
    )
    queue_purge_failed_parser.set_defaults(handler=_handle_queue_purge_failed)

    queue_cancel_parser = queue_subparsers.add_parser(
        "cancel",
        help="Request cancellation for a queue item",
        parents=[db_parent_optional],
    )
    queue_cancel_parser.add_argument("id", help="Queue item id")
    queue_cancel_parser.set_defaults(handler=_handle_queue_cancel)

    ipc_parser = subparsers.add_parser(
        "ipc",
        help="Local IPC control plane",
        parents=[db_parent_optional],
    )
    ipc_subparsers = ipc_parser.add_subparsers(dest="ipc_command", required=True)

    ipc_serve_parser = ipc_subparsers.add_parser(
        "serve",
        help="Start the IPC server",
        parents=[db_parent_optional],
    )
    ipc_serve_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_serve_parser.set_defaults(handler=_handle_ipc_serve)

    ipc_enqueue_parser = ipc_subparsers.add_parser(
        "enqueue",
        help="Enqueue an operator command via IPC",
        parents=[db_parent_optional],
    )
    ipc_enqueue_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_enqueue_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Optional existing run ID to attach tasks to",
    )
    ipc_enqueue_parser.add_argument(
        "--retries",
        type=int,
        default=3,
        dest="max_retries",
        help="Maximum retries for this queue item",
    )
    ipc_enqueue_parser.add_argument(
        "--max-attempts",
        type=int,
        dest="max_retries",
        help="Alias for --retries (maximum attempts for this queue item)",
    )
    ipc_enqueue_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        dest="timeout_seconds",
        help="Timeout in seconds for this queue item (default: 300)",
    )
    ipc_enqueue_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string to enqueue",
    )
    ipc_enqueue_parser.set_defaults(handler=_handle_ipc_enqueue)

    ipc_ping_parser = ipc_subparsers.add_parser(
        "ping",
        help="Ping the IPC server",
        parents=[db_parent_optional],
    )
    ipc_ping_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_ping_parser.set_defaults(handler=_handle_ipc_ping)

    ipc_queue_stats_parser = ipc_subparsers.add_parser(
        "queue-stats",
        help="Show queue summary statistics via IPC",
        parents=[db_parent_optional],
    )
    ipc_queue_stats_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_queue_stats_parser.set_defaults(handler=_handle_ipc_queue_stats)

    ipc_daemon_status_parser = ipc_subparsers.add_parser(
        "daemon-status",
        help="Show daemon status via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_status_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_status_parser.set_defaults(handler=_handle_ipc_daemon_status)

    ipc_daemon_pause_parser = ipc_subparsers.add_parser(
        "daemon-pause",
        help="Pause daemon processing via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_pause_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_pause_parser.set_defaults(handler=_handle_ipc_daemon_pause)

    ipc_daemon_resume_parser = ipc_subparsers.add_parser(
        "daemon-resume",
        help="Resume daemon processing via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_resume_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_resume_parser.set_defaults(handler=_handle_ipc_daemon_resume)

    ipc_purge_failed_parser = ipc_subparsers.add_parser(
        "purge-failed",
        help="Delete failed queue items via IPC",
        parents=[db_parent_optional],
    )
    ipc_purge_failed_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_purge_failed_parser.set_defaults(handler=_handle_ipc_purge_failed)

    ipc_requeue_stale_parser = ipc_subparsers.add_parser(
        "requeue-stale",
        help="Requeue stale in-progress items via IPC",
        parents=[db_parent_optional],
    )
    ipc_requeue_stale_parser.add_argument(
        "--older-than-minutes",
        type=int,
        required=True,
        help="Requeue items older than this many minutes",
    )
    ipc_requeue_stale_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of stale items to requeue",
    )
    ipc_requeue_stale_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_requeue_stale_parser.set_defaults(handler=_handle_ipc_requeue_stale)

    ipc_queue_cancel_parser = ipc_subparsers.add_parser(
        "queue-cancel",
        help="Request cancellation for a queue item via IPC",
        parents=[db_parent_optional],
    )
    ipc_queue_cancel_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_queue_cancel_parser.add_argument("id", help="Queue item id")
    ipc_queue_cancel_parser.set_defaults(handler=_handle_ipc_queue_cancel)

    ipc_run_show_parser = ipc_subparsers.add_parser(
        "run-show",
        help="Show a run summary via IPC",
        parents=[db_parent_optional],
    )
    ipc_run_show_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_run_show_parser.add_argument(
        "run_id",
        help="Run ID to show",
    )
    ipc_run_show_parser.set_defaults(handler=_handle_ipc_run_show)

    return parser


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    if _has_shell_prompt_paste(argv):
        print(
            "It looks like you pasted your shell prompt. "
            "Paste only the command starting with `python -m gismo.cli.main ...`."
        )
        raise SystemExit(2)
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("No command provided.")
    handler(args)


def _build_registry(state_store: StateStore, policy: PermissionPolicy) -> ToolRegistry:
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


if __name__ == "__main__":
    main()
