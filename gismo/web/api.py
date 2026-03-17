"""GISMO web API — pure data layer (no HTTP).

All functions return JSON-serialisable dicts/lists and raise
``ValueError`` for bad input (404-class) or ``RuntimeError`` for
state errors.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gismo.core.models import QueueStatus
from gismo.core.state import StateStore
from gismo.memory.store import list_namespaces, list_items_for_snapshot


# ── helpers ────────────────────────────────────────────────────────────────


def _dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _status_val(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


# ── status / daemon ────────────────────────────────────────────────────────


def get_status(db_path: str) -> dict[str, Any]:
    """Return daemon heartbeat info + queue stats."""
    with StateStore(db_path) as store:
        hb = store.get_daemon_heartbeat()
        paused = store.get_daemon_paused()
        stats = store.queue_stats()

    now = datetime.now(timezone.utc)
    daemon: dict[str, Any]
    if hb is None:
        daemon = {"running": False, "paused": paused}
    else:
        last_seen = hb.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_secs = max(0, int((now - last_seen).total_seconds()))
        stale = age_secs > 30
        daemon = {
            "running": True,
            "paused": paused,
            "stale": stale,
            "pid": hb.pid,
            "started_at": _dt(hb.started_at),
            "last_seen": _dt(hb.last_seen),
            "age_secs": age_secs,
        }
    return {"daemon": daemon, "queue": stats}


def get_queue_stats(db_path: str) -> dict[str, Any]:
    """Return queue summary statistics only."""
    with StateStore(db_path) as store:
        return store.queue_stats()


def set_daemon_paused(db_path: str, paused: bool) -> dict[str, Any]:
    with StateStore(db_path) as store:
        store.set_daemon_paused(paused)
    return {"paused": paused}


# ── queue ──────────────────────────────────────────────────────────────────


def get_queue(db_path: str, limit: int = 100) -> list[dict[str, Any]]:
    with StateStore(db_path) as store:
        items = store.list_queue_items(limit=limit, newest_first=True)
    result = []
    for item in items:
        result.append({
            "id": item.id,
            "status": _status_val(item.status),
            "command_text": item.command_text,
            "attempt_count": item.attempt_count,
            "max_retries": item.max_retries,
            "created_at": _dt(item.created_at),
            "updated_at": _dt(item.updated_at),
            "started_at": _dt(item.started_at),
            "finished_at": _dt(item.finished_at),
            "last_error": item.last_error,
            "cancel_requested": item.cancel_requested,
            "run_id": item.run_id,
        })
    return result


def cancel_queue_item(db_path: str, item_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        item = store.request_queue_item_cancel(item_id)
    if item is None:
        raise ValueError(f"Queue item not found: {item_id}")
    return {"id": item.id, "status": _status_val(item.status)}


def purge_failed(db_path: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        count = store.delete_queue_items_by_status(QueueStatus.FAILED)
    return {"deleted": count}


# ── runs ───────────────────────────────────────────────────────────────────


def get_runs(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    with StateStore(db_path) as store:
        runs = list(store.list_runs(limit=limit, newest_first=True))
        task_map = {run.id: list(store.list_tasks(run.id)) for run in runs}

    result = []
    for run in runs:
        tasks = task_map.get(run.id, [])
        statuses = [_status_val(t.status) for t in tasks]
        total = len(tasks)
        succ = statuses.count("SUCCEEDED")
        fail = statuses.count("FAILED")
        running = statuses.count("RUNNING")

        if fail:
            run_status = "failed"
        elif running:
            run_status = "running"
        elif total and succ == total:
            run_status = "succeeded"
        else:
            run_status = "pending"

        result.append({
            "id": run.id,
            "label": run.label or "",
            "status": run_status,
            "created_at": _dt(run.created_at),
            "task_total": total,
            "task_succeeded": succ,
            "task_failed": fail,
            "task_running": running,
        })
    return result


def get_run_detail(db_path: str, run_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        runs = [r for r in store.list_runs(limit=10000) if r.id == run_id]
        if not runs:
            raise ValueError(f"Run not found: {run_id}")
        run = runs[0]
        tasks = list(store.list_tasks(run_id))
        tool_calls = list(store.list_tool_calls(run_id))

    return {
        "id": run.id,
        "label": run.label or "",
        "created_at": _dt(run.created_at),
        "metadata": run.metadata_json,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": _status_val(t.status),
                "created_at": _dt(t.created_at),
                "updated_at": _dt(t.updated_at),
                "error": t.error,
                "failure_type": _status_val(t.failure_type),
            }
            for t in tasks
        ],
        "tool_calls": [
            {
                "id": tc.id,
                "tool_name": tc.tool_name,
                "status": _status_val(tc.status),
                "started_at": _dt(tc.started_at),
                "finished_at": _dt(tc.finished_at),
                "error": tc.error,
            }
            for tc in tool_calls
        ],
    }


# ── memory ─────────────────────────────────────────────────────────────────


def get_memory(db_path: str) -> dict[str, Any]:
    namespaces = list_namespaces(db_path)
    items = list_items_for_snapshot(db_path, namespace=None, namespace_prefix=None)

    items_by_ns: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.is_tombstoned:
            continue
        entry = {
            "id": item.id,
            "key": item.key,
            "kind": item.kind,
            "value": item.value,
            "confidence": item.confidence,
            "source": item.source,
            "tags": item.tags,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        items_by_ns.setdefault(item.namespace, []).append(entry)

    ns_list = [
        {
            "namespace": ns.namespace,
            "item_count": ns.item_count,
            "tombstone_count": ns.tombstone_count,
            "last_write_at": ns.last_write_at,
            "retired": ns.retired,
        }
        for ns in namespaces
    ]
    return {"namespaces": ns_list, "items": items_by_ns}


# ── Plan approval ─────────────────────────────────────────────────────────


def get_plans(
    db_path: str,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from gismo.core.models import PlanStatus

    status_filter = None
    if status:
        try:
            status_filter = PlanStatus(status.upper())
        except ValueError:
            pass

    with StateStore(db_path) as store:
        plans = store.list_pending_plans(status=status_filter, limit=limit)

    return [
        {
            "id": p.id,
            "status": p.status.value,
            "risk_level": p.risk_level,
            "risk_flags": p.risk_json.get("risk_flags", []),
            "intent": p.intent,
            "user_text": p.user_text,
            "actor": p.actor,
            "created_at": _dt(p.created_at),
            "updated_at": _dt(p.updated_at),
            "action_count": len(p.plan_json.get("actions", [])),
            "rejection_reason": p.rejection_reason,
            "approved_at": _dt(p.approved_at),
            "rejected_at": _dt(p.rejected_at),
        }
        for p in plans
    ]


def get_plan_detail(db_path: str, plan_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
    if plan is None:
        raise ValueError(f"Plan not found: {plan_id}")
    return {
        "id": plan.id,
        "status": plan.status.value,
        "risk_level": plan.risk_level,
        "risk": plan.risk_json,
        "explain": plan.explain_json,
        "intent": plan.intent,
        "user_text": plan.user_text,
        "actor": plan.actor,
        "created_at": _dt(plan.created_at),
        "updated_at": _dt(plan.updated_at),
        "plan": plan.plan_json,
        "rejection_reason": plan.rejection_reason,
        "approved_at": _dt(plan.approved_at),
        "rejected_at": _dt(plan.rejected_at),
    }


def approve_plan(db_path: str, plan_id: str) -> dict[str, Any]:
    from gismo.core.models import PlanStatus
    from gismo.core.plan_store import enqueue_plan_actions

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is already {plan.status.value.lower()}")
        enqueued_ids, skipped = enqueue_plan_actions(store, plan.plan_json)
        store.approve_pending_plan(plan_id)

    return {"id": plan_id, "status": "APPROVED", "enqueued_ids": enqueued_ids, "skipped": skipped}


def reject_plan(db_path: str, plan_id: str, reason: str | None = None) -> dict[str, Any]:
    from gismo.core.models import PlanStatus

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is already {plan.status.value.lower()}")
        store.reject_pending_plan(plan_id, reason=reason)

    return {"id": plan_id, "status": "REJECTED", "reason": reason}


def patch_plan(
    db_path: str,
    plan_id: str,
    *,
    action_index: int | None = None,
    new_command: str | None = None,
    remove_action: bool = False,
) -> dict[str, Any]:
    from gismo.core.models import PlanStatus

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is {plan.status.value.lower()} and cannot be edited")

        if action_index is None:
            raise ValueError("action_index is required")

        new_plan = dict(plan.plan_json)
        actions = list(new_plan.get("actions", []))

        if action_index < 0 or action_index >= len(actions):
            raise ValueError(
                f"action_index {action_index} out of range (plan has {len(actions)} actions)"
            )

        if remove_action:
            actions.pop(action_index)
        elif new_command is not None:
            actions[action_index] = dict(actions[action_index])
            actions[action_index]["command"] = new_command
        else:
            raise ValueError("Provide new_command or remove_action=true")

        new_plan["actions"] = actions
        updated = store.update_pending_plan_json(plan_id, new_plan)

    return {
        "id": plan_id,
        "action_count": len(new_plan["actions"]),
        "plan": updated.plan_json if updated else new_plan,
    }


# ── Chat ───────────────────────────────────────────────────────────────────

_CHAT_HISTORY_FILE = Path(".gismo") / "chat_history.jsonl"


def _append_chat_record(message: str, reply: str) -> None:
    """Append a single user/assistant exchange to the JSONL history file."""
    import json

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": message,
        "assistant": reply,
    }
    try:
        _CHAT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _CHAT_HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # never let logging failures break the chat


def _clean_reply(text: str) -> str:
    """Strip model artefacts from a chat reply before display."""
    import re
    # Remove special tokens like <|end|>, <|assistant|>, <|im_end|>, etc.
    text = re.sub(r"<\|[^|>]*\|>", "", text)
    return text.strip()


def _build_chat_system(db_path: str) -> str:
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path) or "Operator"
    return (
        "I am GISMO, a local-first, policy-controlled personal AI assistant built by Mike Burns. "
        "I run entirely on your hardware — no cloud services, no silent actions, and a full audit trail of everything I do. "
        f"The operator's name is {name}. Address them as {name} when appropriate. "
        "My job is to help you manage tasks, queues, plans, runs, and memory on your own machine. "
        "I speak directly and concisely. I do not output JSON unless you ask for it. "
        "I never take actions outside what your operator policy explicitly permits."
    )


def chat_message(
    db_path: str,
    message: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    """Send a message to the local LLM and return the reply."""
    from gismo.llm.ollama import ollama_freeform_chat, OllamaError

    messages = list(history) + [{"role": "user", "content": message}]
    try:
        reply = ollama_freeform_chat(messages, system=_build_chat_system(db_path), model="gismo")
    except OllamaError as exc:
        raise RuntimeError(str(exc)) from exc
    reply = _clean_reply(reply)
    _append_chat_record(message, reply)
    return {"reply": reply}


# ── Onboarding ──────────────────────────────────────────────────────────────


def get_onboarding_status(db_path: str) -> dict[str, Any]:
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path)
    return {"needs_onboarding": name is None, "operator_name": name}


def complete_onboarding(db_path: str, name: str, voice_id: str) -> dict[str, Any]:
    from gismo.onboarding import set_operator_name
    from gismo.tts.prefs import set_voice
    from gismo.tts.voices import validate_voice

    name = name.strip() or "Operator"
    validate_voice(voice_id)
    set_operator_name(db_path, name)
    set_voice(db_path, voice_id)
    return {"ok": True, "name": name, "voice": voice_id}


# ── System health ──────────────────────────────────────────────────────────


def get_system_health() -> dict[str, Any]:
    """Return CPU and RAM usage percentages."""
    import psutil

    return {
        "cpu_percent": psutil.cpu_percent(),
        "virtual_memory": psutil.virtual_memory().percent,
    }


# ── Devices ────────────────────────────────────────────────────────────────


def _devices_file() -> Path:
    return Path(".gismo") / "devices.json"


def get_devices(db_path: str) -> list[dict[str, Any]]:
    import json
    try:
        with _devices_file().open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def add_device(db_path: str, name: str, device_type: str, address: str = "") -> dict[str, Any]:
    import json
    import uuid
    devices = get_devices(db_path)
    device: dict[str, Any] = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "type": device_type,
        "address": address,
        "status": "online",
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    devices.append(device)
    fp = _devices_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as fh:
        json.dump(devices, fh, ensure_ascii=False, indent=2)
    return device


# ── Activity feed ──────────────────────────────────────────────────────────


def get_activity_feed(db_path: str, limit: int = 40) -> list[dict[str, Any]]:
    """Merge recent queue items, runs, and chat history into a unified activity timeline."""
    import json as _json

    _Q_COLORS = {
        "QUEUED": "blue", "IN_PROGRESS": "teal",
        "SUCCEEDED": "green", "FAILED": "red", "CANCELLED": "gray",
    }
    _R_COLORS = {"succeeded": "green", "failed": "red", "running": "teal", "pending": "blue"}

    events: list[dict[str, Any]] = []

    for item in get_queue(db_path, limit=25):
        st = item["status"]
        label = (item.get("command_text") or "")[:60] or f"item/{item['id'][:8]}"
        events.append({
            "type": "queue",
            "label": label,
            "status": st,
            "color": _Q_COLORS.get(st, "gray"),
            "timestamp": item.get("updated_at") or item.get("created_at"),
        })

    for run in get_runs(db_path, limit=20):
        st = run["status"]
        events.append({
            "type": "run",
            "label": run.get("label") or f"run/{run['id'][:8]}",
            "status": st.upper(),
            "color": _R_COLORS.get(st, "gray"),
            "timestamp": run.get("created_at"),
        })

    # Pull recent chat exchanges from history file
    try:
        hist_path = _CHAT_HISTORY_FILE
        if hist_path.exists():
            lines = hist_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines[-20:]):
                rec = _json.loads(line)
                snippet = (rec.get("user") or "")[:50]
                events.append({
                    "type": "chat",
                    "label": snippet or "(empty)",
                    "status": "CHAT",
                    "color": "teal",
                    "timestamp": rec.get("timestamp"),
                })
    except Exception:
        pass

    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return events[:limit]


# ── Morning briefing ───────────────────────────────────────────────────────


def get_briefing(db_path: str) -> dict[str, Any]:
    """Generate a text briefing from current system state."""
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path) or "Operator"
    data = get_status(db_path)
    daemon = data.get("daemon", {})
    by_status = (data.get("queue") or {}).get("by_status", {})

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

    parts = [f"{greeting}, {name}."]
    queued  = by_status.get("QUEUED", 0)
    running = by_status.get("IN_PROGRESS", 0)
    failed  = by_status.get("FAILED", 0)
    done    = by_status.get("SUCCEEDED", 0)

    if running:
        parts.append(f"{running} task{'s' if running != 1 else ''} currently running.")
    if queued:
        parts.append(f"{queued} item{'s' if queued != 1 else ''} queued.")
    if failed:
        parts.append(f"\u26a0 {failed} failed item{'s' if failed != 1 else ''} need attention.")
    if not running and not queued and done:
        parts.append(f"All clear \u2014 {done} completed.")

    if not daemon.get("running"):
        parts.append("Daemon is offline.")
    elif daemon.get("paused"):
        parts.append("Daemon is paused.")
    else:
        parts.append("Systems nominal.")

    try:
        pending = get_plans(db_path, status="PENDING")
        if pending:
            parts.append(f"{len(pending)} plan{'s' if len(pending) != 1 else ''} awaiting approval.")
    except Exception:
        pass

    return {"name": name, "briefing": " ".join(parts)}


# ── TTS ────────────────────────────────────────────────────────────────────


def get_voices(db_path: str) -> dict[str, Any]:
    """Return available voices with download status and current preference."""
    from gismo.tts.voices import VOICES, DEFAULT_VOICE, is_downloaded
    from gismo.tts.prefs import get_voice

    current = get_voice(db_path)
    voice_list = [
        {
            "id": vid,
            "name": info["name"],
            "lang": info["lang"],
            "quality": info["quality"],
            "description": info["description"],
            "engine": info.get("engine", "piper"),
            "downloaded": is_downloaded(vid),
            "is_default": vid == DEFAULT_VOICE,
            "is_selected": vid == current,
        }
        for vid, info in VOICES.items()
    ]
    return {"voices": voice_list, "current": current}


def set_voice_preference(db_path: str, voice_id: str) -> dict[str, Any]:
    from gismo.tts.prefs import set_voice

    set_voice(db_path, voice_id)
    return {"voice": voice_id}


def tts_synthesize(db_path: str, text: str, voice_id: str | None = None) -> bytes:
    """Synthesize text and return WAV bytes. Downloads model if needed."""
    from gismo.tts.prefs import get_voice
    from gismo.tts.engine import synthesize

    if not voice_id:
        voice_id = get_voice(db_path)
    return synthesize(text, voice_id)
