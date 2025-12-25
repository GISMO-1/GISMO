"""Local IPC control plane for GISMO."""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Dict

from gismo.cli.operator import parse_command
from gismo.core.models import QueueStatus, TaskStatus
from gismo.core.state import StateStore


@dataclass(frozen=True)
class IPCEndpoint:
    address: str
    family: str


@dataclass(frozen=True)
class IPCResponse:
    ok: bool
    request_id: str
    data: Dict[str, Any] | None
    error: str | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "data": self.data,
            "error": self.error,
        }


LOGGER = logging.getLogger(__name__)


class IPCConnectionError(RuntimeError):
    """Raised when the IPC client cannot connect to the server."""


def _connection_error_types() -> tuple[type[BaseException], ...]:
    if os.name == "nt":
        return (FileNotFoundError, OSError, EOFError)
    return (FileNotFoundError, ConnectionRefusedError, OSError, EOFError)


def _connect(endpoint: IPCEndpoint) -> Client:
    return Client(endpoint.address, family=endpoint.family)


def default_ipc_endpoint() -> IPCEndpoint:
    if os.name == "nt":
        return IPCEndpoint(r"\\.\pipe\gismo-ipc", "AF_PIPE")
    return IPCEndpoint("/tmp/gismo-ipc.sock", "AF_UNIX")


def load_ipc_token(token: str | None) -> str:
    value = token or os.environ.get("GISMO_IPC_TOKEN")
    if not value:
        raise ValueError("IPC token required via --token or GISMO_IPC_TOKEN")
    return value


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_queue_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "total": stats["total"],
        "by_status": stats["by_status"],
        "created_at": {
            "oldest": _serialize_dt(stats["created_at"]["oldest"]),
            "newest": _serialize_dt(stats["created_at"]["newest"]),
        },
        "updated_at": {
            "oldest": _serialize_dt(stats["updated_at"]["oldest"]),
            "newest": _serialize_dt(stats["updated_at"]["newest"]),
        },
        "attempts": stats["attempts"],
    }


def _serialize_run_show(state_store: StateStore, run_id: str) -> Dict[str, Any] | None:
    run = state_store.get_run(run_id)
    if run is None:
        return None
    tasks = list(state_store.list_tasks(run.id))
    tool_calls = list(state_store.list_tool_calls(run.id))
    task_payloads = []
    for task in tasks:
        task_payloads.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status.value,
                "error": task.error,
                "output_json": task.output_json,
                "created_at": _serialize_dt(task.created_at),
                "updated_at": _serialize_dt(task.updated_at),
                "failure_type": task.failure_type.value if task.failure_type else None,
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
                "started_at": _serialize_dt(call.started_at),
                "finished_at": _serialize_dt(call.finished_at),
                "output_json": call.output_json,
                "error": call.error,
            }
        )
    return {
        "run": {
            "id": run.id,
            "label": run.label,
            "created_at": _serialize_dt(run.created_at),
        },
        "tasks": task_payloads,
        "tool_calls": call_payloads,
    }


def handle_ipc_request(
    request: Dict[str, Any],
    expected_token: str,
    state_store: StateStore,
) -> Dict[str, Any]:
    request_id = str(request.get("request_id") or uuid.uuid4())
    token = request.get("token")
    action = request.get("action")
    args = request.get("args") or {}

    if token != expected_token:
        return IPCResponse(False, request_id, None, "unauthorized").to_dict()

    try:
        if action == "enqueue":
            command_text = str(args.get("command") or "").strip()
            if not command_text:
                raise ValueError("enqueue requires a command string")
            parse_command(command_text)
            run_id = args.get("run_id")
            max_attempts = int(args.get("max_attempts", 3))
            item = state_store.enqueue_command(
                command_text=command_text,
                run_id=run_id,
                max_attempts=max_attempts,
            )
            data = {"queue_item_id": item.id, "status": item.status.value}
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "queue_stats":
            stats = state_store.queue_stats()
            data = _serialize_queue_stats(stats)
            data["db_path"] = state_store.db_path
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "daemon_status":
            data = {"paused": state_store.get_daemon_paused()}
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "daemon_pause":
            state_store.set_daemon_paused(True)
            data = {"paused": True}
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "daemon_resume":
            state_store.set_daemon_paused(False)
            data = {"paused": False}
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "queue_purge_failed":
            deleted = state_store.delete_queue_items_by_status(QueueStatus.FAILED)
            data = {"deleted": deleted}
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "queue_requeue_stale":
            older_than_minutes = int(args.get("older_than_minutes", 0))
            limit = args.get("limit")
            limit_value = int(limit) if limit is not None else 100
            if older_than_minutes <= 0:
                raise ValueError("older_than_minutes must be > 0")
            updated = state_store.requeue_stale_in_progress_queue(
                older_than_seconds=older_than_minutes * 60,
                limit=limit_value,
            )
            data = {
                "requeued": updated,
                "older_than_minutes": older_than_minutes,
                "limit": limit_value,
            }
            return IPCResponse(True, request_id, data, None).to_dict()
        if action == "run_show":
            run_id = str(args.get("run_id") or "").strip()
            if not run_id:
                raise ValueError("run_show requires a run id")
            payload = _serialize_run_show(state_store, run_id)
            if payload is None:
                return IPCResponse(False, request_id, None, "not_found").to_dict()
            return IPCResponse(True, request_id, payload, None).to_dict()
        return IPCResponse(False, request_id, None, "unsupported_action").to_dict()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return IPCResponse(False, request_id, None, str(exc)).to_dict()


def _parse_json_payload(raw: bytes) -> Dict[str, Any] | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _log_request(request_id: str, action: str | None, caller: str | None) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    safe_action = action or "unknown"
    safe_caller = caller or "unknown"
    LOGGER.info(
        "ipc_request request_id=%s action=%s timestamp=%s caller=%s",
        request_id,
        safe_action,
        timestamp,
        safe_caller,
    )


def serve_ipc(db_path: str, token: str) -> None:
    endpoint = default_ipc_endpoint()
    socket_path = Path(endpoint.address) if endpoint.family == "AF_UNIX" else None
    if socket_path is not None:
        if socket_path.exists():
            if socket_path.is_socket():
                socket_path.unlink()
            else:
                raise ValueError(f"IPC socket path exists and is not a socket: {socket_path}")
        socket_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    listener = Listener(endpoint.address, family=endpoint.family)
    state_store = StateStore(db_path)

    try:
        while True:
            try:
                conn = listener.accept()
            except KeyboardInterrupt:
                break
            with conn:
                try:
                    raw = conn.recv_bytes()
                except EOFError:
                    continue
                request = _parse_json_payload(raw)
                if request is None:
                    request_id = str(uuid.uuid4())
                    _log_request(request_id, "invalid_json", None)
                    response = IPCResponse(False, request_id, None, "invalid_json").to_dict()
                    conn.send_bytes(json.dumps(response).encode("utf-8"))
                    continue
                request_id = str(request.get("request_id") or uuid.uuid4())
                _log_request(request_id, str(request.get("action")), None)
                response = handle_ipc_request(request, token, state_store)
                conn.send_bytes(json.dumps(response).encode("utf-8"))
    finally:
        listener.close()
        if socket_path is not None and socket_path.exists() and socket_path.is_socket():
            socket_path.unlink()


def ipc_request(action: str, args: Dict[str, Any], token: str) -> Dict[str, Any]:
    endpoint = default_ipc_endpoint()
    request_id = str(uuid.uuid4())
    request = {
        "request_id": request_id,
        "token": token,
        "action": action,
        "args": args,
    }
    try:
        with _connect(endpoint) as conn:
            conn.send_bytes(json.dumps(request).encode("utf-8"))
            response_raw = conn.recv_bytes()
    except _connection_error_types() as exc:
        raise IPCConnectionError("IPC connection failed") from exc
    payload = _parse_json_payload(response_raw)
    if payload is None:
        raise ValueError("Invalid IPC response")
    return payload


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "-"


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


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _run_status(tasks: list[Dict[str, Any]]) -> str:
    if not tasks:
        return "pending"
    statuses = {task["status"] for task in tasks}
    if TaskStatus.FAILED.value in statuses:
        return "failed"
    if TaskStatus.RUNNING.value in statuses:
        return "running"
    if statuses.issubset({TaskStatus.SUCCEEDED.value}):
        return "succeeded"
    return "pending"


def _run_time_bounds(
    run: Dict[str, Any],
    tasks: list[Dict[str, Any]],
    tool_calls: list[Dict[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    start_candidates = [
        _parse_dt(run.get("created_at")),
    ]
    start_candidates.extend(_parse_dt(task.get("created_at")) for task in tasks)
    start_candidates.extend(_parse_dt(call.get("started_at")) for call in tool_calls)
    start_candidates = [value for value in start_candidates if value is not None]
    start_time = min(start_candidates) if start_candidates else None

    end_candidates = [
        _parse_dt(task.get("updated_at")) for task in tasks if task.get("updated_at")
    ]
    end_candidates.extend(
        _parse_dt(call.get("finished_at")) for call in tool_calls if call.get("finished_at")
    )
    end_candidates = [value for value in end_candidates if value is not None]
    end_time = max(end_candidates) if end_candidates else None
    return start_time, end_time


def format_queue_stats_output(stats: Dict[str, Any]) -> str:
    lines = [
        f"DB: {stats['db_path']}",
        f"Total: {stats['total']}",
        "By status:",
    ]
    for status in QueueStatus:
        lines.append(f"  {status.value:12} {stats['by_status'].get(status.value, 0)}")
    created_oldest = _parse_dt(stats["created_at"]["oldest"])
    created_newest = _parse_dt(stats["created_at"]["newest"])
    updated_oldest = _parse_dt(stats["updated_at"]["oldest"])
    updated_newest = _parse_dt(stats["updated_at"]["newest"])
    lines.append(
        f"Created: oldest={_fmt_dt(created_oldest)} newest={_fmt_dt(created_newest)}"
    )
    lines.append(
        f"Updated: oldest={_fmt_dt(updated_oldest)} newest={_fmt_dt(updated_newest)}"
    )
    lines.append(
        "Attempts: "
        f"items_with_attempts={stats['attempts']['items_with_attempts']} "
        f"max_attempt_count={stats['attempts']['max_attempt_count']}"
    )
    return "\n".join(lines)


def format_enqueue_output(data: Dict[str, Any]) -> str:
    return f"Enqueued {data['queue_item_id']} status={data['status']}"


def format_daemon_status_output(data: Dict[str, Any]) -> str:
    paused = data.get("paused", False)
    state = "paused" if paused else "running"
    return f"Daemon status: {state}"


def format_daemon_pause_output(data: Dict[str, Any]) -> str:
    paused = data.get("paused", False)
    return "Daemon paused." if paused else "Daemon pause failed."


def format_daemon_resume_output(data: Dict[str, Any]) -> str:
    paused = data.get("paused", True)
    return "Daemon resumed." if not paused else "Daemon resume failed."


def format_queue_purge_failed_output(data: Dict[str, Any]) -> str:
    deleted = int(data.get("deleted", 0))
    return f"Deleted {deleted} failed queue item(s)."


def format_queue_requeue_stale_output(data: Dict[str, Any]) -> str:
    requeued = int(data.get("requeued", 0))
    older_than_minutes = data.get("older_than_minutes", "-")
    limit = data.get("limit", "-")
    return (
        f"Requeued {requeued} stale queue item(s) "
        f"(older_than_minutes={older_than_minutes}, limit={limit})."
    )


def format_run_show_output(payload: Dict[str, Any]) -> str:
    run = payload["run"]
    tasks = payload["tasks"]
    tool_calls = payload["tool_calls"]
    status = _run_status(tasks)
    start_time, end_time = _run_time_bounds(run, tasks, tool_calls)

    lines = [
        "=== GISMO Run Summary ===",
        f"Run ID:     {run['id']}",
        f"Status:     {status}",
        f"Started:    {_fmt_dt(start_time)}",
        f"Finished:   {_fmt_dt(end_time)}",
        "Tasks:",
    ]

    if not tasks:
        lines.append("  (no tasks)")
        return "\n".join(lines)

    calls_by_task: Dict[str, list[Dict[str, Any]]] = {}
    for call in tool_calls:
        calls_by_task.setdefault(call["task_id"], []).append(call)

    for task in tasks:
        lines.append(f"- {task['id']} {task['title']} [{task['status']}]")
        if task.get("error"):
            lines.append(f"  error: {_summarize_value(task['error'], 200)}")
        if task.get("output_json"):
            lines.append(f"  output: {_summarize_value(task['output_json'], 200)}")
        task_calls = calls_by_task.get(task["id"], [])
        if not task_calls:
            lines.append("  Tool Calls: none")
            continue
        lines.append("  Tool Calls:")
        for call in task_calls:
            lines.append(
                "    - "
                f"{call['id']} tool={call['tool_name']} status={call['status']} "
                f"started={_fmt_dt(_parse_dt(call.get('started_at')))} "
                f"finished={_fmt_dt(_parse_dt(call.get('finished_at')))}"
            )
            if call.get("output_json"):
                lines.append(f"      output: {_summarize_value(call['output_json'], 200)}")
            if call.get("error"):
                lines.append(f"      error: {_summarize_value(call['error'], 200)}")

    return "\n".join(lines)


def parse_ipc_response(response: Dict[str, Any]) -> IPCResponse:
    return IPCResponse(
        ok=bool(response.get("ok")),
        request_id=str(response.get("request_id") or ""),
        data=response.get("data"),
        error=response.get("error"),
    )
