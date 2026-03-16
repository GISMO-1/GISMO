"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import shlex
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from gismo.cli import memory_doctor as memory_doctor_cli
from gismo.cli import memory_explain as memory_explain_cli
from gismo.cli import memory_profile as memory_profile_cli
from gismo.cli import memory_preview as memory_preview_cli
from gismo.cli import memory_snapshot as memory_snapshot_cli
from gismo.cli import memory_summarize as memory_summarize_cli
from gismo.cli import agent_role as agent_role_cli
from gismo.tui import app as tui_app
from gismo.web import server as web_server
from gismo.cli import tts_cli
from gismo.cli import plan as plan_cli
from gismo.cli import agent_session as agent_session_cli
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
from gismo.core.explain import PlanExplain, build_plan_explain
from gismo.core.gating import ConfirmationDecision, confirm_plan_gate
from gismo.core.policy_summary import PolicySummary, summarize_policy
from gismo.core.risk import (
    PlanRisk,
    classify_plan_risk,
    command_implies_write,
    command_is_readonly,
    infer_action_risk,
    infer_tools_from_command,
)
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool
from gismo.core.tool_receipts import ToolReceiptReplayReport, replay_tool_receipts
from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool
from gismo.llm.ollama import OllamaError, ollama_chat, resolve_ollama_config
from gismo.llm.prompts import build_system_prompt, build_user_prompt
from gismo.memory.injection import (
    MEMORY_INJECTION_BYTE_CAP,
    MEMORY_INJECTION_ITEM_CAP,
    MEMORY_INJECTION_TRACE_AUDIT_LIMIT,
    MemoryInjectionTrace,
    build_memory_injection_trace,
    prompt_selection_filters,
    profile_filters_payload,
    profile_selection_filters,
    select_injection_items,
)
from gismo.memory.store import (
    MemoryItem,
    MemoryNamespaceDetail,
    MemoryNamespaceSummary,
    MemoryRetentionDetail,
    MemoryProfile,
    MemorySelectionReason,
    get_item as memory_get_item,
    get_namespace as memory_get_namespace,
    get_profile_by_selector as memory_get_profile_by_selector,
    get_retention_detail as memory_get_retention_detail,
    list_profile_items as memory_list_profile_items,
    list_prompt_items as memory_list_prompt_items,
    list_namespaces as memory_list_namespaces,
    list_retention_rules as memory_list_retention_rules,
    link_selection_traces_to_run as memory_link_selection_traces_to_run,
    plan_retention_for_write as memory_plan_retention_for_write,
    policy_hash_for_path,
    put_item as memory_put_item,
    record_profile_selection_trace as memory_record_profile_selection_trace,
    record_prompt_selection_trace as memory_record_prompt_selection_trace,
    record_retention_decision as memory_record_retention_decision,
    record_event as memory_record_event,
    retire_namespace as memory_retire_namespace,
    search_items as memory_search_items,
    set_retention_rule as memory_set_retention_rule,
    tombstone_item as memory_tombstone_item,
    update_selection_trace_decision as memory_update_selection_trace_decision,
    apply_retention_evictions as memory_apply_retention_evictions,
    clear_retention_rule as memory_clear_retention_rule,
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


def _serialize_memory_namespace_summary(
    namespace: MemoryNamespaceSummary,
) -> dict[str, object]:
    return {
        "namespace": namespace.namespace,
        "item_count": namespace.item_count,
        "tombstone_count": namespace.tombstone_count,
        "last_write_at": namespace.last_write_at,
        "retired": namespace.retired,
        "retired_at": namespace.retired_at,
    }


def _serialize_memory_namespace_detail(
    namespace: MemoryNamespaceDetail,
) -> dict[str, object]:
    payload = _serialize_memory_namespace_summary(namespace)
    payload["retired_reason"] = namespace.retired_reason
    return payload


def _serialize_memory_retention_detail(
    detail: MemoryRetentionDetail,
) -> dict[str, object]:
    return {
        "namespace": detail.namespace,
        "max_items": detail.max_items,
        "ttl_seconds": detail.ttl_seconds,
        "policy_source": detail.policy_source,
        "created_at": detail.created_at,
        "updated_at": detail.updated_at,
        "item_count": detail.item_count,
        "tombstone_count": detail.tombstone_count,
        "last_write_at": detail.last_write_at,
    }


def _print_memory_namespace_list(namespaces: list[MemoryNamespaceSummary]) -> None:
    if not namespaces:
        print("(no namespaces)")
        return
    for namespace in namespaces:
        last_write = namespace.last_write_at or "-"
        retired_flag = "yes" if namespace.retired else "no"
        print(
            f"- {namespace.namespace} items={namespace.item_count} "
            f"tombstones={namespace.tombstone_count} last_write={last_write} "
            f"retired={retired_flag}"
        )


def _print_memory_namespace_detail(namespace: MemoryNamespaceDetail) -> None:
    print(f"Namespace:     {namespace.namespace}")
    print(f"Items:         {namespace.item_count}")
    print(f"Tombstones:    {namespace.tombstone_count}")
    print(f"Last write:    {namespace.last_write_at or '-'}")
    print(f"Retired:       {'yes' if namespace.retired else 'no'}")
    print(f"Retired at:    {namespace.retired_at or '-'}")
    print(f"Retired reason: {namespace.retired_reason or '-'}")


def _print_memory_retention_list(rules: list[MemoryRetentionDetail]) -> None:
    if not rules:
        print("(no retention rules)")
        return
    for rule in rules:
        print(
            f"- {rule.namespace} max_items={rule.max_items} "
            f"ttl_seconds={rule.ttl_seconds} "
            f"items={rule.item_count} tombstones={rule.tombstone_count}"
        )


def _print_memory_retention_detail(detail: MemoryRetentionDetail) -> None:
    print(f"Namespace:    {detail.namespace}")
    print(f"Max items:    {detail.max_items}")
    print(f"TTL seconds:  {detail.ttl_seconds}")
    print(f"Policy source:{detail.policy_source}")
    print(f"Created:      {detail.created_at}")
    print(f"Updated:      {detail.updated_at}")
    print(f"Items:        {detail.item_count}")
    print(f"Tombstones:   {detail.tombstone_count}")
    print(f"Last write:   {detail.last_write_at or '-'}")


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


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _merge_action_risk(action_risk: str, inferred: str) -> str:
    if action_risk not in _RISK_ORDER:
        return inferred
    if inferred not in _RISK_ORDER:
        return action_risk
    return action_risk if _RISK_ORDER[action_risk] >= _RISK_ORDER[inferred] else inferred


def _action_tools_allowed(command_text: str, policy_summary: PolicySummary) -> bool:
    tool_names = infer_tools_from_command(command_text)
    if not tool_names:
        return False
    return all(tool in policy_summary.allowed_tools for tool in tool_names)


def _enforce_inquire_readonly(
    plan: dict,
    *,
    policy_summary: PolicySummary,
    non_interactive: bool,
) -> dict:
    intent = plan.get("intent")
    intent_text = intent.strip().lower() if isinstance(intent, str) else ""
    if intent_text != "inquire":
        return plan
    actions = list(plan.get("actions") or [])
    if not actions:
        return plan
    normalized: list[dict[str, object]] = []
    invalid_actions: list[str] = []
    modified = False
    for action in actions:
        command_text = action.get("command") or ""
        command_str = command_text if isinstance(command_text, str) else str(command_text)
        command_str = command_str.strip()
        lowered = command_str.lower()
        if lowered.startswith("enqueue:"):
            command_str = command_str.split(":", 1)[1].strip()
            modified = True
        if not command_str:
            invalid_actions.append("empty action")
            continue
        if not command_str.lower().startswith("echo:"):
            invalid_actions.append(command_str)
            continue
        if not _action_tools_allowed(command_str, policy_summary):
            invalid_actions.append(command_str)
            continue
        if action.get("type") != "echo":
            modified = True
        normalized.append(
            {
                "type": "echo",
                "command": command_str,
                "timeout_seconds": 0,
                "retries": 0,
                "why": action.get("why") or "answer inquiry without enqueue",
                "risk": infer_action_risk(command_str).lower(),
            }
        )
    if invalid_actions:
        message = "Inquire intent must be echo-only and cannot enqueue actions."
        if non_interactive:
            print(f"ERROR: {message}", file=sys.stderr)
            raise SystemExit(2)
        notes = _coerce_str_list(plan.get("notes"))
        notes.append(message)
        plan["actions"] = normalized
        plan["notes"] = notes
        return plan
    if modified:
        notes = _coerce_str_list(plan.get("notes"))
        notes.append("Normalized inquire intent to non-enqueue echo actions.")
        plan["notes"] = notes
    plan["actions"] = normalized
    return plan


def _is_inquire_intent(plan: dict) -> bool:
    intent = plan.get("intent")
    if not isinstance(intent, str):
        return False
    return intent.strip().lower() == "inquire"


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

MEMORY_SUGGESTION_MAX = 5
MEMORY_SUGGESTION_KINDS = {
    "fact",
    "preference",
    "constraint",
    "procedure",
    "note",
    "summary",
}
MEMORY_SUGGESTION_CONFIDENCE = {"high", "medium", "low"}


def _normalize_memory_suggestions(raw: object, notes: list[str]) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("Ignored memory_suggestions because it was not a list.")
        return []
    suggestions: list[dict[str, str]] = []
    invalid_count = 0
    for index, entry in enumerate(raw):
        if len(suggestions) >= MEMORY_SUGGESTION_MAX:
            notes.append(
                "Truncated memory_suggestions to "
                f"{MEMORY_SUGGESTION_MAX} item(s)."
            )
            break
        if not isinstance(entry, dict):
            invalid_count += 1
            continue
        namespace = entry.get("namespace", "global")
        namespace_text = namespace.strip() if isinstance(namespace, str) else ""
        if not namespace_text:
            invalid_count += 1
            continue
        key = entry.get("key")
        key_text = key.strip() if isinstance(key, str) else ""
        if not key_text:
            invalid_count += 1
            continue
        kind = entry.get("kind")
        kind_text = kind.strip().lower() if isinstance(kind, str) else ""
        if kind_text not in MEMORY_SUGGESTION_KINDS:
            invalid_count += 1
            continue
        value_json = entry.get("value_json")
        if not isinstance(value_json, str):
            invalid_count += 1
            continue
        try:
            json.loads(value_json)
        except json.JSONDecodeError:
            invalid_count += 1
            continue
        confidence = entry.get("confidence")
        confidence_text = confidence.strip().lower() if isinstance(confidence, str) else ""
        if confidence_text not in MEMORY_SUGGESTION_CONFIDENCE:
            invalid_count += 1
            continue
        why = entry.get("why")
        why_text = why.strip() if isinstance(why, str) else ""
        if not why_text:
            invalid_count += 1
            continue
        suggestions.append(
            {
                "namespace": namespace_text,
                "key": key_text,
                "kind": kind_text,
                "value_json": value_json,
                "confidence": confidence_text,
                "why": why_text,
            }
        )
    if invalid_count:
        notes.append(f"Ignored {invalid_count} invalid memory_suggestion(s).")
    return suggestions


def _normalize_llm_plan(plan: dict, max_actions: int) -> dict:
    allowed_fields = {"intent", "assumptions", "actions", "notes", "memory_suggestions"}
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
    allowed_action_types = {"enqueue", "echo"}
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
            if action_type_text not in allowed_action_types:
                coerced_command = _coerce_action_type_to_command(action_type_text)
                if coerced_command:
                    action_type_text = "enqueue"
                    command_text = coerced_command
                    timeout_seconds = 30
                    retries = 0
                    risk_text = "medium"
            inferred_risk = infer_action_risk(command_text).lower() if command_text else "low"
            risk_text = _merge_action_risk(risk_text, inferred_risk)
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
    unknown_types = sorted(
        {
            a["type"]
            for a in actions
            if a["type"] and a["type"] not in allowed_action_types
        }
    )
    if unknown_types:
        notes.append(f"Ignored unsupported action types: {', '.join(unknown_types)}.")
    memory_suggestions = _normalize_memory_suggestions(plan.get("memory_suggestions"), notes)
    return {
        "intent": intent_text,
        "assumptions": assumptions,
        "actions": actions,
        "notes": notes,
        "memory_suggestions": memory_suggestions,
    }


def _memory_put_command_for_suggestion(suggestion: dict[str, str]) -> str:
    value_arg = shlex.quote(suggestion["value_json"])
    namespace_arg = shlex.quote(suggestion["namespace"])
    key_arg = shlex.quote(suggestion["key"])
    kind_arg = shlex.quote(suggestion["kind"])
    confidence_arg = shlex.quote(suggestion["confidence"])
    return (
        "gismo memory put "
        f"--namespace {namespace_arg} "
        f"--key {key_arg} "
        f"--kind {kind_arg} "
        f"--value {value_arg} "
        f"--confidence {confidence_arg} "
        "--source llm"
    )


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
    suggestions = plan.get("memory_suggestions") or []
    print("Suggested memory updates (advisory only):")
    if not suggestions:
        print("  (none)")
    else:
        for index, suggestion in enumerate(suggestions, start=1):
            namespace = suggestion.get("namespace") or "global"
            key = suggestion.get("key") or "-"
            kind = suggestion.get("kind") or "-"
            confidence = suggestion.get("confidence") or "-"
            why = suggestion.get("why") or "-"
            value_json = suggestion.get("value_json") or "-"
            print(
                f"{index}. {namespace}/{key} kind={kind} confidence={confidence}"
            )
            print(f"   why: {why}")
            print(f"   value_json: {value_json}")
            print(f"   apply: {_memory_put_command_for_suggestion(suggestion)}")


def _print_plan_explain(explain: PlanExplain, *, verbose: bool) -> None:
    print("=== Plan Explain ===")
    print(f"Summary: {explain.summary}")
    print(f"Risk level: {explain.risk_level}")
    if explain.risk_flags:
        print(f"Risk flags: {', '.join(explain.risk_flags)}")
    else:
        print("Risk flags: none")
    if explain.rationale:
        print("Rationale:")
        for item in explain.rationale:
            print(f"- {item}")
    else:
        print("Rationale: none")
    print(f"Allowed tools: {explain.allowed_tools_summary}")
    print(f"Memory injection: {explain.memory_injection}")
    suggestions = explain.memory_suggestions
    if suggestions.get("exists"):
        print(
            "Memory suggestions: advisory only "
            f"(count={suggestions.get('count', 0)})"
        )
    else:
        print("Memory suggestions: none")
    if verbose:
        print("Explain details:")
        print(f"- shell allowlist: {explain.shell_allowlist_summary}")
        write_tools = ", ".join(explain.write_permissions) if explain.write_permissions else "none"
        print(f"- write permissions: {write_tools}")
        trace = explain.memory_injection_trace
        if isinstance(trace, dict):
            eligibility = trace.get("eligibility")
            selected_count = None
            if isinstance(eligibility, dict):
                selected_count = eligibility.get("selected_items")
            print(f"- memory injection hash: {trace.get('injection_hash')}")
            if selected_count is not None:
                print(f"- memory selected count: {selected_count}")


def _print_plan_json(
    *,
    plan: dict,
    explain_payload: PlanExplain,
    enqueue: bool,
    dry_run: bool,
) -> None:
    output = {
        "plan": plan,
        "explain": explain_payload.to_dict(),
        "enqueue": enqueue,
        "dry_run": dry_run,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


def _print_agent_json(
    *,
    goal: str,
    risk: PlanRisk,
    plan: dict,
    explain_payload: PlanExplain | None,
    actions_count: int,
    run_ids: list[str],
    final_status: str,
    error_reason: str | None,
) -> None:
    output = {
        "goal": goal,
        "plan": plan,
        "explain": explain_payload.to_dict() if explain_payload else None,
        "risk": risk.to_dict(),
        "actions_count": actions_count,
        "run_ids": run_ids,
        "final_status": final_status,
        "error_reason": error_reason,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


def _print_agent_summary(
    *,
    goal: str,
    risk: PlanRisk,
    actions_count: int,
    run_ids: list[str],
    final_status: str,
    error_reason: str | None,
) -> None:
    print("=== Agent Summary ===")
    print(f"Goal: {goal}")
    print(f"Plan risk: {risk.risk_level}")
    risk_flags = risk.risk_flags
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


def _confirm_plan_gate(
    risk: PlanRisk,
    *,
    yes: bool,
    non_interactive: bool,
    dry_run: bool,
    context: str,
    policy_summary: PolicySummary | None,
) -> ConfirmationDecision:
    return confirm_plan_gate(
        risk,
        yes=yes,
        non_interactive=non_interactive,
        dry_run=dry_run,
        context=context,
        policy_summary=policy_summary,
        is_interactive_tty=_is_interactive_tty,
    )


def _load_prompt_policy_summary(
    policy_path: str | None,
    *,
    default_allowed_tools: set[str] | None = None,
) -> PolicySummary:
    repo_root = Path(__file__).resolve().parents[2]
    resolved_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    allowed = default_allowed_tools or set()
    policy = load_policy(resolved_path, repo_root=repo_root, default_allowed_tools=allowed)
    return summarize_policy(policy)


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


def _tool_receipt_summary(receipts: list) -> dict[str, object]:
    counts = {"total": len(receipts), "success": 0, "error": 0}
    if not receipts:
        return {
            "counts": counts,
            "first_started_at": None,
            "last_finished_at": None,
            "top_tools": [],
        }
    tool_counts: dict[str, int] = {}
    started_times = []
    finished_times = []
    for receipt in receipts:
        if receipt.status.value == "success":
            counts["success"] += 1
        else:
            counts["error"] += 1
        tool_counts[receipt.tool_name] = tool_counts.get(receipt.tool_name, 0) + 1
        started_times.append(receipt.started_at)
        finished_times.append(receipt.finished_at)
    top_tools = sorted(
        tool_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]
    return {
        "counts": counts,
        "first_started_at": min(started_times).isoformat() if started_times else None,
        "last_finished_at": max(finished_times).isoformat() if finished_times else None,
        "top_tools": [{"tool_name": name, "count": count} for name, count in top_tools],
    }


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


@contextmanager
def _open_state_store(db_path: str) -> Iterator[StateStore]:
    state_store = StateStore(db_path)
    try:
        yield state_store
    finally:
        state_store.close()


def run_demo(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    try:
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
    finally:
        state_store.close()


def run_demo_graph(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    try:
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
    finally:
        state_store.close()


def run_operator(db_path: str, command_parts: list[str], policy_path: str | None) -> None:
    command_text = " ".join(command_parts).strip()
    if not command_text:
        raise ValueError("Operator run requires a command string.")

    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    try:
        plan = parse_command(command_text)
        normalized = normalize_command(command_text)
        default_tools = required_tools(plan) if policy_path is None else set()
        default_tools.discard("run_shell")
        policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
        if warn:
            _warn_missing_default_policy()
        policy = load_policy(
            policy_path,
            repo_root=repo_root,
            default_allowed_tools=default_tools,
        )
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
            orchestrator.run_tool(
                run.id,
                task,
                task.input_json["tool"],
                task.input_json["payload"],
            )
        else:
            orchestrator.run_task_graph(run.id)

        _print_operator_summary(state_store, run.id)
    finally:
        state_store.close()


def _serialize_run_show_payload(
    run,
    *,
    status: str,
    start_time: datetime | None,
    end_time: datetime | None,
    counts: dict[str, int],
    tasks: list,
    tool_calls: list,
    tool_receipts: list,
    memory_provenance: object,
) -> dict[str, object]:
    task_payloads = []
    for task in tasks:
        task_payloads.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status.value,
                "error": task.error,
                "output_json": task.output_json,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "failure_type": task.failure_type.value if task.failure_type else None,
                "status_reason": task.status_reason,
            }
        )
    call_payloads = []
    for call in tool_calls:
        call_payloads.append(
            {
                "id": call.id,
                "task_id": call.task_id,
                "tool_name": call.tool_name,
                "status": call.status.value,
                "started_at": call.started_at.isoformat(),
                "finished_at": call.finished_at.isoformat() if call.finished_at else None,
                "output_json": call.output_json,
                "error": call.error,
                "failure_type": call.failure_type.value if call.failure_type else None,
            }
        )
    memory_payload = memory_provenance.to_dict()
    agent_role = None
    agent_session = None
    if isinstance(run.metadata_json, dict):
        agent_role = run.metadata_json.get("agent_role")
        agent_session = run.metadata_json.get("agent_session")
    tool_receipt_summary = _tool_receipt_summary(tool_receipts)
    return {
        "run": {
            "id": run.id,
            "label": run.label,
            "created_at": run.created_at.isoformat(),
        },
        "status": status,
        "started_at": start_time.isoformat() if start_time else None,
        "finished_at": end_time.isoformat() if end_time else None,
        "task_counts": counts,
        "tasks": task_payloads,
        "tool_calls": call_payloads,
        "tool_receipts_summary": tool_receipt_summary,
        "agent_role": agent_role,
        "agent_session": agent_session,
        "memory_provenance": memory_payload,
    }


def _print_memory_provenance(provenance: object) -> None:
    payload = provenance.to_dict()
    injected = payload["injected"]
    suggested = payload["suggested"]
    applied = payload["applied"]
    policy = payload["policy"]

    print("Memory provenance:")
    print("  Injected memory:")
    print(f"    count: {injected.get('count', 0)}")
    namespaces = injected.get("namespaces") or []
    if namespaces:
        print(f"    namespaces: {', '.join(namespaces)}")
    else:
        print("    namespaces: -")
    cap_items = injected.get("cap_items")
    cap_bytes = injected.get("cap_bytes")
    if cap_items is not None or cap_bytes is not None:
        print(f"    caps: items={cap_items or '-'} bytes={cap_bytes or '-'}")
    if injected.get("bytes") is not None:
        print(f"    bytes_used: {injected['bytes']}")
    profile = injected.get("profile") or {}
    if isinstance(profile, dict) and profile.get("profile_id"):
        print(
            "    profile: "
            f"{profile.get('name') or '-'} ({profile.get('profile_id')})"
        )
        if profile.get("include_namespaces") or profile.get("exclude_namespaces"):
            include_ns = ", ".join(profile.get("include_namespaces") or []) or "-"
            exclude_ns = ", ".join(profile.get("exclude_namespaces") or []) or "-"
            print(f"    profile namespaces: include={include_ns} exclude={exclude_ns}")
        if profile.get("include_kinds") or profile.get("exclude_kinds"):
            include_kinds = ", ".join(profile.get("include_kinds") or []) or "-"
            exclude_kinds = ", ".join(profile.get("exclude_kinds") or []) or "-"
            print(f"    profile kinds: include={include_kinds} exclude={exclude_kinds}")
        if profile.get("max_items") is not None:
            print(f"    profile max_items: {profile.get('max_items')}")

    print("  Suggested memory updates:")
    print(f"    count: {suggested.get('count', 0)}")
    items = suggested.get("items") or []
    if not items:
        print("    (none)")
    else:
        for item in items:
            print(
                "    - "
                f"{item.get('namespace')}/{item.get('key')} "
                f"kind={item.get('kind')} "
                f"confidence={item.get('confidence')} "
                f"source={item.get('source')}"
            )
    if suggested.get("truncated"):
        print(f"    +{suggested.get('remaining', 0)} more")

    print("  Apply results:")
    print(
        "    "
        f"applied={applied.get('applied', 0)} "
        f"skipped={applied.get('skipped', 0)} "
        f"denied={applied.get('denied', 0)}"
    )
    applied_items = applied.get("applied_items") or []
    if applied_items:
        print("    applied:")
        for item in applied_items:
            print(f"      - {item.get('namespace')}/{item.get('key')}")
    denied_items = applied.get("denied_items") or []
    if denied_items:
        print("    denied:")
        for item in denied_items:
            reason = item.get("reason")
            suffix = f" reason={reason}" if reason else ""
            print(f"      - {item.get('namespace')}/{item.get('key')}{suffix}")

    print("  Policy/confirmation:")
    print(f"    policy_path: {policy.get('path') or '-'}")
    print(f"    yes: {policy.get('yes')}")
    print(f"    non_interactive: {policy.get('non_interactive')}")
    print(f"    decision_path: {policy.get('decision_path') or '-'}")


def run_show(db_path: str, run_id: str, *, json_output: bool = False) -> None:
    state_store = StateStore(db_path)
    try:
        run = state_store.get_run(run_id)
        if run is None:
            print(f"Run not found: {run_id}")
            raise SystemExit(2)

        tasks = list(state_store.list_tasks(run.id))
        tool_calls = list(state_store.list_tool_calls(run.id))
        tool_receipts = list(state_store.list_tool_receipts(run.id))
        status = _run_status(tasks)
        start_time, end_time = _run_time_bounds(run, tasks, tool_calls)
        counts = _task_status_counts(tasks)
        receipt_summary = _tool_receipt_summary(tool_receipts)
        memory_provenance = state_store.get_memory_provenance(run.id)

        if json_output:
            payload = _serialize_run_show_payload(
                run,
                status=status,
                start_time=start_time,
                end_time=end_time,
                counts=counts,
                tasks=tasks,
                tool_calls=tool_calls,
                tool_receipts=tool_receipts,
                memory_provenance=memory_provenance,
            )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return

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
        receipt_counts = receipt_summary["counts"]
        top_tools = receipt_summary["top_tools"]
        top_tools_label = ", ".join(
            f"{entry['tool_name']}({entry['count']})" for entry in top_tools
        )
        print(
            "Tool Calls: "
            f"{receipt_counts['total']} "
            f"(success={receipt_counts['success']} error={receipt_counts['error']}) "
            f"first={receipt_summary['first_started_at'] or '-'} "
            f"last={receipt_summary['last_finished_at'] or '-'}"
        )
        if top_tools_label:
            print(f"Top Tools:  {top_tools_label}")
        agent_role = None
        agent_session = None
        if isinstance(run.metadata_json, dict):
            agent_role = run.metadata_json.get("agent_role")
            agent_session = run.metadata_json.get("agent_session")
        if isinstance(agent_role, dict):
            role_name = agent_role.get("role_name") or "-"
            role_id = agent_role.get("role_id") or "-"
            profile_id = agent_role.get("memory_profile_id") or "-"
            print(f"Role:       {role_name} ({role_id}) profile={profile_id}")
        if isinstance(agent_session, dict):
            session_id = agent_session.get("session_id") or "-"
            step_count = agent_session.get("step_count")
            max_steps = agent_session.get("max_steps")
            step_label = f"{step_count}/{max_steps}" if step_count is not None else "-"
            print(f"Session:    {session_id} steps={step_label}")
        print("Tasks:")
        if not tasks:
            print("  (no tasks)")
            if memory_provenance.has_data():
                _print_memory_provenance(memory_provenance)
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
        if memory_provenance.has_data():
            _print_memory_provenance(memory_provenance)
    finally:
        state_store.close()


def run_list(db_path: str, limit: int, newest_first: bool) -> None:
    state_store = StateStore(db_path)
    try:
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
    finally:
        state_store.close()


def _serialize_tool_receipt(receipt) -> dict[str, object]:
    return {
        "id": receipt.id,
        "run_id": receipt.run_id,
        "session_id": receipt.session_id,
        "role_id": receipt.role_id,
        "role_name": receipt.role_name,
        "plan_event_id": receipt.plan_event_id,
        "tool_name": receipt.tool_name,
        "tool_kind": receipt.tool_kind,
        "request_payload_json": receipt.request_payload_json,
        "response_payload_json": receipt.response_payload_json,
        "status": receipt.status.value,
        "started_at": receipt.started_at.isoformat(),
        "finished_at": receipt.finished_at.isoformat(),
        "duration_ms": receipt.duration_ms,
        "request_sha256": receipt.request_sha256,
        "response_sha256": receipt.response_sha256,
        "error_type": receipt.error_type,
        "error_message": receipt.error_message,
        "policy_decision_id": receipt.policy_decision_id,
        "policy_snapshot": receipt.policy_snapshot,
    }


def _serialize_replay_report(report: ToolReceiptReplayReport) -> dict[str, object]:
    return {
        "run_id": report.run_id,
        "export_count": report.export_count,
        "db_count": report.db_count,
        "missing_in_export": report.missing_in_export,
        "missing_in_db": report.missing_in_db,
        "hash_mismatches": report.hash_mismatches,
        "ordering_matches": report.ordering_matches,
    }


def run_tools_receipts_list(db_path: str, run_id: str, *, json_output: bool) -> None:
    state_store = StateStore(db_path)
    try:
        receipts = list(state_store.list_tool_receipts(run_id))
        if json_output:
            payload = [_serialize_tool_receipt(receipt) for receipt in receipts]
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        print(f"Run: {run_id}")
        print(f"Tool receipts: {len(receipts)}")
        if not receipts:
            return
        header = (
            f"{'RECEIPT ID':8}  {'TOOL':16}  {'STATUS':7}  {'STARTED':20}  "
            f"{'DURATION_MS':11}  {'ERROR':40}"
        )
        print(header)
        print("-" * len(header))
        for receipt in receipts:
            error_summary = _summarize_value(receipt.error_message, 40)
            print(
                f"{receipt.id[:8]:8}  {receipt.tool_name:16}  {receipt.status.value:7}  "
                f"{_fmt_dt(receipt.started_at):20}  {receipt.duration_ms:11}  {error_summary:40}"
            )
    finally:
        state_store.close()


def run_tools_receipts_show(db_path: str, receipt_id: str, *, json_output: bool) -> None:
    state_store = StateStore(db_path)
    try:
        receipt = state_store.get_tool_receipt(receipt_id)
        if receipt is None:
            print(f"Tool receipt not found: {receipt_id}")
            raise SystemExit(2)
        if json_output:
            payload = _serialize_tool_receipt(receipt)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        print("=== GISMO Tool Receipt ===")
        print(f"Receipt ID: {receipt.id}")
        print(f"Run ID:     {receipt.run_id}")
        print(f"Tool:       {receipt.tool_name} ({receipt.tool_kind})")
        print(f"Status:     {receipt.status.value}")
        print(f"Started:    {_fmt_dt(receipt.started_at)}")
        print(f"Finished:   {_fmt_dt(receipt.finished_at)}")
        print(f"Duration:   {receipt.duration_ms}ms")
        if receipt.session_id:
            print(f"Session:    {receipt.session_id}")
        if receipt.role_name or receipt.role_id:
            print(f"Role:       {receipt.role_name or '-'} ({receipt.role_id or '-'})")
        if receipt.plan_event_id:
            print(f"Plan Event: {receipt.plan_event_id}")
        print(f"Request:    {_summarize_value(receipt.request_payload_json, 200)}")
        print(f"Response:   {_summarize_value(receipt.response_payload_json, 200)}")
        print(f"Req Hash:   {receipt.request_sha256}")
        print(f"Resp Hash:  {receipt.response_sha256}")
        if receipt.error_type or receipt.error_message:
            print(f"Error Type: {receipt.error_type or '-'}")
            print(f"Error Msg:  {_summarize_value(receipt.error_message, 200)}")
        if receipt.policy_snapshot:
            print(f"Policy:     {_summarize_value(receipt.policy_snapshot, 200)}")
    finally:
        state_store.close()


def run_tools_replay(
    db_path: str,
    *,
    run_id: str,
    export_path: str,
    json_output: bool,
) -> None:
    state_store = StateStore(db_path)
    try:
        report = replay_tool_receipts(state_store, run_id=run_id, export_path=export_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(3)
    finally:
        state_store.close()

    exit_code = 0
    if report.missing_in_export or report.missing_in_db or report.hash_mismatches:
        exit_code = 2
    if not report.ordering_matches and report.export_count and report.db_count:
        exit_code = 2

    if json_output:
        payload = _serialize_replay_report(report)
        payload["exit_code"] = exit_code
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        raise SystemExit(exit_code)

    print("=== GISMO Tool Receipt Replay ===")
    print(f"Run ID:             {report.run_id}")
    print(f"Receipts (export):  {report.export_count}")
    print(f"Receipts (db):      {report.db_count}")
    print(f"Ordering matches:   {report.ordering_matches}")
    print(f"Missing in export:  {len(report.missing_in_export)}")
    if report.missing_in_export:
        for receipt_id in report.missing_in_export[:5]:
            print(f"  - {receipt_id}")
        if len(report.missing_in_export) > 5:
            print(f"  +{len(report.missing_in_export) - 5} more")
    print(f"Missing in db:      {len(report.missing_in_db)}")
    if report.missing_in_db:
        for receipt_id in report.missing_in_db[:5]:
            print(f"  - {receipt_id}")
        if len(report.missing_in_db) > 5:
            print(f"  +{len(report.missing_in_db) - 5} more")
    print(f"Hash mismatches:    {len(report.hash_mismatches)}")
    if report.hash_mismatches:
        for mismatch in report.hash_mismatches[:5]:
            print(f"  - {mismatch['id']} field={mismatch['field']}")
        if len(report.hash_mismatches) > 5:
            print(f"  +{len(report.hash_mismatches) - 5} more")
    raise SystemExit(exit_code)

@dataclass
class MemoryDecision:
    action: str
    allowed: bool
    confirmation_required: bool
    confirmation_provided: bool
    confirmation_mode: str | None
    reason: str | None


@dataclass
class MemoryApplyResult:
    applied: int
    skipped: int
    denied: int
    applied_items: list[dict[str, str]]
    exit_code: int | None = None
    policy_path: str | None = None
    decision_path: str | None = None


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


def _retired_namespace_meta(namespace: MemoryNamespaceDetail) -> dict[str, object]:
    return {
        "namespace_retired": True,
        "retired_at": namespace.retired_at,
        "retired_reason": namespace.retired_reason,
    }


def _merge_result_meta(
    policy_meta: dict[str, object],
    extra_meta: dict[str, object],
) -> dict[str, object]:
    merged = dict(policy_meta)
    merged.update(extra_meta)
    return merged


def _memory_write_decision(
    policy: PermissionPolicy,
    db_path: str,
    *,
    namespace: str,
    action: str,
) -> tuple[MemoryDecision, dict[str, object]]:
    decision = _evaluate_memory_policy(policy, action, namespace)
    if not decision.allowed:
        return decision, {}
    namespace_detail = memory_get_namespace(db_path, namespace=namespace)
    if not namespace_detail or not namespace_detail.retired:
        return decision, {}
    override_action = f"{action}.retired"
    override_decision = _evaluate_memory_policy(policy, override_action, namespace)
    retired_meta = _retired_namespace_meta(namespace_detail)
    if not override_decision.allowed:
        return override_decision, retired_meta
    combined = MemoryDecision(
        action=override_action,
        allowed=True,
        confirmation_required=(
            decision.confirmation_required or override_decision.confirmation_required
        ),
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None,
    )
    return combined, retired_meta


def _memory_request_from_suggestion(
    suggestion: dict[str, str],
    *,
    value: object,
    source: str,
) -> dict[str, object]:
    return {
        "namespace": suggestion["namespace"],
        "key": suggestion["key"],
        "kind": suggestion["kind"],
        "value_json": json.dumps(value, ensure_ascii=False, sort_keys=True),
        "tags_json": None,
        "confidence": suggestion["confidence"],
        "source": source,
        "ttl_seconds": None,
    }


def _memory_decision_path(*, yes: bool, non_interactive: bool) -> str:
    interactive = _is_interactive_tty()
    if non_interactive or yes or not interactive:
        return "non-interactive"
    return "interactive"


def _apply_memory_suggestions(
    db_path: str,
    suggestions: list[dict[str, str]],
    *,
    policy_path: str | None,
    yes: bool,
    non_interactive: bool,
    related_event_id: str,
    actor: str,
) -> MemoryApplyResult:
    if not suggestions:
        return MemoryApplyResult(applied=0, skipped=0, denied=0, applied_items=[])
    policy, resolved_policy_path = _load_memory_policy(policy_path)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    decision_path = _memory_decision_path(yes=yes, non_interactive=non_interactive)
    action = "memory.put"
    result = MemoryApplyResult(
        applied=0,
        skipped=0,
        denied=0,
        applied_items=[],
        policy_path=resolved_policy_path,
        decision_path=decision_path,
    )
    candidates: list[dict[str, object]] = []
    retired_meta_by_namespace: dict[str, dict[str, object]] = {}
    retention_now = datetime.now(timezone.utc)
    for suggestion in suggestions:
        value = json.loads(suggestion["value_json"])
        decision, retired_meta = _memory_write_decision(
            policy,
            db_path,
            namespace=suggestion["namespace"],
            action=action,
        )
        if retired_meta:
            retired_meta_by_namespace[suggestion["namespace"]] = retired_meta
        if not decision.allowed:
            memory_record_event(
                db_path,
                operation="put",
                actor=actor,
                policy_hash=policy_hash,
                request=_memory_request_from_suggestion(
                    suggestion,
                    value=value,
                    source="llm",
                ),
                result_meta=_merge_result_meta(
                    _memory_policy_result_meta(decision),
                    retired_meta_by_namespace.get(suggestion["namespace"], {}),
                ),
                related_ask_event_id=related_event_id,
            )
            result.denied += 1
            continue
        retention_plan = memory_plan_retention_for_write(
            db_path,
            namespace=suggestion["namespace"],
            key=suggestion["key"],
            now=retention_now,
        )
        retention_decision = None
        retention_policy_meta = None
        if retention_plan is not None and (retention_plan.evictions or retention_plan.shortfall):
            retention_action = "memory.retention.enforce"
            retention_decision = _evaluate_memory_policy(
                policy,
                retention_action,
                suggestion["namespace"],
            )
            retention_policy_meta = _memory_policy_result_meta(retention_decision)
            if not retention_decision.allowed:
                retention_event_id = memory_record_retention_decision(
                    db_path,
                    plan=retention_plan,
                    namespace=suggestion["namespace"],
                    key=suggestion["key"],
                    actor=actor,
                    policy_hash=policy_hash,
                    policy_meta=retention_policy_meta,
                    related_ask_event_id=related_event_id,
                )
                memory_record_event(
                    db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=_memory_request_from_suggestion(
                        suggestion,
                        value=value,
                        source="llm",
                    ),
                    result_meta=_merge_result_meta(
                        _merge_result_meta(
                            _memory_policy_result_meta(decision),
                            retired_meta_by_namespace.get(suggestion["namespace"], {}),
                        ),
                        {
                            "retention_event_id": retention_event_id,
                            "retention_decision": "denied",
                            "retention_reason": "policy_denied",
                        },
                    ),
                    related_ask_event_id=related_event_id,
                )
                result.denied += 1
                continue
            if retention_plan.shortfall:
                retention_event_id = memory_record_retention_decision(
                    db_path,
                    plan=retention_plan,
                    namespace=suggestion["namespace"],
                    key=suggestion["key"],
                    actor=actor,
                    policy_hash=policy_hash,
                    policy_meta=retention_policy_meta,
                    related_ask_event_id=related_event_id,
                )
                memory_record_event(
                    db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=_memory_request_from_suggestion(
                        suggestion,
                        value=value,
                        source="llm",
                    ),
                    result_meta=_merge_result_meta(
                        _merge_result_meta(
                            _memory_policy_result_meta(decision),
                            retired_meta_by_namespace.get(suggestion["namespace"], {}),
                        ),
                        {
                            "retention_event_id": retention_event_id,
                            "retention_decision": "denied",
                            "retention_reason": "shortfall",
                        },
                    ),
                    related_ask_event_id=related_event_id,
                )
                result.denied += 1
                continue
        candidates.append(
            {
                "suggestion": suggestion,
                "value": value,
                "decision": decision,
                "retention_plan": retention_plan,
                "retention_decision": retention_decision,
                "retention_policy_meta": retention_policy_meta,
            }
        )

    allowed = [candidate for candidate in candidates if candidate["decision"].allowed]
    if not allowed:
        return result

    confirm_needed = [
        candidate
        for candidate in allowed
        if candidate["decision"].confirmation_required
        or (
            candidate["retention_decision"] is not None
            and candidate["retention_decision"].confirmation_required
        )
    ]
    if confirm_needed and not yes:
        if non_interactive or not _is_interactive_tty():
            for candidate in confirm_needed:
                suggestion = candidate["suggestion"]
                value = candidate["value"]
                decision = candidate["decision"]
                retention_plan = candidate["retention_plan"]
                retention_decision = candidate["retention_decision"]
                retention_event_id = None
                if retention_plan is not None and retention_decision is not None:
                    denied_retention = MemoryDecision(
                        action=retention_decision.action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_required",
                    )
                    retention_event_id = memory_record_retention_decision(
                        db_path,
                        plan=retention_plan,
                        namespace=suggestion["namespace"],
                        key=suggestion["key"],
                        actor=actor,
                        policy_hash=policy_hash,
                        policy_meta=_memory_policy_result_meta(denied_retention),
                        related_ask_event_id=related_event_id,
                    )
                if decision.confirmation_required:
                    denied = MemoryDecision(
                        action=action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_required",
                    )
                    result_meta = _merge_result_meta(
                        _memory_policy_result_meta(denied),
                        retired_meta_by_namespace.get(suggestion["namespace"], {}),
                    )
                else:
                    result_meta = _merge_result_meta(
                        _memory_policy_result_meta(decision),
                        retired_meta_by_namespace.get(suggestion["namespace"], {}),
                    )
                if retention_event_id:
                    result_meta = _merge_result_meta(
                        result_meta,
                        {
                            "retention_event_id": retention_event_id,
                            "retention_decision": "denied",
                            "retention_reason": "confirmation_required",
                        },
                    )
                memory_record_event(
                    db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=_memory_request_from_suggestion(
                        suggestion,
                        value=value,
                        source="llm",
                    ),
                    result_meta=result_meta,
                    related_ask_event_id=related_event_id,
                )
                result.denied += 1
            result.skipped += len(allowed) - len(confirm_needed)
            result.exit_code = 2
            return result
        if len(confirm_needed) == len(allowed) and len(confirm_needed) > 1:
            response = input(
                "Apply all memory suggestions (including retention enforcement)? [y/N]:"
            )
            if response.strip().lower() not in {"y", "yes"}:
                for candidate in confirm_needed:
                    suggestion = candidate["suggestion"]
                    value = candidate["value"]
                    decision = candidate["decision"]
                    retention_plan = candidate["retention_plan"]
                    retention_decision = candidate["retention_decision"]
                    retention_event_id = None
                    if retention_plan is not None and retention_decision is not None:
                        denied_retention = MemoryDecision(
                            action=retention_decision.action,
                            allowed=False,
                            confirmation_required=True,
                            confirmation_provided=False,
                            confirmation_mode=None,
                            reason="confirmation_declined",
                        )
                        retention_event_id = memory_record_retention_decision(
                            db_path,
                            plan=retention_plan,
                            namespace=suggestion["namespace"],
                            key=suggestion["key"],
                            actor=actor,
                            policy_hash=policy_hash,
                            policy_meta=_memory_policy_result_meta(denied_retention),
                            related_ask_event_id=related_event_id,
                        )
                    denied = MemoryDecision(
                        action=action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_declined",
                    )
                    result_meta = _merge_result_meta(
                        _memory_policy_result_meta(denied)
                        if decision.confirmation_required
                        else _memory_policy_result_meta(decision),
                        retired_meta_by_namespace.get(suggestion["namespace"], {}),
                    )
                    if retention_event_id:
                        result_meta = _merge_result_meta(
                            result_meta,
                            {
                                "retention_event_id": retention_event_id,
                                "retention_decision": "denied",
                                "retention_reason": "confirmation_declined",
                            },
                        )
                    memory_record_event(
                        db_path,
                        operation="put",
                        actor=actor,
                        policy_hash=policy_hash,
                        request=_memory_request_from_suggestion(
                            suggestion,
                            value=value,
                            source="llm",
                        ),
                        result_meta=result_meta,
                        related_ask_event_id=related_event_id,
                    )
                    result.denied += 1
                return result
            for candidate in confirm_needed:
                decision = candidate["decision"]
                retention_decision = candidate["retention_decision"]
                decision.confirmation_provided = True
                decision.confirmation_mode = "prompt"
                if retention_decision and retention_decision.confirmation_required:
                    retention_decision.confirmation_provided = True
                    retention_decision.confirmation_mode = "prompt"
        else:
            for candidate in confirm_needed:
                suggestion = candidate["suggestion"]
                value = candidate["value"]
                decision = candidate["decision"]
                retention_plan = candidate["retention_plan"]
                retention_decision = candidate["retention_decision"]
                retention_notice = ""
                if retention_plan is not None and retention_plan.evictions:
                    retention_notice = (
                        f" (evicts {len(retention_plan.evictions)} item(s))"
                    )
                response = input(
                    f"Apply memory suggestion {suggestion['namespace']}/{suggestion['key']}"
                    f"{retention_notice}? [y/N]:"
                )
                if response.strip().lower() not in {"y", "yes"}:
                    retention_event_id = None
                    if retention_plan is not None and retention_decision is not None:
                        denied_retention = MemoryDecision(
                            action=retention_decision.action,
                            allowed=False,
                            confirmation_required=True,
                            confirmation_provided=False,
                            confirmation_mode=None,
                            reason="confirmation_declined",
                        )
                        retention_event_id = memory_record_retention_decision(
                            db_path,
                            plan=retention_plan,
                            namespace=suggestion["namespace"],
                            key=suggestion["key"],
                            actor=actor,
                            policy_hash=policy_hash,
                            policy_meta=_memory_policy_result_meta(denied_retention),
                            related_ask_event_id=related_event_id,
                        )
                    denied = MemoryDecision(
                        action=action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_declined",
                    )
                    result_meta = _merge_result_meta(
                        _memory_policy_result_meta(denied)
                        if decision.confirmation_required
                        else _memory_policy_result_meta(decision),
                        retired_meta_by_namespace.get(suggestion["namespace"], {}),
                    )
                    if retention_event_id:
                        result_meta = _merge_result_meta(
                            result_meta,
                            {
                                "retention_event_id": retention_event_id,
                                "retention_decision": "denied",
                                "retention_reason": "confirmation_declined",
                            },
                        )
                    memory_record_event(
                        db_path,
                        operation="put",
                        actor=actor,
                        policy_hash=policy_hash,
                        request=_memory_request_from_suggestion(
                            suggestion,
                            value=value,
                            source="llm",
                        ),
                        result_meta=result_meta,
                        related_ask_event_id=related_event_id,
                    )
                    result.denied += 1
                    decision.allowed = False
                else:
                    decision.confirmation_provided = True
                    decision.confirmation_mode = "prompt"
                    if retention_decision and retention_decision.confirmation_required:
                        retention_decision.confirmation_provided = True
                        retention_decision.confirmation_mode = "prompt"
    elif confirm_needed and yes:
        for candidate in confirm_needed:
            decision = candidate["decision"]
            retention_decision = candidate["retention_decision"]
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
            if retention_decision and retention_decision.confirmation_required:
                retention_decision.confirmation_provided = True
                retention_decision.confirmation_mode = "yes-flag"

    for candidate in allowed:
        suggestion = candidate["suggestion"]
        value = candidate["value"]
        decision = candidate["decision"]
        retention_plan = candidate["retention_plan"]
        retention_decision = candidate["retention_decision"]
        retention_policy_meta = candidate["retention_policy_meta"]
        if not decision.allowed:
            continue
        if decision.confirmation_required and not decision.confirmation_provided:
            result.skipped += 1
            continue
        if retention_decision and retention_decision.confirmation_required:
            if not retention_decision.confirmation_provided:
                result.skipped += 1
                continue
        extra_meta = _merge_result_meta(
            _memory_policy_result_meta(decision),
            retired_meta_by_namespace.get(suggestion["namespace"], {}),
        )
        if retention_plan is not None:
            retention_event_id = memory_record_retention_decision(
                db_path,
                plan=retention_plan,
                namespace=suggestion["namespace"],
                key=suggestion["key"],
                actor=actor,
                policy_hash=policy_hash,
                policy_meta=retention_policy_meta,
                related_ask_event_id=related_event_id,
            )
            if retention_plan.shortfall:
                memory_record_event(
                    db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=_memory_request_from_suggestion(
                        suggestion,
                        value=value,
                        source="llm",
                    ),
                    result_meta=_merge_result_meta(
                        extra_meta,
                        {
                            "retention_event_id": retention_event_id,
                            "retention_decision": "denied",
                            "retention_reason": "shortfall",
                        },
                    ),
                    related_ask_event_id=related_event_id,
                )
                result.denied += 1
                continue
            if retention_plan.evictions:
                memory_apply_retention_evictions(
                    db_path,
                    plan=retention_plan,
                    actor=actor,
                    policy_hash=policy_hash,
                    retention_event_id=retention_event_id,
                    related_ask_event_id=related_event_id,
                )
            extra_meta = _merge_result_meta(
                extra_meta,
                {
                    "retention_event_id": retention_event_id,
                    "retention_evictions": len(retention_plan.evictions),
                },
            )
        item = memory_put_item(
            db_path,
            namespace=suggestion["namespace"],
            key=suggestion["key"],
            kind=suggestion["kind"],
            value=value,
            tags=None,
            confidence=suggestion["confidence"],
            source="llm",
            ttl_seconds=None,
            actor=actor,
            policy_hash=policy_hash,
            result_meta_extra=extra_meta,
            related_ask_event_id=related_event_id,
        )
        result.applied += 1
        result.applied_items.append(
            {"namespace": item.namespace, "key": item.key}
        )
    return result


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
    decision, retired_meta = _memory_write_decision(
        policy,
        args.db_path,
        namespace=args.namespace,
        action=action,
    )
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
            result_meta=_merge_result_meta(
                _memory_policy_result_meta(decision),
                retired_meta,
            ),
        )
        if retired_meta:
            print("Memory put blocked: namespace is retired.", file=sys.stderr)
        else:
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
                result_meta=_merge_result_meta(
                    _memory_policy_result_meta(denied),
                    retired_meta,
                ),
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
                    result_meta=_merge_result_meta(
                        _memory_policy_result_meta(denied),
                        retired_meta,
                    ),
                )
                print("Confirmation declined; memory not written.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    retention_meta: dict[str, object] = {}
    retention_plan = memory_plan_retention_for_write(
        args.db_path,
        namespace=args.namespace,
        key=args.key,
        now=datetime.now(timezone.utc),
    )
    if retention_plan is not None:
        retention_decision = None
        retention_policy_meta = None
        if retention_plan.evictions or retention_plan.shortfall:
            retention_action = "memory.retention.enforce"
            retention_decision = _evaluate_memory_policy(
                policy,
                retention_action,
                args.namespace,
            )
            if not retention_decision.allowed:
                retention_policy_meta = _memory_policy_result_meta(retention_decision)
                retention_event_id = memory_record_retention_decision(
                    args.db_path,
                    plan=retention_plan,
                    namespace=args.namespace,
                    key=args.key,
                    actor=actor,
                    policy_hash=policy_hash,
                    policy_meta=retention_policy_meta,
                )
                memory_record_event(
                    args.db_path,
                    operation="put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=_merge_result_meta(
                        _merge_result_meta(
                            _memory_policy_result_meta(decision),
                            retired_meta,
                        ),
                        {
                            "retention_event_id": retention_event_id,
                            "retention_decision": "denied",
                            "retention_reason": "policy_denied",
                        },
                    ),
                )
                print("Memory put blocked: retention policy denied.", file=sys.stderr)
                raise SystemExit(2)
            if retention_decision.confirmation_required:
                if args.yes:
                    retention_decision.confirmation_provided = True
                    retention_decision.confirmation_mode = "yes-flag"
                elif args.non_interactive or not _is_interactive_tty():
                    denied = MemoryDecision(
                        action=retention_decision.action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_required",
                    )
                    retention_policy_meta = _memory_policy_result_meta(denied)
                    retention_event_id = memory_record_retention_decision(
                        args.db_path,
                        plan=retention_plan,
                        namespace=args.namespace,
                        key=args.key,
                        actor=actor,
                        policy_hash=policy_hash,
                        policy_meta=retention_policy_meta,
                    )
                    memory_record_event(
                        args.db_path,
                        operation="put",
                        actor=actor,
                        policy_hash=policy_hash,
                        request=request,
                        result_meta=_merge_result_meta(
                            _merge_result_meta(
                                _memory_policy_result_meta(decision),
                                retired_meta,
                            ),
                            {
                                "retention_event_id": retention_event_id,
                                "retention_decision": "denied",
                                "retention_reason": "confirmation_required",
                            },
                        ),
                    )
                    print(
                        "Confirmation required for retention enforcement. "
                        "Re-run with --yes to proceed.",
                        file=sys.stderr,
                    )
                    raise SystemExit(2)
                else:
                    response = input(
                        f"Retention would tombstone {len(retention_plan.evictions)} "
                        f"item(s) in {args.namespace}. Proceed? [y/N]:"
                    )
                    if response.strip().lower() not in {"y", "yes"}:
                        denied = MemoryDecision(
                            action=retention_decision.action,
                            allowed=False,
                            confirmation_required=True,
                            confirmation_provided=False,
                            confirmation_mode=None,
                            reason="confirmation_declined",
                        )
                        retention_policy_meta = _memory_policy_result_meta(denied)
                        retention_event_id = memory_record_retention_decision(
                            args.db_path,
                            plan=retention_plan,
                            namespace=args.namespace,
                            key=args.key,
                            actor=actor,
                            policy_hash=policy_hash,
                            policy_meta=retention_policy_meta,
                        )
                        memory_record_event(
                            args.db_path,
                            operation="put",
                            actor=actor,
                            policy_hash=policy_hash,
                            request=request,
                            result_meta=_merge_result_meta(
                                _merge_result_meta(
                                    _memory_policy_result_meta(decision),
                                    retired_meta,
                                ),
                                {
                                    "retention_event_id": retention_event_id,
                                    "retention_decision": "denied",
                                    "retention_reason": "confirmation_declined",
                                },
                            ),
                        )
                        print(
                            "Confirmation declined; retention not applied.",
                            file=sys.stderr,
                        )
                        raise SystemExit(2)
                    retention_decision.confirmation_provided = True
                    retention_decision.confirmation_mode = "prompt"
            retention_policy_meta = _memory_policy_result_meta(retention_decision)
        retention_event_id = memory_record_retention_decision(
            args.db_path,
            plan=retention_plan,
            namespace=args.namespace,
            key=args.key,
            actor=actor,
            policy_hash=policy_hash,
            policy_meta=retention_policy_meta,
        )
        if retention_plan.shortfall:
            memory_record_event(
                args.db_path,
                operation="put",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=_merge_result_meta(
                    _merge_result_meta(
                        _memory_policy_result_meta(decision),
                        retired_meta,
                    ),
                    {
                        "retention_event_id": retention_event_id,
                        "retention_decision": "denied",
                        "retention_reason": "shortfall",
                    },
                ),
            )
            print(
                "Memory put blocked: retention rule cannot be satisfied.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if retention_plan.evictions:
            memory_apply_retention_evictions(
                args.db_path,
                plan=retention_plan,
                actor=actor,
                policy_hash=policy_hash,
                retention_event_id=retention_event_id,
            )
        retention_meta = {
            "retention_event_id": retention_event_id,
            "retention_evictions": len(retention_plan.evictions),
        }
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
        result_meta_extra=_merge_result_meta(
            _memory_policy_result_meta(decision),
            _merge_result_meta(retired_meta, retention_meta),
        ),
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


def run_memory_namespace_list(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    namespaces = memory_list_namespaces(args.db_path)
    memory_record_event(
        args.db_path,
        operation="namespace.list",
        actor=actor,
        policy_hash=policy_hash,
        request={},
        result_meta={"count": len(namespaces)},
    )
    if args.json:
        payload = [_serialize_memory_namespace_summary(entry) for entry in namespaces]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    _print_memory_namespace_list(namespaces)


def run_memory_namespace_show(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    namespace = memory_get_namespace(args.db_path, namespace=args.namespace)
    memory_record_event(
        args.db_path,
        operation="namespace.show",
        actor=actor,
        policy_hash=policy_hash,
        request={"namespace": args.namespace},
        result_meta={"found": namespace is not None},
    )
    if namespace is None:
        print(f"Memory namespace not found: {args.namespace}")
        raise SystemExit(2)
    if args.json:
        print(json.dumps(_serialize_memory_namespace_detail(namespace), ensure_ascii=False, sort_keys=True))
        return
    _print_memory_namespace_detail(namespace)


def run_memory_namespace_retire(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    action = "memory.namespace.retire"
    decision = _evaluate_memory_policy(policy, action, args.namespace)
    request = {"namespace": args.namespace, "reason": args.reason}
    if not decision.allowed:
        result_meta = _merge_result_meta(
            _memory_policy_result_meta(decision),
            {"policy_path": resolved_policy_path, "retire_reason": args.reason},
        )
        memory_record_event(
            args.db_path,
            operation="namespace.retire",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
        )
        print("Namespace retirement blocked by policy.", file=sys.stderr)
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
            result_meta = _merge_result_meta(
                _memory_policy_result_meta(denied),
                {"policy_path": resolved_policy_path, "retire_reason": args.reason},
            )
            memory_record_event(
                args.db_path,
                operation="namespace.retire",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
            )
            print(
                "Confirmation required for namespace retire. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                f"Retire memory namespace {args.namespace}? [y/N]:"
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
                result_meta = _merge_result_meta(
                    _memory_policy_result_meta(denied),
                    {"policy_path": resolved_policy_path, "retire_reason": args.reason},
                )
                memory_record_event(
                    args.db_path,
                    operation="namespace.retire",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=result_meta,
                )
                print("Confirmation declined; namespace not retired.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    namespace, changed = memory_retire_namespace(
        args.db_path,
        namespace=args.namespace,
        reason=args.reason,
    )
    result_meta = _merge_result_meta(
        _memory_policy_result_meta(decision),
        {
            "policy_path": resolved_policy_path,
            "retire_reason": args.reason,
            "retired": namespace.retired,
            "retired_at": namespace.retired_at,
            "changed": changed,
        },
    )
    memory_record_event(
        args.db_path,
        operation="namespace.retire",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )
    if changed:
        print(f"Retired memory namespace: {namespace.namespace}")
    else:
        print(f"Memory namespace already retired: {namespace.namespace}")


def _validate_retention_value(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if value < 1:
        print(f"{label} must be >= 1.", file=sys.stderr)
        raise SystemExit(2)
    return value


def run_memory_retention_list(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    rules = memory_list_retention_rules(args.db_path)
    memory_record_event(
        args.db_path,
        operation="retention.list",
        actor=actor,
        policy_hash=policy_hash,
        request={},
        result_meta={"count": len(rules)},
    )
    if args.json:
        payload = [_serialize_memory_retention_detail(rule) for rule in rules]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    _print_memory_retention_list(rules)


def run_memory_retention_show(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    detail = memory_get_retention_detail(args.db_path, namespace=args.namespace)
    memory_record_event(
        args.db_path,
        operation="retention.show",
        actor=actor,
        policy_hash=policy_hash,
        request={"namespace": args.namespace},
        result_meta={"found": detail is not None},
    )
    if detail is None:
        print(f"Retention rule not found: {args.namespace}")
        raise SystemExit(2)
    if args.json:
        print(json.dumps(_serialize_memory_retention_detail(detail), ensure_ascii=False, sort_keys=True))
        return
    _print_memory_retention_detail(detail)


def run_memory_retention_set(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    max_items = _validate_retention_value(args.max_items, "max-items")
    ttl_seconds = _validate_retention_value(args.ttl_seconds, "ttl-seconds")
    if max_items is None and ttl_seconds is None:
        print("Provide --max-items and/or --ttl-seconds.", file=sys.stderr)
        raise SystemExit(2)
    action = "memory.retention.set"
    decision = _evaluate_memory_policy(policy, action, args.namespace)
    request = {
        "namespace": args.namespace,
        "max_items": max_items,
        "ttl_seconds": ttl_seconds,
        "reason": args.reason,
    }
    if not decision.allowed:
        memory_record_event(
            args.db_path,
            operation="retention.set",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=_memory_policy_result_meta(decision),
        )
        print("Retention set blocked by policy.", file=sys.stderr)
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
                operation="retention.set",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=_memory_policy_result_meta(denied),
            )
            print(
                "Confirmation required for retention set. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                f"Set retention for namespace {args.namespace}? [y/N]:"
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
                    operation="retention.set",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=_memory_policy_result_meta(denied),
                )
                print("Confirmation declined; retention not updated.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    rule, changed = memory_set_retention_rule(
        args.db_path,
        namespace=args.namespace,
        max_items=max_items,
        ttl_seconds=ttl_seconds,
        policy_source="operator",
    )
    result_meta = _merge_result_meta(
        _memory_policy_result_meta(decision),
        {
            "changed": changed,
            "policy_source": rule.policy_source,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        },
    )
    memory_record_event(
        args.db_path,
        operation="retention.set",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )
    status = "Updated" if changed else "Reaffirmed"
    print(f"{status} retention for {rule.namespace}.")


def run_memory_retention_clear(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _memory_policy_hash(resolved_policy_path)
    action = "memory.retention.clear"
    decision = _evaluate_memory_policy(policy, action, args.namespace)
    request = {"namespace": args.namespace}
    if not decision.allowed:
        memory_record_event(
            args.db_path,
            operation="retention.clear",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=_memory_policy_result_meta(decision),
        )
        print("Retention clear blocked by policy.", file=sys.stderr)
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
                operation="retention.clear",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=_memory_policy_result_meta(denied),
            )
            print(
                "Confirmation required for retention clear. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                f"Clear retention for namespace {args.namespace}? [y/N]:"
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
                    operation="retention.clear",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=_memory_policy_result_meta(denied),
                )
                print("Confirmation declined; retention not cleared.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    changed = memory_clear_retention_rule(args.db_path, namespace=args.namespace)
    result_meta = _merge_result_meta(
        _memory_policy_result_meta(decision),
        {"changed": changed},
    )
    memory_record_event(
        args.db_path,
        operation="retention.clear",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )
    if changed:
        print(f"Cleared retention for {args.namespace}.")
    else:
        print(f"No retention rule found for {args.namespace}.")


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
    try:
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
    finally:
        state_store.close()


def run_enqueue(
    db_path: str,
    command_text: str,
    *,
    run_id: str | None,
    max_retries: int,
    timeout_seconds: int,
) -> None:
    state_store = StateStore(db_path)
    try:
        item = state_store.enqueue_command(
            command_text=command_text,
            run_id=run_id,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        print(f"Enqueued {item.id} status={item.status.value}")
    finally:
        state_store.close()


@dataclass(frozen=True)
class MemoryInjection:
    block: str
    count: int
    bytes: int
    keys: list[dict[str, str]]
    cap_items: int
    cap_bytes: int
    trace: MemoryInjectionTrace
    profile: dict[str, object] | None = None


@dataclass(frozen=True)
class AgentRoleContext:
    role_id: str
    role_name: str
    memory_profile_id: str | None


def _resolve_memory_profile(db_path: str, selector: str) -> MemoryProfile:
    profile = memory_get_profile_by_selector(db_path, selector)
    if profile is None:
        print(f"Memory profile not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if profile.retired_at:
        print(f"Memory profile is retired: {profile.name}", file=sys.stderr)
        raise SystemExit(2)
    return profile


def _build_memory_injection(
    db_path: str,
    *,
    source: str,
    profile_selector: str | None = None,
    plan_id: str | None = None,
    run_id: str | None = None,
) -> MemoryInjection:
    profile_filters = None
    selection_filters = None
    trace_profile = None
    if profile_selector:
        profile = _resolve_memory_profile(db_path, profile_selector)
        items = memory_list_profile_items(
            db_path,
            profile=profile,
            limit=MEMORY_INJECTION_ITEM_CAP,
        )
        effective_limit = min(
            MEMORY_INJECTION_ITEM_CAP,
            profile.max_items if profile.max_items is not None else MEMORY_INJECTION_ITEM_CAP,
        )
        profile_filters = profile_filters_payload(profile, effective_limit)
        trace_profile = profile_filters
        selection_filters = profile_selection_filters(profile)
        memory_record_profile_selection_trace(
            db_path,
            profile=profile,
            selected_items=items,
            run_id=run_id,
            plan_id=plan_id,
        )
    else:
        items = memory_list_prompt_items(db_path, limit=MEMORY_INJECTION_ITEM_CAP)
        selection_filters = prompt_selection_filters()
        memory_record_prompt_selection_trace(
            db_path,
            selected_items=items,
            run_id=run_id,
            plan_id=plan_id,
        )
    if selection_filters is None:
        raise RuntimeError("Memory selection filters were not resolved.")
    selection = select_injection_items(
        items,
        cap_items=MEMORY_INJECTION_ITEM_CAP,
        cap_bytes=MEMORY_INJECTION_BYTE_CAP,
    )
    capped_entries = selection.entries
    total_bytes = selection.total_bytes
    excluded_due_to_cap = selection.excluded_items
    if excluded_due_to_cap and (plan_id or run_id):
        include_reason = "include.profile" if profile_selector else "include.default"
        for item in excluded_due_to_cap:
            memory_update_selection_trace_decision(
                db_path,
                run_id=run_id,
                plan_id=plan_id,
                namespace=item.namespace,
                key=item.key,
                kind=item.kind,
                decision="exclude",
                reasons=[
                    MemorySelectionReason(code="exclude.cap"),
                    MemorySelectionReason(code=include_reason),
                ],
            )
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
        cap_items=MEMORY_INJECTION_ITEM_CAP,
        cap_bytes=MEMORY_INJECTION_BYTE_CAP,
        trace=build_memory_injection_trace(
            db_path,
            selected_items=selection.items,
            source=source,
            filters=selection_filters,
            cap_items=MEMORY_INJECTION_ITEM_CAP,
            cap_bytes=MEMORY_INJECTION_BYTE_CAP,
            profile=trace_profile,
        ),
        profile=profile_filters,
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
            "memory_injected_cap_items": memory_injection.cap_items,
            "memory_injected_cap_bytes": memory_injection.cap_bytes,
            "memory_profile": memory_injection.profile,
        }
    )


def _memory_injection_status(memory_injection: MemoryInjection | None) -> str:
    if memory_injection is None:
        return "none"
    if memory_injection.profile:
        return "profile"
    return "memory"


def _apply_agent_role_payload(
    payload: dict[str, object],
    role_context: AgentRoleContext | None,
) -> None:
    if role_context is None:
        return
    payload["agent_role"] = {
        "role_id": role_context.role_id,
        "role_name": role_context.role_name,
        "memory_profile_id": role_context.memory_profile_id,
    }


def _resolve_agent_role(db_path: str, selector: str) -> AgentRoleContext:
    state_store = StateStore(db_path)
    try:
        role = state_store.get_agent_role_by_selector(selector)
    finally:
        state_store.close()
    if role is None:
        print(f"Agent role not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if role.retired_at:
        print(f"Agent role is retired: {role.name}", file=sys.stderr)
        raise SystemExit(2)
    if role.memory_profile_id:
        profile = memory_get_profile_by_selector(db_path, role.memory_profile_id)
        if profile is None:
            print(
                f"Agent role references missing memory profile: {role.memory_profile_id}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if profile.retired_at:
            print(
                f"Agent role memory profile is retired: {profile.name}",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return AgentRoleContext(
        role_id=role.role_id,
        role_name=role.name,
        memory_profile_id=role.memory_profile_id,
    )


def _record_memory_profile_use(
    *,
    db_path: str,
    memory_injection: MemoryInjection | None,
    actor: str,
    related_event_id: str | None,
) -> None:
    if not memory_injection or not memory_injection.profile:
        return
    profile = memory_injection.profile
    request = {
        "profile_id": profile.get("profile_id"),
        "profile_name": profile.get("name"),
        "resolved_filters": profile,
    }
    result_meta = {
        "selected_count": memory_injection.count,
        "selected_keys": memory_injection.keys,
        "cap_items": memory_injection.cap_items,
        "cap_bytes": memory_injection.cap_bytes,
    }
    memory_record_event(
        db_path,
        operation="memory.profile.use",
        actor=actor,
        policy_hash=policy_hash_for_path(None),
        request=request,
        result_meta=result_meta,
        related_ask_event_id=related_event_id,
    )


def _record_memory_injection_trace(
    *,
    db_path: str,
    memory_injection: MemoryInjection | None,
    actor: str,
    related_event_id: str | None,
) -> None:
    if memory_injection is None:
        return
    trace_payload = memory_injection.trace.to_dict(
        max_selected_items=MEMORY_INJECTION_TRACE_AUDIT_LIMIT
    )
    request = {
        "source": memory_injection.trace.source,
        "profile": memory_injection.trace.profile,
    }
    memory_record_event(
        db_path,
        operation="memory.inject",
        actor=actor,
        policy_hash=policy_hash_for_path(None),
        request=request,
        result_meta=trace_payload,
        related_ask_event_id=related_event_id,
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
    non_interactive: bool,
    memory_injection: MemoryInjection | None = None,
    role_context: AgentRoleContext | None = None,
    policy_path: str | None = None,
    json_output: bool = False,
    record_event: bool = True,
) -> tuple[dict, PlanRisk, PlanExplain, PolicySummary, StateStore, dict[str, object]]:
    if not user_text or not user_text.strip():
        raise ValueError(f"{actor} requires a natural language request.")
    config = resolve_ollama_config(url=host, model=model, timeout_s=timeout_s)
    state_store = StateStore(db_path)
    try:
        policy_summary = _load_prompt_policy_summary(policy_path)
        system_prompt = build_system_prompt(
            policy_summary=policy_summary,
            max_actions=max_actions,
        )
        user_prompt = build_user_prompt(
            user_text,
            memory_block=memory_injection.block if memory_injection else None,
        )
        if not json_output:
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
            _apply_agent_role_payload(payload, role_context)
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
                _apply_agent_role_payload(payload, role_context)
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
            _apply_agent_role_payload(payload, role_context)
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
            _apply_agent_role_payload(payload, role_context)
            state_store.record_event(
                actor=actor,
                event_type=EVENT_TYPE_LLM_PLAN,
                message="LLM plan parsing failed.",
                json_payload=payload,
            )
            raise
        plan = _enforce_inquire_readonly(
            plan,
            policy_summary=policy_summary,
            non_interactive=non_interactive,
        )
        risk = classify_plan_risk(plan.get("actions", []))
        trace_payload = memory_injection.trace.to_dict() if memory_injection else None
        explain_payload = build_plan_explain(
            plan=plan,
            risk=risk,
            policy_summary=policy_summary,
            memory_injection=_memory_injection_status(memory_injection),
            memory_injection_trace=trace_payload,
            memory_suggestions_count=len(plan.get("memory_suggestions") or []),
        )
        if not json_output:
            _print_llm_plan(plan)
            _print_plan_explain(explain_payload, verbose=explain)
        payload = {
            "model": config.model,
            "host": config.url,
            "timeout_s": config.timeout_s,
            "user_text": user_text,
            "plan": plan,
            "explain": explain_payload.to_dict(),
            "enqueue": enqueue,
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _apply_memory_injection_payload(payload, memory_injection)
        _apply_agent_role_payload(payload, role_context)
        if record_event:
            state_store.record_event(
                actor=actor,
                event_type=EVENT_TYPE_LLM_PLAN,
                message="LLM plan generated.",
                json_payload=payload,
            )
        return plan, risk, explain_payload, policy_summary, state_store, payload
    except BaseException:
        state_store.close()
        raise


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
    memory_profile: str | None = None,
    apply_memory_suggestions: bool = False,
    non_interactive: bool = False,
    policy_path: str | None = None,
    json_output: bool = False,
    defer: bool = False,
) -> None:
    plan_event_id = str(uuid4())
    memory_injection = None
    if use_memory or memory_profile:
        source = "--memory-profile" if memory_profile else "--memory"
        memory_injection = _build_memory_injection(
            db_path,
            source=source,
            profile_selector=memory_profile,
            plan_id=plan_event_id,
        )
        _record_memory_injection_trace(
            db_path=db_path,
            memory_injection=memory_injection,
            actor="ask",
            related_event_id=plan_event_id,
        )
    plan, risk, explain_payload, policy_summary, state_store, payload = _request_llm_plan(
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
        non_interactive=non_interactive,
        memory_injection=memory_injection,
        policy_path=policy_path,
        json_output=json_output,
        record_event=False,
    )
    try:
        _record_memory_profile_use(
            db_path=db_path,
            memory_injection=memory_injection,
            actor="ask",
            related_event_id=plan_event_id,
        )
        apply_result = MemoryApplyResult(
            applied=0,
            skipped=0,
            denied=0,
            applied_items=[],
        )
        if apply_memory_suggestions:
            suggestions = plan.get("memory_suggestions") or []
            if not suggestions:
                if not json_output:
                    print("No suggestions to apply")
            else:
                apply_result = _apply_memory_suggestions(
                    db_path,
                    suggestions,
                    policy_path=policy_path,
                    yes=yes,
                    non_interactive=non_interactive,
                    related_event_id=plan_event_id,
                    actor="ask",
                )
                payload.update(
                    {
                        "apply_memory_suggestions_requested": True,
                        "apply_memory_suggestions_result": {
                            "applied": apply_result.applied,
                            "skipped": apply_result.skipped,
                            "denied": apply_result.denied,
                        },
                        "apply_memory_suggestions_applied": apply_result.applied_items,
                        "apply_memory_policy_path": apply_result.policy_path,
                        "apply_memory_yes": yes,
                        "apply_memory_non_interactive": non_interactive,
                        "apply_memory_decision_path": apply_result.decision_path,
                    }
                )
                state_store.record_event(
                    actor="ask",
                    event_type=EVENT_TYPE_LLM_PLAN,
                    message="LLM plan generated.",
                    json_payload=payload,
                    event_id=plan_event_id,
                )
                if not json_output:
                    print(
                        "Memory suggestions summary: "
                        f"applied={apply_result.applied} "
                        f"skipped={apply_result.skipped} "
                        f"denied={apply_result.denied}"
                    )
                if apply_result.exit_code is not None:
                    raise SystemExit(apply_result.exit_code)
            if not suggestions:
                payload.update(
                    {
                        "apply_memory_suggestions_requested": True,
                        "apply_memory_suggestions_result": {
                            "applied": 0,
                            "skipped": 0,
                            "denied": 0,
                        },
                        "apply_memory_suggestions_applied": [],
                        "apply_memory_policy_path": None,
                        "apply_memory_yes": yes,
                        "apply_memory_non_interactive": non_interactive,
                        "apply_memory_decision_path": _memory_decision_path(
                            yes=yes,
                            non_interactive=non_interactive,
                        ),
                    }
                )
                state_store.record_event(
                    actor="ask",
                    event_type=EVENT_TYPE_LLM_PLAN,
                    message="LLM plan generated.",
                    json_payload=payload,
                    event_id=plan_event_id,
                )
                if json_output:
                    _print_plan_json(
                        plan=plan,
                        explain_payload=explain_payload,
                        enqueue=enqueue,
                        dry_run=dry_run,
                    )
                return

        if not apply_memory_suggestions:
            payload.update(
                {
                    "apply_memory_suggestions_requested": False,
                    "apply_memory_suggestions_result": {
                        "applied": 0,
                        "skipped": 0,
                        "denied": 0,
                    },
                    "apply_memory_suggestions_applied": [],
                    "apply_memory_policy_path": None,
                    "apply_memory_yes": yes,
                    "apply_memory_non_interactive": non_interactive,
                    "apply_memory_decision_path": _memory_decision_path(
                        yes=yes,
                        non_interactive=non_interactive,
                    ),
                }
            )
            state_store.record_event(
                actor="ask",
                event_type=EVENT_TYPE_LLM_PLAN,
                message="LLM plan generated.",
                json_payload=payload,
                event_id=plan_event_id,
            )
        # ── defer: save plan for later operator approval ──────────────────
        if defer and not _is_inquire_intent(plan):
            pending = state_store.create_pending_plan(
                intent=plan.get("intent", ""),
                plan_json=plan,
                risk_level=risk.risk_level,
                risk_json={
                    "risk_level": risk.risk_level,
                    "risk_flags": list(risk.risk_flags),
                    "rationale": list(risk.rationale),
                },
                explain_json=explain_payload.to_dict(),
                user_text=user_text,
                actor="ask",
            )
            if json_output:
                print(json.dumps({"deferred": True, "plan_id": pending.id}, ensure_ascii=False))
            else:
                print(f"Plan saved for approval: {pending.id}")
                print(f"  Risk: {risk.risk_level}")
                print(f"  Intent: {plan.get('intent', '')}")
                print(f"  Actions: {len(plan.get('actions', []))}")
                print(f"  Review with: gismo plan show {pending.id[:8]}")
                print(f"  Approve with: gismo plan approve {pending.id[:8]}")
            return

        if _is_inquire_intent(plan):
            if json_output:
                _print_plan_json(
                    plan=plan,
                    explain_payload=explain_payload,
                    enqueue=False,
                    dry_run=dry_run,
                )
            return
        if json_output and not enqueue:
            _print_plan_json(
                plan=plan,
                explain_payload=explain_payload,
                enqueue=enqueue,
                dry_run=dry_run,
            )
            return

        if not enqueue:
            return
        if dry_run:
            if not json_output:
                print("Dry run: enqueue requested but no items were enqueued.")
            _confirm_plan_gate(
                risk,
                yes=yes,
                non_interactive=non_interactive,
                dry_run=True,
                context="ask",
                policy_summary=policy_summary,
            )
            if json_output:
                _print_plan_json(
                    plan=plan,
                    explain_payload=explain_payload,
                    enqueue=enqueue,
                    dry_run=dry_run,
                )
            return
        _confirm_plan_gate(
            risk,
            yes=yes,
            non_interactive=non_interactive,
            dry_run=False,
            context="ask",
            policy_summary=policy_summary,
        )

        enqueued_ids, skipped = _enqueue_plan_actions(state_store, plan)
        if skipped:
            if not json_output:
                print("Enqueue notes:")
                for note in skipped:
                    print(f"- {note}")
        if enqueued_ids:
            if not json_output:
                print("Enqueued items:")
                for item_id in enqueued_ids:
                    print(f"- {item_id}")
        else:
            if not json_output:
                print("No items enqueued.")
        if json_output:
            _print_plan_json(
                plan=plan,
                explain_payload=explain_payload,
                enqueue=enqueue,
                dry_run=dry_run,
            )
    finally:
        state_store.close()


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
        with _open_state_store(db_path) as state_store:
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
    with _open_state_store(db_path) as state_store:
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
    use_memory: bool = False,
    memory_profile: str | None = None,
    apply_memory_suggestions: bool = False,
    non_interactive: bool = False,
    role: str | None = None,
    json_output: bool = False,
) -> None:
    if not goal_text or not goal_text.strip():
        raise ValueError("agent requires a goal description.")
    if role and (use_memory or memory_profile):
        print("ERROR: --role cannot be combined with --memory or --memory-profile.", file=sys.stderr)
        raise SystemExit(2)
    cycles_limit = 1 if once else max(1, max_cycles)
    run_ids: list[str] = []
    final_status = "unknown"
    final_error: str | None = None
    last_risk: PlanRisk | None = None
    last_explain: PlanExplain | None = None
    last_plan: dict | None = None
    last_actions_count = 0
    role_context = _resolve_agent_role(db_path, role) if role else None
    role_profile_selector = role_context.memory_profile_id if role_context else None
    for cycle in range(1, cycles_limit + 1):
        if not json_output:
            print(f"=== Agent Cycle {cycle} ===")
        plan_event_id = str(uuid4())
        memory_injection = None
        if use_memory or memory_profile or role_profile_selector:
            if role_profile_selector and role_context:
                source = f"role:{role_context.role_name}"
            elif memory_profile:
                source = "--memory-profile"
            else:
                source = "--memory"
            memory_injection = _build_memory_injection(
                db_path,
                source=source,
                profile_selector=role_profile_selector or memory_profile,
                plan_id=plan_event_id,
            )
            _record_memory_injection_trace(
                db_path=db_path,
                memory_injection=memory_injection,
                actor="agent",
                related_event_id=plan_event_id,
            )
        plan, risk, explain_payload, policy_summary, state_store, _payload = _request_llm_plan(
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
            non_interactive=non_interactive,
            policy_path=policy_path,
            json_output=json_output,
            memory_injection=memory_injection,
            role_context=role_context,
            record_event=False,
        )
        try:
            payload = _payload
            apply_result = MemoryApplyResult(
                applied=0,
                skipped=0,
                denied=0,
                applied_items=[],
            )
            _record_memory_profile_use(
                db_path=db_path,
                memory_injection=memory_injection,
                actor="agent",
                related_event_id=plan_event_id,
            )
            event_recorded = False
            if apply_memory_suggestions:
                suggestions = plan.get("memory_suggestions") or []
                if not suggestions:
                    if not json_output:
                        print("No suggestions to apply")
                    payload.update(
                        {
                            "apply_memory_suggestions_requested": True,
                            "apply_memory_suggestions_result": {
                                "applied": 0,
                                "skipped": 0,
                                "denied": 0,
                            },
                            "apply_memory_suggestions_applied": [],
                            "apply_memory_policy_path": None,
                            "apply_memory_yes": yes,
                            "apply_memory_non_interactive": non_interactive,
                            "apply_memory_decision_path": _memory_decision_path(
                                yes=yes,
                                non_interactive=non_interactive,
                            ),
                        }
                    )
                    state_store.record_event(
                        actor="agent",
                        event_type=EVENT_TYPE_LLM_PLAN,
                        message="LLM plan generated.",
                        json_payload=payload,
                        event_id=plan_event_id,
                    )
                    event_recorded = True
                else:
                    apply_result = _apply_memory_suggestions(
                        db_path,
                        suggestions,
                        policy_path=policy_path,
                        yes=yes,
                        non_interactive=non_interactive,
                        related_event_id=plan_event_id,
                        actor="agent",
                    )
                    payload.update(
                        {
                            "apply_memory_suggestions_requested": True,
                            "apply_memory_suggestions_result": {
                                "applied": apply_result.applied,
                                "skipped": apply_result.skipped,
                                "denied": apply_result.denied,
                            },
                            "apply_memory_suggestions_applied": apply_result.applied_items,
                            "apply_memory_policy_path": apply_result.policy_path,
                            "apply_memory_yes": yes,
                            "apply_memory_non_interactive": non_interactive,
                            "apply_memory_decision_path": apply_result.decision_path,
                        }
                    )
                    state_store.record_event(
                        actor="agent",
                        event_type=EVENT_TYPE_LLM_PLAN,
                        message="LLM plan generated.",
                        json_payload=payload,
                        event_id=plan_event_id,
                    )
                    event_recorded = True
                    if not json_output:
                        print(
                            "Memory suggestions summary: "
                            f"applied={apply_result.applied} "
                            f"skipped={apply_result.skipped} "
                            f"denied={apply_result.denied}"
                        )
                    if apply_result.exit_code is not None:
                        raise SystemExit(apply_result.exit_code)

            if not event_recorded:
                payload.update(
                    {
                        "apply_memory_suggestions_requested": False,
                        "apply_memory_suggestions_result": {
                            "applied": 0,
                            "skipped": 0,
                            "denied": 0,
                        },
                        "apply_memory_suggestions_applied": [],
                        "apply_memory_policy_path": None,
                        "apply_memory_yes": yes,
                        "apply_memory_non_interactive": non_interactive,
                        "apply_memory_decision_path": _memory_decision_path(
                            yes=yes,
                            non_interactive=non_interactive,
                        ),
                    }
                )
                state_store.record_event(
                    actor="agent",
                    event_type=EVENT_TYPE_LLM_PLAN,
                    message="LLM plan generated.",
                    json_payload=payload,
                    event_id=plan_event_id,
                )

            actions = plan.get("actions", [])
            last_actions_count = len(actions)
            last_risk = risk
            last_explain = explain_payload
            last_plan = plan

            if dry_run:
                _confirm_plan_gate(
                    risk,
                    yes=yes,
                    non_interactive=non_interactive,
                    dry_run=True,
                    context="agent",
                    policy_summary=policy_summary,
                )
                final_status = "dry-run"
                break

            _confirm_plan_gate(
                risk,
                yes=yes,
                non_interactive=non_interactive,
                dry_run=False,
                context="agent",
                policy_summary=policy_summary,
            )

            run_metadata = {
                "goal": goal_text,
                "cycle": cycle,
                "source": "agent",
                "plan_event_id": plan_event_id,
            }
            if role_context:
                _apply_agent_role_payload(run_metadata, role_context)
            run = state_store.create_run(
                label="agent-cycle",
                metadata=run_metadata,
            )
            run_ids.append(run.id)
            memory_link_selection_traces_to_run(
                db_path,
                plan_id=plan_event_id,
                run_id=run.id,
            )

            enqueued_ids, skipped = _enqueue_plan_actions(state_store, plan, run_id=run.id)
            if skipped:
                if not json_output:
                    print("Enqueue notes:")
                    for note in skipped:
                        print(f"- {note}")
            if enqueued_ids:
                if not json_output:
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
        finally:
            state_store.close()

    if last_risk is None:
        last_risk = PlanRisk(
            risk_level="HIGH",
            risk_flags=[],
            rationale=["No plan was generated."],
        )
    if json_output:
        _print_agent_json(
            goal=goal_text,
            risk=last_risk,
            plan=last_plan or {},
            explain_payload=last_explain,
            actions_count=last_actions_count,
            run_ids=run_ids,
            final_status=final_status,
            error_reason=final_error,
        )
        return
    _print_agent_summary(
        goal=goal_text,
        risk=last_risk,
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
    with _open_state_store(db_path) as state_store:
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
    with _open_state_store(db_path) as state_store:
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


def _snapshot_dependencies() -> memory_snapshot_cli.SnapshotDependencies:
    return memory_snapshot_cli.SnapshotDependencies(
        load_memory_policy=_load_memory_policy,
        memory_policy_hash=_memory_policy_hash,
        memory_decision_path=_memory_decision_path,
        evaluate_memory_policy=_evaluate_memory_policy,
        memory_policy_result_meta=_memory_policy_result_meta,
        is_interactive_tty=_is_interactive_tty,
        memory_decision_cls=MemoryDecision,
    )


def _agent_session_dependencies() -> agent_session_cli.AgentSessionDependencies:
    return agent_session_cli.AgentSessionDependencies(
        request_llm_plan=_request_llm_plan,
        build_memory_injection=_build_memory_injection,
        record_memory_profile_use=_record_memory_profile_use,
        record_memory_injection_trace=_record_memory_injection_trace,
        confirm_plan_gate=_confirm_plan_gate,
        enqueue_plan_actions=_enqueue_plan_actions,
        drain_queue_items=_drain_queue_items,
        queue_status_summary=_queue_status_summary,
        apply_agent_role_payload=_apply_agent_role_payload,
        link_selection_traces_to_run=memory_link_selection_traces_to_run,
        memory_decision_path=_memory_decision_path,
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
    run_show(args.db_path, args.run_id, json_output=args.json)


def _handle_tools_receipts_list(args: argparse.Namespace) -> None:
    run_tools_receipts_list(args.db_path, args.run_id, json_output=args.json)


def _handle_tools_receipts_show(args: argparse.Namespace) -> None:
    run_tools_receipts_show(args.db_path, args.receipt_id, json_output=args.json)


def _handle_tools_replay(args: argparse.Namespace) -> None:
    run_tools_replay(
        args.db_path,
        run_id=args.run_id,
        export_path=args.from_export,
        json_output=args.json,
    )


def _handle_memory_put(args: argparse.Namespace) -> None:
    run_memory_put(args)


def _handle_memory_get(args: argparse.Namespace) -> None:
    run_memory_get(args)


def _handle_memory_search(args: argparse.Namespace) -> None:
    run_memory_search(args)


def _handle_memory_delete(args: argparse.Namespace) -> None:
    run_memory_delete(args)


def _handle_memory_preview(args: argparse.Namespace) -> None:
    memory_preview_cli.run_memory_preview(args)


def _handle_memory_doctor_check(args: argparse.Namespace) -> None:
    memory_doctor_cli.run_memory_doctor_check(args)


def _handle_memory_doctor_repair(args: argparse.Namespace) -> None:
    memory_doctor_cli.run_memory_doctor_repair(args)


def _handle_memory_snapshot_export(args: argparse.Namespace) -> None:
    memory_snapshot_cli.run_memory_snapshot_export(args, _snapshot_dependencies())


def _handle_memory_snapshot_diff(args: argparse.Namespace) -> None:
    memory_snapshot_cli.run_memory_snapshot_diff(args, _snapshot_dependencies())


def _handle_memory_snapshot_import(args: argparse.Namespace) -> None:
    memory_snapshot_cli.run_memory_snapshot_import(args, _snapshot_dependencies())


def _handle_memory_summarize_run(args: argparse.Namespace) -> None:
    memory_summarize_cli.run_memory_summarize_run(args)


def _handle_memory_namespace_list(args: argparse.Namespace) -> None:
    run_memory_namespace_list(args)


def _handle_memory_namespace_show(args: argparse.Namespace) -> None:
    run_memory_namespace_show(args)


def _handle_memory_namespace_retire(args: argparse.Namespace) -> None:
    run_memory_namespace_retire(args)


def _handle_memory_profile_list(args: argparse.Namespace) -> None:
    memory_profile_cli.run_memory_profile_list(args)


def _handle_memory_profile_show(args: argparse.Namespace) -> None:
    memory_profile_cli.run_memory_profile_show(args)


def _handle_memory_profile_create(args: argparse.Namespace) -> None:
    memory_profile_cli.run_memory_profile_create(args)


def _handle_memory_profile_retire(args: argparse.Namespace) -> None:
    memory_profile_cli.run_memory_profile_retire(args)


def _handle_agent_role_list(args: argparse.Namespace) -> None:
    agent_role_cli.run_agent_role_list(args)


def _handle_agent_role_show(args: argparse.Namespace) -> None:
    agent_role_cli.run_agent_role_show(args)


def _handle_agent_role_create(args: argparse.Namespace) -> None:
    agent_role_cli.run_agent_role_create(args)


def _handle_agent_role_retire(args: argparse.Namespace) -> None:
    agent_role_cli.run_agent_role_retire(args)


def _handle_agent_session_start(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_start(args)


def _handle_agent_session_show(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_show(args)


def _handle_agent_session_list(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_list(args)


def _handle_agent_session_pause(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_pause(args)


def _handle_agent_session_resume(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_resume(args, _agent_session_dependencies())


def _handle_agent_session_cancel(args: argparse.Namespace) -> None:
    agent_session_cli.run_agent_session_cancel(args)


def _handle_memory_explain(args: argparse.Namespace) -> None:
    memory_explain_cli.run_memory_explain(args)


def _handle_memory_retention_list(args: argparse.Namespace) -> None:
    run_memory_retention_list(args)


def _handle_memory_retention_show(args: argparse.Namespace) -> None:
    run_memory_retention_show(args)


def _handle_memory_retention_set(args: argparse.Namespace) -> None:
    run_memory_retention_set(args)


def _handle_memory_retention_clear(args: argparse.Namespace) -> None:
    run_memory_retention_clear(args)


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
    defer = getattr(args, "defer", False)
    dry_run = True if args.dry_run is None else args.dry_run
    if args.enqueue and args.dry_run is None:
        dry_run = False
    if defer and args.enqueue:
        print("--defer and --enqueue are mutually exclusive.", file=sys.stderr)
        raise SystemExit(2)
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
        memory_profile=args.memory_profile,
        apply_memory_suggestions=args.apply_memory_suggestions,
        non_interactive=args.non_interactive,
        policy_path=args.policy,
        json_output=args.json,
        defer=defer,
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
        use_memory=args.use_memory,
        memory_profile=args.memory_profile,
        apply_memory_suggestions=args.apply_memory_suggestions,
        non_interactive=args.non_interactive,
        role=args.role,
        json_output=args.json,
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
    with _open_state_store(args.db_path) as state_store:
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
    with _open_state_store(args.db_path) as state_store:
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
    with _open_state_store(args.db_path) as state_store:
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
    with _open_state_store(args.db_path) as state_store:
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
    with _open_state_store(args.db_path) as state_store:
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


def _handle_tui(args: argparse.Namespace) -> None:
    tui_app.run(db_path=args.db_path)


def _handle_app(args: argparse.Namespace) -> None:
    from gismo.desktop.app import launch
    launch(db_path=args.db_path)


def _handle_web(args: argparse.Namespace) -> None:
    web_server.run(
        db_path=args.db_path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def _handle_tts_speak(args: argparse.Namespace) -> None:
    tts_cli.handle_speak(args)


def _handle_tts_voices_list(args: argparse.Namespace) -> None:
    tts_cli.handle_voices_list(args)


def _handle_tts_voices_set(args: argparse.Namespace) -> None:
    tts_cli.handle_voices_set(args)


def _handle_tts_voices_download(args: argparse.Namespace) -> None:
    tts_cli.handle_voices_download(args)


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
    runs_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON",
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

    tools_parser = subparsers.add_parser(
        "tools",
        help="Inspect tool receipts or replay exports",
        parents=[db_parent_optional],
    )
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command", required=True)

    tools_receipts_parser = tools_subparsers.add_parser(
        "receipts",
        help="Inspect tool receipts",
        parents=[db_parent_optional],
    )
    tools_receipts_subparsers = tools_receipts_parser.add_subparsers(
        dest="tools_receipts_command",
        required=True,
    )
    tools_receipts_list_parser = tools_receipts_subparsers.add_parser(
        "list",
        help="List tool receipts for a run",
        parents=[db_parent_optional],
    )
    tools_receipts_list_parser.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="Run ID to list receipts for",
    )
    tools_receipts_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    tools_receipts_list_parser.set_defaults(handler=_handle_tools_receipts_list)

    tools_receipts_show_parser = tools_receipts_subparsers.add_parser(
        "show",
        help="Show a single tool receipt",
        parents=[db_parent_optional],
    )
    tools_receipts_show_parser.add_argument(
        "receipt_id",
        help="Tool receipt ID",
    )
    tools_receipts_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    tools_receipts_show_parser.set_defaults(handler=_handle_tools_receipts_show)

    tools_replay_parser = tools_subparsers.add_parser(
        "replay",
        help="Validate tool receipts against a JSONL export",
        parents=[db_parent_optional],
    )
    tools_replay_parser.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="Run ID to validate",
    )
    tools_replay_parser.add_argument(
        "--from-export",
        dest="from_export",
        required=True,
        help="JSONL export path containing tool_receipt records",
    )
    tools_replay_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without executing tool calls (default)",
    )
    tools_replay_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    tools_replay_parser.set_defaults(handler=_handle_tools_replay)

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

    memory_preview_parser = memory_subparsers.add_parser(
        "preview",
        help="Preview memory injection for a profile",
        parents=[db_parent_optional],
    )
    memory_preview_parser.add_argument(
        "--memory-profile",
        required=True,
        help="Memory profile name or ID",
    )
    memory_preview_parser.add_argument(
        "--namespace",
        action="append",
        help="Restrict preview to namespace(s); supports * prefix matching",
    )
    memory_preview_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_preview_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path",
    )
    memory_preview_parser.set_defaults(handler=_handle_memory_preview)

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

    memory_namespace_parser = memory_subparsers.add_parser(
        "namespace",
        help="Manage memory namespaces",
        parents=[db_parent_optional],
    )
    memory_namespace_subparsers = memory_namespace_parser.add_subparsers(
        dest="memory_namespace_command",
        required=True,
    )
    memory_namespace_list_parser = memory_namespace_subparsers.add_parser(
        "list",
        help="List memory namespaces",
        parents=[db_parent_optional],
    )
    memory_namespace_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_namespace_list_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_namespace_list_parser.set_defaults(handler=_handle_memory_namespace_list)

    memory_namespace_show_parser = memory_namespace_subparsers.add_parser(
        "show",
        help="Show memory namespace details",
        parents=[db_parent_optional],
    )
    memory_namespace_show_parser.add_argument(
        "namespace",
        help="Memory namespace to inspect",
    )
    memory_namespace_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_namespace_show_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_namespace_show_parser.set_defaults(handler=_handle_memory_namespace_show)

    memory_namespace_retire_parser = memory_namespace_subparsers.add_parser(
        "retire",
        help="Retire a memory namespace (metadata only)",
        parents=[db_parent_optional],
    )
    memory_namespace_retire_parser.add_argument(
        "namespace",
        help="Memory namespace to retire",
    )
    memory_namespace_retire_parser.add_argument(
        "--reason",
        required=True,
        help="Reason for retiring the namespace",
    )
    memory_namespace_retire_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for namespace retirement",
    )
    memory_namespace_retire_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_namespace_retire_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_namespace_retire_parser.set_defaults(handler=_handle_memory_namespace_retire)

    memory_profile_parser = memory_subparsers.add_parser(
        "profile",
        help="Manage memory profiles (read-only selection)",
    )
    memory_profile_subparsers = memory_profile_parser.add_subparsers(
        dest="memory_profile_command",
        required=True,
    )
    memory_profile_list_parser = memory_profile_subparsers.add_parser(
        "list",
        help="List memory profiles",
        parents=[db_parent_optional],
    )
    memory_profile_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_profile_list_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_profile_list_parser.set_defaults(handler=_handle_memory_profile_list)

    memory_profile_show_parser = memory_profile_subparsers.add_parser(
        "show",
        help="Show memory profile details",
        parents=[db_parent_optional],
    )
    memory_profile_show_parser.add_argument(
        "selector",
        help="Memory profile name or id",
    )
    memory_profile_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_profile_show_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_profile_show_parser.set_defaults(handler=_handle_memory_profile_show)

    memory_profile_create_parser = memory_profile_subparsers.add_parser(
        "create",
        help="Create a memory profile (requires confirmation)",
        parents=[db_parent_optional],
    )
    memory_profile_create_parser.add_argument(
        "--name",
        required=True,
        help="Profile name (unique)",
    )
    memory_profile_create_parser.add_argument(
        "--description",
        default=None,
        help="Optional profile description",
    )
    memory_profile_create_parser.add_argument(
        "--include-namespace",
        action="append",
        default=[],
        help="Namespace to include (repeatable, comma-separated supported)",
    )
    memory_profile_create_parser.add_argument(
        "--exclude-namespace",
        action="append",
        default=[],
        help="Namespace to exclude (repeatable, comma-separated supported)",
    )
    memory_profile_create_parser.add_argument(
        "--include-kind",
        action="append",
        default=[],
        help="Memory kind to include (repeatable, comma-separated supported)",
    )
    memory_profile_create_parser.add_argument(
        "--exclude-kind",
        action="append",
        default=[],
        help="Memory kind to exclude (repeatable, comma-separated supported)",
    )
    memory_profile_create_parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Maximum number of items after filtering",
    )
    memory_profile_create_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts",
    )
    memory_profile_create_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_profile_create_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_profile_create_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    memory_profile_create_parser.set_defaults(handler=_handle_memory_profile_create)

    memory_profile_retire_parser = memory_profile_subparsers.add_parser(
        "retire",
        help="Retire a memory profile (requires confirmation)",
        parents=[db_parent_optional],
    )
    memory_profile_retire_parser.add_argument(
        "selector",
        help="Memory profile name or id",
    )
    memory_profile_retire_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts",
    )
    memory_profile_retire_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_profile_retire_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_profile_retire_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    memory_profile_retire_parser.set_defaults(handler=_handle_memory_profile_retire)

    memory_explain_parser = memory_subparsers.add_parser(
        "explain",
        help="Explain memory selection decisions for a run or plan",
        parents=[db_parent_optional],
    )
    memory_explain_parser.add_argument(
        "--run",
        help="Run ID to explain memory selection for",
    )
    memory_explain_parser.add_argument(
        "--plan",
        help="Plan event ID to explain memory selection for",
    )
    memory_explain_parser.add_argument(
        "--limit",
        type=int,
        default=memory_explain_cli.DEFAULT_EXPLAIN_LIMIT,
        help="Maximum number of trace entries to display",
    )
    memory_explain_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit selection traces as JSON",
    )
    memory_explain_parser.set_defaults(handler=_handle_memory_explain)

    memory_retention_parser = memory_subparsers.add_parser(
        "retention",
        help="Manage memory retention rules",
        parents=[db_parent_optional],
    )
    memory_retention_subparsers = memory_retention_parser.add_subparsers(
        dest="memory_retention_command",
        required=True,
    )
    memory_retention_list_parser = memory_retention_subparsers.add_parser(
        "list",
        help="List memory retention rules",
        parents=[db_parent_optional],
    )
    memory_retention_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_retention_list_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_retention_list_parser.set_defaults(handler=_handle_memory_retention_list)

    memory_retention_show_parser = memory_retention_subparsers.add_parser(
        "show",
        help="Show memory retention details",
        parents=[db_parent_optional],
    )
    memory_retention_show_parser.add_argument(
        "namespace",
        help="Namespace with retention rules",
    )
    memory_retention_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_retention_show_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_retention_show_parser.set_defaults(handler=_handle_memory_retention_show)

    memory_retention_set_parser = memory_retention_subparsers.add_parser(
        "set",
        help="Set memory retention rules",
        parents=[db_parent_optional],
    )
    memory_retention_set_parser.add_argument(
        "namespace",
        help="Namespace to configure",
    )
    memory_retention_set_parser.add_argument(
        "--max-items",
        dest="max_items",
        type=int,
        default=None,
        help="Maximum number of active items to retain",
    )
    memory_retention_set_parser.add_argument(
        "--ttl-seconds",
        dest="ttl_seconds",
        type=int,
        default=None,
        help="Time-to-live in seconds",
    )
    memory_retention_set_parser.add_argument(
        "--reason",
        required=True,
        help="Reason for setting retention",
    )
    memory_retention_set_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for retention updates",
    )
    memory_retention_set_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_retention_set_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_retention_set_parser.set_defaults(handler=_handle_memory_retention_set)

    memory_retention_clear_parser = memory_retention_subparsers.add_parser(
        "clear",
        help="Clear memory retention rules",
        parents=[db_parent_optional],
    )
    memory_retention_clear_parser.add_argument(
        "namespace",
        help="Namespace to clear",
    )
    memory_retention_clear_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for retention clear",
    )
    memory_retention_clear_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_retention_clear_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_retention_clear_parser.set_defaults(handler=_handle_memory_retention_clear)

    memory_doctor_parser = memory_subparsers.add_parser(
        "doctor",
        help="Diagnose and repair memory database issues",
        parents=[db_parent_optional],
    )
    memory_doctor_subparsers = memory_doctor_parser.add_subparsers(
        dest="memory_doctor_command",
        required=True,
    )
    memory_doctor_check_parser = memory_doctor_subparsers.add_parser(
        "check",
        help="Run read-only diagnostics",
        parents=[db_parent_optional],
    )
    memory_doctor_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_doctor_check_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_doctor_check_parser.set_defaults(handler=_handle_memory_doctor_check)

    memory_doctor_repair_parser = memory_doctor_subparsers.add_parser(
        "repair",
        help="Apply selected memory repairs",
        parents=[db_parent_optional],
    )
    memory_doctor_repair_parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM + ANALYZE",
    )
    memory_doctor_repair_parser.add_argument(
        "--optimize",
        action="store_true",
        help="Alias for --vacuum",
    )
    memory_doctor_repair_parser.add_argument(
        "--reindex",
        action="store_true",
        help="Run REINDEX",
    )
    memory_doctor_repair_parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="Recreate missing memory indexes",
    )
    memory_doctor_repair_parser.add_argument(
        "--enforce-foreign-keys",
        action="store_true",
        help="Check foreign_keys pragma before repair",
    )
    memory_doctor_repair_parser.add_argument(
        "--set-foreign-keys-on",
        action="store_true",
        help="Enable foreign_keys when enforcing",
    )
    memory_doctor_repair_parser.add_argument(
        "--purge-tombstones",
        action="store_true",
        help="Delete tombstoned items older than a cutoff",
    )
    memory_doctor_repair_parser.add_argument(
        "--namespace",
        default=None,
        help="Namespace for tombstone purge",
    )
    memory_doctor_repair_parser.add_argument(
        "--older-than-seconds",
        type=int,
        default=None,
        help="Minimum age in seconds for purge candidates",
    )
    memory_doctor_repair_parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum tombstones to delete per invocation",
    )
    memory_doctor_repair_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan repairs without applying changes",
    )
    memory_doctor_repair_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for repairs",
    )
    memory_doctor_repair_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_doctor_repair_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_doctor_repair_parser.set_defaults(handler=_handle_memory_doctor_repair)

    memory_snapshot_parser = memory_subparsers.add_parser(
        "snapshot",
        help="Export or import memory snapshots",
        parents=[db_parent_optional],
    )
    memory_snapshot_subparsers = memory_snapshot_parser.add_subparsers(
        dest="memory_snapshot_command",
        required=True,
    )

    memory_snapshot_export_parser = memory_snapshot_subparsers.add_parser(
        "export",
        help="Export memory items to a deterministic snapshot",
        parents=[db_parent_optional],
    )
    memory_snapshot_export_parser.add_argument(
        "--namespace",
        required=True,
        help="Namespace or prefix wildcard to export (e.g., global or project:*)",
    )
    memory_snapshot_export_parser.add_argument(
        "--out",
        required=True,
        help="Output snapshot file path",
    )
    memory_snapshot_export_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_snapshot_export_parser.set_defaults(handler=_handle_memory_snapshot_export)

    memory_snapshot_diff_parser = memory_snapshot_subparsers.add_parser(
        "diff",
        help="Diff a snapshot against the current memory store",
        parents=[db_parent_optional],
    )
    memory_snapshot_diff_parser.add_argument(
        "--in",
        dest="in_path",
        required=True,
        help="Input snapshot file path",
    )
    memory_snapshot_diff_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_snapshot_diff_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_snapshot_diff_parser.set_defaults(handler=_handle_memory_snapshot_diff)

    memory_snapshot_import_parser = memory_snapshot_subparsers.add_parser(
        "import",
        help="Import memory items from a snapshot",
        parents=[db_parent_optional],
    )
    memory_snapshot_import_parser.add_argument(
        "--in",
        dest="in_path",
        required=True,
        help="Input snapshot file path",
    )
    memory_snapshot_import_parser.add_argument(
        "--mode",
        choices=["merge", "overwrite", "skip-existing"],
        default="merge",
        help="Import mode (default: merge)",
    )
    memory_snapshot_import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without writing memory items",
    )
    memory_snapshot_import_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for high-risk memory writes",
    )
    memory_snapshot_import_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_snapshot_import_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    memory_snapshot_import_parser.set_defaults(handler=_handle_memory_snapshot_import)

    memory_summarize_parser = memory_subparsers.add_parser(
        "summarize",
        help="Promote run outcomes into persistent memory",
        parents=[db_parent_optional],
    )
    memory_summarize_subparsers = memory_summarize_parser.add_subparsers(
        dest="memory_summarize_command", required=True
    )

    memory_summarize_run_parser = memory_summarize_subparsers.add_parser(
        "run",
        help="Summarize a completed run into persistent memory",
        parents=[db_parent_optional],
    )
    memory_summarize_run_parser.add_argument(
        "run_id",
        help="Run ID to summarize",
    )
    memory_summarize_run_parser.add_argument(
        "--namespace",
        required=True,
        help="Target memory namespace (e.g., project:<name>, global)",
    )
    memory_summarize_run_parser.add_argument(
        "--confidence",
        default="medium",
        choices=["high", "medium", "low"],
        help="Confidence level for written items (default: medium)",
    )
    memory_summarize_run_parser.add_argument(
        "--include-outputs",
        action="store_true",
        dest="include_outputs",
        help="Include individual task outputs as additional memory items",
    )
    memory_summarize_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview what would be written without writing",
    )
    memory_summarize_run_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts",
    )
    memory_summarize_run_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmation",
    )
    memory_summarize_run_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path",
    )
    memory_summarize_run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    memory_summarize_run_parser.set_defaults(handler=_handle_memory_summarize_run)

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
        help="Override the local LLM model (default: gismo or GISMO_OLLAMA_MODEL)",
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
        "--memory-profile",
        dest="memory_profile",
        default=None,
        help="Inject memory using a named profile (read-only)",
    )
    ask_parser.add_argument(
        "--apply-memory-suggestions",
        action="store_true",
        help="Apply memory suggestions from the plan (policy-gated)",
    )
    ask_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file for memory writes",
    )
    ask_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmations",
    )
    ask_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for enqueue actions and memory suggestions",
    )
    ask_parser.add_argument(
        "--explain",
        action="store_true",
        help="Print expanded explain details",
    )
    ask_parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug tracebacks on LLM request errors",
    )
    ask_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output for the plan explain artifact",
    )
    ask_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Show the plan without enqueueing (default unless --enqueue is set)",
    )
    ask_parser.add_argument(
        "--defer",
        action="store_true",
        default=False,
        help="Save the generated plan as a pending plan for later approval (see: gismo plan)",
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
        "--memory",
        dest="use_memory",
        action="store_true",
        help="Inject eligible memory items into the planner prompt (read-only)",
    )
    agent_parser.add_argument(
        "--memory-profile",
        dest="memory_profile",
        default=None,
        help="Inject memory using a named profile (read-only)",
    )
    agent_parser.add_argument(
        "--role",
        dest="role",
        default=None,
        help="Agent role name or id (determines memory profile)",
    )
    agent_parser.add_argument(
        "--apply-memory-suggestions",
        action="store_true",
        help="Apply memory suggestions from the plan (policy-gated)",
    )
    agent_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting for confirmations",
    )
    agent_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output for the plan explain artifact",
    )
    agent_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan and explain without enqueueing",
    )
    agent_parser.add_argument(
        "goal",
        nargs="+",
        help="Goal statement for the agent loop",
    )
    agent_parser.set_defaults(handler=_handle_agent)

    agent_role_parser = subparsers.add_parser(
        "agent-role",
        help="Manage agent roles (deterministic role identities)",
        parents=[db_parent_optional],
    )
    agent_role_subparsers = agent_role_parser.add_subparsers(
        dest="agent_role_command",
    )
    agent_role_list_parser = agent_role_subparsers.add_parser(
        "list",
        help="List agent roles",
        parents=[db_parent_optional],
    )
    agent_role_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_role_list_parser.add_argument(
        "--active-only",
        action="store_true",
        help="Show only active roles",
    )
    agent_role_list_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    agent_role_list_parser.set_defaults(handler=_handle_agent_role_list)

    agent_role_show_parser = agent_role_subparsers.add_parser(
        "show",
        help="Show agent role details",
        parents=[db_parent_optional],
    )
    agent_role_show_parser.add_argument(
        "selector",
        help="Role name or id",
    )
    agent_role_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_role_show_parser.add_argument(
        "--policy",
        default=None,
        help="Optional policy file path for audit hashing",
    )
    agent_role_show_parser.set_defaults(handler=_handle_agent_role_show)

    agent_role_create_parser = agent_role_subparsers.add_parser(
        "create",
        help="Create an agent role (requires confirmation)",
        parents=[db_parent_optional],
    )
    agent_role_create_parser.add_argument(
        "--name",
        required=True,
        help="Role name (unique)",
    )
    agent_role_create_parser.add_argument(
        "--description",
        default=None,
        help="Optional role description",
    )
    agent_role_create_parser.add_argument(
        "--memory-profile",
        dest="memory_profile",
        default=None,
        help="Memory profile selector (name or id)",
    )
    agent_role_create_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    agent_role_create_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for create",
    )
    agent_role_create_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting",
    )
    agent_role_create_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_role_create_parser.set_defaults(handler=_handle_agent_role_create)

    agent_role_retire_parser = agent_role_subparsers.add_parser(
        "retire",
        help="Retire an agent role (requires confirmation)",
        parents=[db_parent_optional],
    )
    agent_role_retire_parser.add_argument(
        "selector",
        help="Role name or id",
    )
    agent_role_retire_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    agent_role_retire_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for retire",
    )
    agent_role_retire_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting",
    )
    agent_role_retire_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_role_retire_parser.set_defaults(handler=_handle_agent_role_retire)

    agent_session_parser = subparsers.add_parser(
        "agent-session",
        help="Manage supervised agent sessions",
        parents=[db_parent_optional],
    )
    agent_session_subparsers = agent_session_parser.add_subparsers(
        dest="agent_session_command",
    )
    agent_session_start_parser = agent_session_subparsers.add_parser(
        "start",
        help="Start a new agent session",
        parents=[db_parent_optional],
    )
    agent_session_start_parser.add_argument(
        "--goal",
        required=True,
        help="Goal statement for the session",
    )
    agent_session_start_parser.add_argument(
        "--role",
        default=None,
        help="Agent role name or id (determines memory profile)",
    )
    agent_session_start_parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="Maximum number of session steps (default: 12)",
    )
    agent_session_start_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_session_start_parser.set_defaults(handler=_handle_agent_session_start)

    agent_session_show_parser = agent_session_subparsers.add_parser(
        "show",
        help="Show a session",
        parents=[db_parent_optional],
    )
    agent_session_show_parser.add_argument(
        "session_id",
        help="Session id",
    )
    agent_session_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_session_show_parser.set_defaults(handler=_handle_agent_session_show)

    agent_session_list_parser = agent_session_subparsers.add_parser(
        "list",
        help="List sessions",
        parents=[db_parent_optional],
    )
    agent_session_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    agent_session_list_parser.set_defaults(handler=_handle_agent_session_list)

    agent_session_pause_parser = agent_session_subparsers.add_parser(
        "pause",
        help="Pause a session",
        parents=[db_parent_optional],
    )
    agent_session_pause_parser.add_argument(
        "session_id",
        help="Session id",
    )
    agent_session_pause_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    agent_session_pause_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting",
    )
    agent_session_pause_parser.set_defaults(handler=_handle_agent_session_pause)

    agent_session_resume_parser = agent_session_subparsers.add_parser(
        "resume",
        help="Resume a session (one iteration)",
        parents=[db_parent_optional],
    )
    agent_session_resume_parser.add_argument(
        "session_id",
        help="Session id",
    )
    agent_session_resume_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    agent_session_resume_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for high-risk plans",
    )
    agent_session_resume_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting",
    )
    agent_session_resume_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan and explain without enqueueing",
    )
    agent_session_resume_parser.set_defaults(handler=_handle_agent_session_resume)

    agent_session_cancel_parser = agent_session_subparsers.add_parser(
        "cancel",
        help="Cancel a session",
        parents=[db_parent_optional],
    )
    agent_session_cancel_parser.add_argument(
        "session_id",
        help="Session id",
    )
    agent_session_cancel_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    agent_session_cancel_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail closed instead of prompting",
    )
    agent_session_cancel_parser.set_defaults(handler=_handle_agent_session_cancel)

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

    tui_parser = subparsers.add_parser(
        "tui",
        help="Open the live terminal dashboard",
        parents=[db_parent_optional],
    )
    tui_parser.set_defaults(handler=_handle_tui)

    app_parser = subparsers.add_parser(
        "app",
        help="Open GISMO as a native desktop window (no browser)",
        parents=[db_parent_optional],
    )
    app_parser.set_defaults(handler=_handle_app)

    web_parser = subparsers.add_parser(
        "web",
        help="Open the local web dashboard in a browser",
        parents=[db_parent_optional],
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=7800,
        help="Port to listen on (default: 7800)",
    )
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    web_parser.add_argument(
        "--no-browser",
        action="store_true",
        default=False,
        help="Don't open the browser automatically",
    )
    web_parser.set_defaults(handler=_handle_web)

    # ── gismo plan ─────────────────────────────────────────────────────────
    plan_parser = subparsers.add_parser(
        "plan",
        help="Manage pending plans (review, approve, reject, edit)",
        parents=[db_parent_optional],
    )
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)

    plan_list_parser = plan_subparsers.add_parser(
        "list",
        help="List plans",
        parents=[db_parent_optional],
    )
    plan_list_parser.add_argument("--status", default=None, metavar="STATUS",
                                  help="Filter by status: PENDING, APPROVED, REJECTED")
    plan_list_parser.add_argument("--limit", type=int, default=50)
    plan_list_parser.add_argument("--json", action="store_true", default=False)
    plan_list_parser.set_defaults(handler=lambda a: plan_cli.handle_plan_list(a))

    plan_show_parser = plan_subparsers.add_parser(
        "show",
        help="Show plan details",
        parents=[db_parent_optional],
    )
    plan_show_parser.add_argument("id", help="Plan ID or prefix")
    plan_show_parser.add_argument("--json", action="store_true", default=False)
    plan_show_parser.set_defaults(handler=lambda a: plan_cli.handle_plan_show(a))

    plan_approve_parser = plan_subparsers.add_parser(
        "approve",
        help="Approve a pending plan and enqueue its actions",
        parents=[db_parent_optional],
    )
    plan_approve_parser.add_argument("id", help="Plan ID or prefix")
    plan_approve_parser.add_argument("--yes", action="store_true", default=False,
                                     help="Skip confirmation prompt")
    plan_approve_parser.set_defaults(handler=lambda a: plan_cli.handle_plan_approve(a))

    plan_reject_parser = plan_subparsers.add_parser(
        "reject",
        help="Reject a pending plan",
        parents=[db_parent_optional],
    )
    plan_reject_parser.add_argument("id", help="Plan ID or prefix")
    plan_reject_parser.add_argument("--reason", default=None, metavar="TEXT",
                                    help="Optional rejection reason")
    plan_reject_parser.add_argument("--yes", action="store_true", default=False)
    plan_reject_parser.set_defaults(handler=lambda a: plan_cli.handle_plan_reject(a))

    plan_edit_parser = plan_subparsers.add_parser(
        "edit",
        help="Edit an action in a pending plan before approval",
        parents=[db_parent_optional],
    )
    plan_edit_parser.add_argument("id", help="Plan ID or prefix")
    plan_edit_parser.add_argument("--action", type=int, required=True, metavar="N",
                                  help="1-based action index to edit")
    plan_edit_parser.add_argument("--cmd", default=None, metavar="COMMAND",
                                  help="Replace action command text")
    plan_edit_parser.add_argument("--remove", action="store_true", default=False,
                                  help="Remove the action from the plan")
    plan_edit_parser.set_defaults(handler=lambda a: plan_cli.handle_plan_edit(a))

    # ── gismo tts ──────────────────────────────────────────────────────────
    tts_parser = subparsers.add_parser(
        "tts",
        help="Text-to-speech via piper-tts",
        parents=[db_parent_optional],
    )
    tts_subparsers = tts_parser.add_subparsers(dest="tts_command", required=True)

    tts_speak_parser = tts_subparsers.add_parser(
        "speak",
        help="Synthesize and play text",
        parents=[db_parent_optional],
    )
    tts_speak_parser.add_argument("text", help="Text to synthesize")
    tts_speak_parser.add_argument("--voice", default=None, help="Voice ID (overrides preference)")
    tts_speak_parser.add_argument("--out", default=None, metavar="FILE", help="Write WAV to file instead of playing")
    tts_speak_parser.add_argument("--no-play", action="store_true", default=False, help="Write WAV to stdout instead of playing")
    tts_speak_parser.set_defaults(handler=_handle_tts_speak)

    tts_voices_parser = tts_subparsers.add_parser(
        "voices",
        help="Manage voices",
        parents=[db_parent_optional],
    )
    tts_voices_subparsers = tts_voices_parser.add_subparsers(dest="tts_voices_command", required=True)

    tts_voices_list_parser = tts_voices_subparsers.add_parser(
        "list",
        help="List available voices",
        parents=[db_parent_optional],
    )
    tts_voices_list_parser.set_defaults(handler=_handle_tts_voices_list)

    tts_voices_set_parser = tts_voices_subparsers.add_parser(
        "set",
        help="Set preferred voice",
        parents=[db_parent_optional],
    )
    tts_voices_set_parser.add_argument("voice", help="Voice ID")
    tts_voices_set_parser.set_defaults(handler=_handle_tts_voices_set)

    tts_voices_download_parser = tts_voices_subparsers.add_parser(
        "download",
        help="Pre-download a voice model",
        parents=[db_parent_optional],
    )
    tts_voices_download_parser.add_argument("voice", help="Voice ID")
    tts_voices_download_parser.set_defaults(handler=_handle_tts_voices_download)

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
    if len(argv) > 1 and argv[0] == "agent" and argv[1] == "role":
        argv = ["agent-role", *argv[2:]]
    if len(argv) > 1 and argv[0] == "agent" and argv[1] == "session":
        argv = ["agent-session", *argv[2:]]
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
    db_path = getattr(args, "db_path", None)
    if db_path:
        try:
            from gismo.onboarding import needs_onboarding, run_cli_onboarding
            if needs_onboarding(db_path):
                run_cli_onboarding(db_path)
        except Exception:
            pass  # never let onboarding block a command
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
