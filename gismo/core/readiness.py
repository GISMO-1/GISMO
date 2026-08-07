"""Central readiness/status helpers for operator-facing GISMO surfaces."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from gismo.core.background_worker import STALE_SECONDS, get_background_worker_status
from gismo.core.models import PlanStatus, QueueStatus
from gismo.core.state import StateStore
from gismo.llm.model_policy import get_model_health, load_model_policy, peek_model_discovery
from gismo.onboarding import get_operator_name

LOGGER = logging.getLogger(__name__)

_AUTOSTART_EVENT_TYPES = (
    "daemon_autostart_failed",
    "daemon_autostart_started",
    "daemon_autostart_skipped",
)
_AUTOSTART_FAILURE_WINDOW_SECONDS = 300
_MIN_RUNNING_STALE_SECONDS = 120
_RUNNING_GRACE_SECONDS = 15
# If the daemon has a last_seen heartbeat but is not running and the heartbeat
# is older than this threshold, "starting" transitions to "degraded" so the
# operator gets a clear signal that something went wrong.  4 × STALE_SECONDS
# gives the daemon two full stale windows of grace before we escalate.
_STARTING_STALE_SECONDS = 4 * STALE_SECONDS  # 120 s by default
_STUCK_STARTING_EVENT_TYPE = "daemon_stuck_starting"


def build_runtime_status(
    db_path: str,
    *,
    model_probe: str = "cached",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state_error = False
    try:
        with StateStore(db_path) as store:
            queue_stats = store.queue_stats()
            pending_plans = list(store.list_pending_plans(status=PlanStatus.PENDING, limit=100))
            running_items = list(store.list_queue_items_by_status(QueueStatus.IN_PROGRESS))
            recent_events = store.list_events(limit=80)
    except Exception:  # noqa: BLE001 - readiness must report state failure, not claim success
        LOGGER.exception("readiness_state_query_failed")
        state_error = True
        queue_stats = {"total": 0, "by_status": {}}
        pending_plans = []
        running_items = []
        recent_events = []
    database = {
        "ready": not state_error,
        "state": "ready" if not state_error else "blocked",
        "detail": "Ready" if not state_error else "State is unavailable",
    }
    try:
        daemon = get_background_worker_status(db_path).to_dict()
        daemon["available"] = bool(daemon)
    except Exception:  # noqa: BLE001 - surface the worker-status failure deterministically
        LOGGER.exception("readiness_worker_status_failed")
        daemon = {
            "running": False,
            "stale": False,
            "paused": False,
            "pid": None,
            "started_at": None,
            "last_seen": None,
            "age_seconds": None,
            "available": False,
        }
    daemon["age_secs"] = daemon.get("age_seconds")
    daemon["state"] = _daemon_state_label(daemon)
    starting_is_stale = _is_starting_stale(daemon, now)
    if starting_is_stale and not _recent_stuck_event(recent_events, now):
        _emit_stuck_starting_event(db_path, daemon=daemon)
    try:
        onboarding = _build_onboarding_status(db_path)
    except Exception:  # noqa: BLE001 - setup state is part of readiness reporting
        LOGGER.exception("readiness_onboarding_status_failed")
        onboarding = {
            "completed": False,
            "needs_onboarding": False,
            "operator_name": None,
            "state": "blocked",
            "detail": "Setup state is unavailable",
        }
    startup = _build_startup_status(recent_events, now=now)
    queue = _build_queue_status(queue_stats, running_items, now=now)
    try:
        models = _build_model_status(db_path, probe=model_probe)
    except Exception:  # noqa: BLE001 - model configuration failure is not readiness
        LOGGER.exception("readiness_model_status_failed")
        models = {
            "state": "degraded",
            "detail": "Model status is unavailable",
            "required": False,
            "blocking": False,
            "health": None,
        }
    if state_error:
        approval_state = "unavailable"
        approval_detail = "Approval state is unavailable."
    elif pending_plans:
        approval_state = "needs_approval"
        approval_detail = (
            f"{len(pending_plans)} request"
            f"{'s' if len(pending_plans) != 1 else ''} need approval."
        )
    else:
        approval_state = "ready"
        approval_detail = "No approvals are waiting."
    approvals = {
        "pending_count": len(pending_plans),
        "state": approval_state,
        "detail": approval_detail,
    }
    gismo = _build_gismo_status(
        daemon=daemon,
        queue=queue,
        approvals=approvals,
        onboarding=onboarding,
        models=models,
        startup=startup,
        database=database,
        starting_is_stale=starting_is_stale,
    )
    api = {
        "ready": bool(database["ready"]),
        "state": "ready" if database["ready"] else "degraded",
        "detail": "Listening" if database["ready"] else "State access failed",
    }
    stages = [
        {
            "key": "state",
            "label": "State",
            "ready": bool(database["ready"]),
            "detail": database["detail"],
        },
        {
            "key": "worker",
            "label": "Worker",
            "ready": bool(daemon["running"] and not daemon["stale"]),
            "detail": _worker_detail(daemon, startup),
        },
        {
            "key": "setup",
            "label": "Setup",
            "ready": onboarding["completed"],
            "detail": onboarding["detail"],
        },
        {
            "key": "model",
            "label": "Model",
            "ready": not bool(models.get("blocking")),
            "detail": models["detail"],
        },
        {
            "key": "api",
            "label": "API",
            "ready": bool(api["ready"]),
            "detail": api["detail"],
        },
    ]
    return {
        "daemon": daemon,
        "queue": queue,
        "database": database,
        "api": api,
        "startup": startup,
        "onboarding": onboarding,
        "approvals": approvals,
        "models": models,
        "gismo": gismo,
        "readiness": {
            "ready": bool(gismo["ready"]),
            "surface_ready": bool(gismo["surface_ready"]),
            "gismo_state": gismo["state"],
            "summary": gismo["summary"],
            "stages": stages,
        },
    }


def build_readiness_payload(db_path: str) -> dict[str, Any]:
    return dict(build_runtime_status(db_path, model_probe="cached")["readiness"])


def _build_onboarding_status(db_path: str) -> dict[str, Any]:
    name = get_operator_name(db_path)
    completed = bool(name and str(name).strip())
    return {
        "completed": completed,
        "needs_onboarding": not completed,
        "operator_name": name,
        "state": "ready" if completed else "blocked",
        "detail": "Complete" if completed else "Needs setup",
    }


def _build_startup_status(events: list[Any], *, now: datetime) -> dict[str, Any]:
    selected = next(
        (event for event in events if getattr(event, "event_type", None) in _AUTOSTART_EVENT_TYPES),
        None,
    )
    if selected is None:
        return {
            "state": "unknown",
            "detail": "No recent startup event.",
            "recent_failure": False,
            "recent_start": False,
            "event_type": None,
            "timestamp": None,
            "payload": {},
        }
    timestamp = selected.ts
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((now - timestamp).total_seconds()))
    recent_failure = (
        selected.event_type == "daemon_autostart_failed"
        and age_seconds <= _AUTOSTART_FAILURE_WINDOW_SECONDS
    )
    recent_start = (
        selected.event_type == "daemon_autostart_started"
        and age_seconds <= _AUTOSTART_FAILURE_WINDOW_SECONDS
    )
    detail = selected.message
    if isinstance(selected.json_payload, dict):
        source = str(selected.json_payload.get("source") or "").strip()
        if source:
            detail = f"{selected.message} ({source})"
    return {
        "state": (
            "failed"
            if selected.event_type == "daemon_autostart_failed"
            else "started"
            if selected.event_type == "daemon_autostart_started"
            else "healthy"
        ),
        "detail": detail,
        "recent_failure": recent_failure,
        "recent_start": recent_start,
        "event_type": selected.event_type,
        "timestamp": timestamp.isoformat(),
        "payload": selected.json_payload or {},
    }


def _build_queue_status(queue_stats: dict[str, Any], running_items: list[Any], *, now: datetime) -> dict[str, Any]:
    by_status = dict(queue_stats.get("by_status") or {})
    stale_running = 0
    for item in running_items:
        started_at = getattr(item, "started_at", None)
        if started_at is None:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - started_at).total_seconds()))
        timeout_seconds = max(
            int(getattr(item, "timeout_seconds", 0) or 0),
            _MIN_RUNNING_STALE_SECONDS,
        )
        if age_seconds > timeout_seconds + _RUNNING_GRACE_SECONDS:
            stale_running += 1
    return {
        "total": int(queue_stats.get("total") or 0),
        "by_status": by_status,
        "queued": int(by_status.get("QUEUED") or 0),
        "running": int(by_status.get("IN_PROGRESS") or 0),
        "succeeded": int(by_status.get("SUCCEEDED") or 0),
        "failed": int(by_status.get("FAILED") or 0),
        "cancelled": int(by_status.get("CANCELLED") or 0),
        "stale_running": stale_running,
    }


def _build_model_status(db_path: str, *, probe: str) -> dict[str, Any]:
    if probe == "cached":
        cached = peek_model_discovery()
        if cached is None:
            policy = load_model_policy(db_path)
            return {
                "state": "unknown",
                "detail": "Checking availability",
                "required": bool(policy.primary_assistant_model),
                "blocking": False,
                "health": {"policy": policy.to_dict(), "probe": "cached"},
            }
        return _build_cached_model_status(db_path, cached)
    health = get_model_health(db_path)
    issues = list(health.get("issues") or [])
    degraded = bool((health.get("degraded_mode") or {}).get("active"))
    ollama_available = bool(health.get("ollama_available"))
    policy = dict(health.get("policy") or {})
    required = bool(str(policy.get("primary_assistant_model") or "").strip())
    if degraded:
        state = "degraded"
        detail = str((health.get("degraded_mode") or {}).get("reason") or "Reduced mode")
    elif not ollama_available:
        state = "offline"
        detail = "Model service unavailable"
    elif issues:
        state = "degraded"
        detail = str(issues[0])
    else:
        state = "ready"
        detail = "Ready"
    return {
        "state": state,
        "detail": detail,
        "required": required,
        "blocking": bool(required and (degraded or not ollama_available)),
        "health": health,
    }


def _build_cached_model_status(db_path: str, discovery: dict[str, Any]) -> dict[str, Any]:
    policy = load_model_policy(db_path)
    installed = set(discovery.get("installed_models") or [])
    required = bool(policy.primary_assistant_model)
    blocking = False
    if not discovery.get("ollama_available"):
        state = "offline"
        detail = "Model service unavailable"
        blocking = required
    elif policy.primary_assistant_model not in installed:
        state = "degraded"
        detail = "Main model is not installed"
        blocking = required
    elif policy.planner_model not in installed:
        state = "degraded"
        detail = "Planner model is not installed"
    elif policy.helper_model and policy.helper_model not in installed:
        state = "degraded"
        detail = "Helper model is not installed"
    else:
        state = "ready"
        detail = "Ready"
    return {
        "state": state,
        "detail": detail,
        "required": required,
        "blocking": blocking,
        "health": {
            "installed_models": list(discovery.get("installed_models") or []),
            "loaded_models": list(discovery.get("loaded_models") or []),
            "ollama_available": bool(discovery.get("ollama_available")),
            "policy": policy.to_dict(),
            "probe": "cached",
        },
    }


def _build_gismo_status(
    *,
    daemon: dict[str, Any],
    queue: dict[str, Any],
    approvals: dict[str, Any],
    onboarding: dict[str, Any],
    models: dict[str, Any],
    startup: dict[str, Any],
    database: dict[str, Any],
    starting_is_stale: bool = False,
) -> dict[str, Any]:
    daemon_available = bool(
        daemon.get(
            "available",
            all(key in daemon for key in ("running", "stale", "paused")),
        )
    )
    worker_ready = bool(
        daemon_available
        and daemon.get("running")
        and not daemon.get("stale")
    )
    state = "ready"
    detail = "Ready."
    if not database.get("ready"):
        state = "blocked"
        detail = "Local state is unavailable."
    elif not daemon_available:
        state = "offline"
        detail = "Background service status is unavailable."
    elif not worker_ready:
        if startup["recent_failure"]:
            state = "blocked"
            detail = "Background service could not start."
        elif daemon.get("stale") and daemon.get("running"):
            state = "degraded"
            detail = "Background service is not responding."
        elif daemon["last_seen"]:
            if starting_is_stale:
                state = "degraded"
                detail = "Background service is not responding."
            else:
                state = "starting"
                detail = "Background service is reconnecting."
        elif startup.get("recent_start"):
            state = "starting"
            detail = "Background service is starting."
        else:
            state = "offline"
            detail = "Background service is offline."
    elif bool(daemon["paused"]):
        state = "blocked"
        detail = "Work is paused."
    elif onboarding["needs_onboarding"]:
        state = "blocked"
        detail = "Finish setup to continue."
    elif approvals["pending_count"]:
        state = "approval_needed"
        detail = approvals["detail"]
    elif queue["stale_running"] > 0:
        state = "degraded"
        detail = "Some work may be stuck."
    elif queue["failed"] > 0:
        state = "degraded"
        detail = (
            f"{queue['failed']} failed item{'s' if queue['failed'] != 1 else ''} need attention."
        )
    elif models["state"] not in {"ready", "unknown"}:
        state = "degraded"
        detail = models["detail"]
    surface_ready = bool(
        database.get("ready")
        and worker_ready
        and not models.get("blocking", False)
    )
    summary = _build_summary(
        state=state,
        detail=detail,
        queue=queue,
        daemon=daemon,
        approvals=approvals,
        onboarding=onboarding,
        database=database,
    )
    return {
        "state": state,
        "label": _state_label(state),
        "ready": state == "ready",
        "surface_ready": surface_ready,
        "working": queue["running"] > 0,
        "paused": bool(daemon["paused"]),
        "approval_needed": approvals["pending_count"] > 0,
        "detail": detail,
        "summary": summary,
    }


def _is_starting_stale(daemon: dict[str, Any], now: datetime) -> bool:
    """Return True when the daemon has a stale heartbeat and is overdue to resume.

    This detects the case where the daemon was seen before (``last_seen`` is set)
    but is no longer running and has not produced a new heartbeat within
    ``_STARTING_STALE_SECONDS``.  The ordinary "starting" grace period uses
    ``STALE_SECONDS``; we give the daemon four full intervals before escalating.
    """
    if daemon.get("running") or not daemon.get("last_seen"):
        return False
    try:
        last_seen = datetime.fromisoformat(daemon["last_seen"])
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - last_seen).total_seconds()))
        return age_seconds > _STARTING_STALE_SECONDS
    except (ValueError, TypeError):
        return False


def _recent_stuck_event(events: list[Any], now: datetime) -> bool:
    """Return True when a stuck-starting event was already emitted within the stale window.

    Prevents duplicate events on every status poll when the daemon is stuck.
    """
    for event in events:
        if getattr(event, "event_type", None) != _STUCK_STARTING_EVENT_TYPE:
            continue
        ts = getattr(event, "ts", None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = max(0, int((now - ts).total_seconds()))
        if age <= _STARTING_STALE_SECONDS:
            return True
    return False


def _emit_stuck_starting_event(db_path: str, *, daemon: dict[str, Any]) -> None:
    """Write a security event when the stuck-starting threshold is crossed.

    Opens a new StateStore connection rather than re-using the caller's context
    so the event is flushed immediately and is visible on the next poll.
    Silently swallows exceptions so a write failure never prevents readiness
    from being served.
    """
    try:
        with StateStore(db_path) as store:
            store.record_event(
                actor="system",
                event_type=_STUCK_STARTING_EVENT_TYPE,
                message="Background service did not resume within the expected window.",
                json_payload={
                    "last_seen": daemon.get("last_seen"),
                    "age_seconds": daemon.get("age_seconds"),
                    "stale_threshold_seconds": _STARTING_STALE_SECONDS,
                },
            )
    except Exception:  # noqa: BLE001 - never raise from readiness computation
        pass


def _build_summary(
    *,
    state: str,
    detail: str,
    queue: dict[str, Any],
    daemon: dict[str, Any],
    approvals: dict[str, Any],
    onboarding: dict[str, Any],
    database: dict[str, Any] | None = None,
) -> str:
    if state == "ready":
        if queue["running"] > 0:
            return f"GISMO is ready and handling {queue['running']} active task{'s' if queue['running'] != 1 else ''}."
        return "GISMO is ready."
    if state == "approval_needed":
        return approvals["detail"]
    if database is not None and not database.get("ready"):
        return detail
    if state == "blocked" and onboarding["needs_onboarding"]:
        return "Finish setup to continue."
    if state == "blocked" and daemon["paused"]:
        return "GISMO is paused."
    if state == "starting":
        return "GISMO is starting up."
    if state == "offline":
        return "GISMO is offline."
    return detail


def _worker_detail(daemon: dict[str, Any], startup: dict[str, Any]) -> str:
    daemon_available = bool(
        daemon.get(
            "available",
            all(key in daemon for key in ("running", "stale", "paused")),
        )
    )
    if not daemon_available:
        return "Status unavailable"
    if daemon["running"] and not daemon["stale"]:
        if daemon["paused"]:
            return "Paused"
        age = daemon.get("age_seconds")
        if age is None:
            return "Running"
        return f"Running ({age}s heartbeat)"
    if startup["recent_failure"]:
        return startup["detail"]
    if daemon.get("last_seen"):
        return f"Waiting for heartbeat ({STALE_SECONDS}s stale window)"
    return "Waiting to start"


def _state_label(state: str) -> str:
    labels = {
        "ready": "Ready",
        "starting": "Starting",
        "degraded": "Degraded",
        "approval_needed": "Approval Needed",
        "blocked": "Blocked",
        "offline": "Offline",
    }
    return labels.get(state, state.replace("_", " ").title())


def _daemon_state_label(daemon: dict[str, Any]) -> str:
    if daemon["running"] and not daemon["stale"]:
        return "online"
    if daemon.get("last_seen"):
        return "starting"
    return "offline"
