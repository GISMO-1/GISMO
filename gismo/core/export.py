"""Audit export utilities for runs, tasks, and tool calls."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gismo.core.models import Run, Task, ToolCall
from gismo.core.paths import resolve_exports_dir
from gismo.core.state import MemoryEventRecord, MemoryProvenance, StateStore


def export_run_jsonl(
    state_store: StateStore,
    run_id: str,
    *,
    out_path: str | Path | None = None,
    redact: bool = False,
    base_dir: Path | None = None,
) -> Path:
    run = state_store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")
    if base_dir is None:
        exports_dir = resolve_exports_dir(state_store.db_path)
    else:
        exports_dir = base_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
    resolved_out = _resolve_output_path(run_id, out_path, exports_dir)
    tasks = list(state_store.list_tasks(run_id))
    tool_calls = list(state_store.list_tool_calls(run_id))
    memory_provenance = state_store.get_memory_provenance(run.id)
    plan_event_id = None
    if isinstance(run.metadata_json, dict):
        plan_event_id = run.metadata_json.get("plan_event_id")
    plan_event = state_store.get_event(plan_event_id) if plan_event_id else None
    memory_events = state_store.list_memory_events(
        related_run_id=run.id,
        related_ask_event_id=plan_event_id,
    )
    records = _build_records(
        run,
        tasks,
        tool_calls,
        memory_provenance=memory_provenance,
        plan_event=plan_event,
        memory_events=memory_events,
        redact=redact,
    )
    _write_jsonl(resolved_out, records)
    return resolved_out


def export_latest_run_jsonl(
    state_store: StateStore,
    *,
    out_path: str | Path | None = None,
    redact: bool = False,
    base_dir: Path | None = None,
) -> Path:
    run = state_store.get_latest_run()
    if run is None:
        raise ValueError("No runs found to export")
    return export_run_jsonl(
        state_store,
        run.id,
        out_path=out_path,
        redact=redact,
        base_dir=base_dir,
    )


def _resolve_output_path(run_id: str, out_path: str | Path | None, exports_dir: Path) -> Path:
    if out_path is None:
        resolved = exports_dir / f"{run_id}.jsonl"
    else:
        resolved = Path(out_path).expanduser()
    resolved = resolved.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")


def _build_records(
    run: Run,
    tasks: list[Task],
    tool_calls: list[ToolCall],
    *,
    memory_provenance: MemoryProvenance,
    plan_event: Any | None,
    memory_events: list[MemoryEventRecord],
    redact: bool,
) -> list[dict[str, Any]]:
    sorted_tasks = sorted(tasks, key=lambda task: task.created_at)
    sorted_calls = sorted(tool_calls, key=lambda call: (call.started_at, call.attempt_number))
    records = [_serialize_run(run, memory_provenance)]
    if plan_event is not None:
        records.append(
            _serialize_event(plan_event, memory_provenance=memory_provenance, redact=redact)
        )
    if memory_events:
        records.extend(
            _serialize_memory_event(event, redact=redact) for event in memory_events
        )
    records.extend(_serialize_task(task, redact=redact) for task in sorted_tasks)
    records.extend(_serialize_tool_call(call, redact=redact) for call in sorted_calls)
    return records


def _serialize_run(run: Run, memory_provenance: MemoryProvenance) -> dict[str, Any]:
    payload = asdict(run)
    payload["created_at"] = run.created_at.isoformat()
    return {
        "record_type": "run",
        "id": run.id,
        "created_at": payload["created_at"],
        "label": run.label,
        "metadata": run.metadata_json,
        "status": "CREATED",
        "failure_type": "NONE",
        "memory_provenance": memory_provenance.to_dict(),
    }


def _serialize_event(
    event: Any,
    *,
    memory_provenance: MemoryProvenance,
    redact: bool,
) -> dict[str, Any]:
    payload = event.json_payload
    if redact:
        payload = _redact_payload(payload)
    return {
        "record_type": "event",
        "id": event.id,
        "timestamp": event.ts.isoformat(),
        "actor": event.actor,
        "event_type": event.event_type,
        "message": event.message,
        "payload": payload,
        "memory_provenance": memory_provenance.to_dict(),
    }


def _serialize_memory_event(
    event: MemoryEventRecord,
    *,
    redact: bool,
) -> dict[str, Any]:
    request = event.request
    result_meta = event.result_meta
    if redact:
        request = _redact_payload(request)
        result_meta = _redact_payload(result_meta)
    confirmation = result_meta.get("confirmation", {}) if isinstance(result_meta, dict) else {}
    return {
        "record_type": "memory_event",
        "id": event.id,
        "timestamp": event.timestamp.isoformat(),
        "operation": event.operation,
        "actor": event.actor,
        "policy_hash": event.policy_hash,
        "request": request,
        "result_meta": result_meta,
        "related_run_id": event.related_run_id,
        "related_ask_event_id": event.related_ask_event_id,
        "originating_run_id": event.related_run_id,
        "originating_event_id": event.related_ask_event_id,
        "policy_decision": result_meta.get("policy_decision"),
        "policy_reason": result_meta.get("policy_reason"),
        "confirmation_required": confirmation.get("required"),
        "confirmation_provided": confirmation.get("provided"),
        "confirmation_mode": confirmation.get("mode"),
    }


def _serialize_task(task: Task, *, redact: bool) -> dict[str, Any]:
    inputs = task.input_json
    outputs = task.output_json
    if redact:
        inputs = _redact_payload(inputs)
        outputs = _redact_outputs(outputs)
    tool_name = None
    if isinstance(task.input_json, dict):
        tool_name = task.input_json.get("tool")
    return {
        "record_type": "task",
        "id": task.id,
        "run_id": task.run_id,
        "title": task.title,
        "description": task.description,
        "tool_name": tool_name,
        "inputs": inputs,
        "outputs": outputs,
        "status": task.status.value,
        "failure_type": task.failure_type.value if task.failure_type else "NONE",
        "error": task.error,
        "status_reason": task.status_reason,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _serialize_tool_call(call: ToolCall, *, redact: bool) -> dict[str, Any]:
    inputs = call.input_json
    outputs = call.output_json
    if redact:
        inputs = _redact_payload(inputs)
        outputs = _redact_outputs(outputs)
    return {
        "record_type": "tool_call",
        "id": call.id,
        "run_id": call.run_id,
        "task_id": call.task_id,
        "tool_name": call.tool_name,
        "inputs": inputs,
        "outputs": outputs,
        "status": call.status.value,
        "failure_type": call.failure_type.value if call.failure_type else "NONE",
        "error": call.error,
        "attempt_number": call.attempt_number,
        "started_at": call.started_at.isoformat(),
        "finished_at": call.finished_at.isoformat() if call.finished_at else None,
    }


def _redact_outputs(output: Any) -> Any:
    if output is None:
        return None
    if _payload_size(output) > 1024:
        return "[REDACTED]"
    return _redact_payload(output)


def _payload_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"content", "stdout", "stderr"}:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    return payload
