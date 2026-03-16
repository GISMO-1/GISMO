"""GISMO web API — pure data layer (no HTTP).

All functions return JSON-serialisable dicts/lists and raise
``ValueError`` for bad input (404-class) or ``RuntimeError`` for
state errors.
"""
from __future__ import annotations

from datetime import datetime, timezone
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

_CHAT_SYSTEM = (
    "I am GISMO, a local-first, policy-controlled personal AI assistant built by Mike Burns. "
    "I run entirely on your hardware — no cloud services, no silent actions, and a full audit trail of everything I do. "
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
        reply = ollama_freeform_chat(messages, system=_CHAT_SYSTEM, model="gismo")
    except OllamaError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"reply": reply}


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
