"""Helpers for ensuring the GISMO worker process is running."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gismo.core.state import StateStore
from gismo.core.paths import normalize_database_path

STALE_SECONDS = 30
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundWorkerStatus:
    running: bool
    stale: bool
    paused: bool
    pid: int | None
    started_at: str | None
    last_seen: str | None
    age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stale": self.stale,
            "paused": self.paused,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_seen": self.last_seen,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class BackgroundWorkerStartResult:
    started: bool
    already_running: bool
    healthy: bool
    failed: bool
    source: str
    reason: str | None = None
    worker_status: BackgroundWorkerStatus | None = None
    event_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "already_running": self.already_running,
            "healthy": self.healthy,
            "failed": self.failed,
            "source": self.source,
            "reason": self.reason,
            "event_type": self.event_type,
            "worker_status": (
                self.worker_status.to_dict()
                if self.worker_status is not None
                else None
            ),
        }


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def get_background_worker_status(db_path: str) -> BackgroundWorkerStatus:
    with StateStore(db_path) as store:
        heartbeat = store.get_daemon_heartbeat()
        paused = store.get_daemon_paused()
    if heartbeat is None:
        return BackgroundWorkerStatus(
            running=False,
            stale=False,
            paused=paused,
            pid=None,
            started_at=None,
            last_seen=None,
            age_seconds=None,
        )
    last_seen = heartbeat.last_seen
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - last_seen).total_seconds()))
    stale = age_seconds > STALE_SECONDS
    running = not stale or _pid_is_running(heartbeat.pid)
    return BackgroundWorkerStatus(
        running=running,
        stale=stale and not running,
        paused=paused,
        pid=heartbeat.pid,
        started_at=_isoformat(getattr(heartbeat, "started_at", None)),
        last_seen=_isoformat(heartbeat.last_seen),
        age_seconds=age_seconds,
    )


def _worker_is_healthy(db_path: str) -> bool:
    return get_background_worker_status(db_path).running


def ensure_background_worker_status(
    db_path: str,
    *,
    source: str = "system",
) -> BackgroundWorkerStartResult:
    """Start the GISMO worker if no healthy worker is already running."""
    db_file = Path(normalize_database_path(db_path))
    db_file.parent.mkdir(parents=True, exist_ok=True)
    if _worker_is_healthy(str(db_file)):
        status = get_background_worker_status(str(db_file))
        _record_autostart_event(
            str(db_file),
            event_type="daemon_autostart_skipped",
            message="Background service already running.",
            payload={
                "source": source,
                "healthy": True,
                "worker_status": status.to_dict(),
            },
        )
        return BackgroundWorkerStartResult(
            started=False,
            already_running=True,
            healthy=True,
            failed=False,
            source=source,
            worker_status=status,
            event_type="daemon_autostart_skipped",
        )
    status = get_background_worker_status(str(db_file))

    argv = [sys.executable, "-m", "gismo.cli.main", "daemon", "--db", str(db_file)]
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": {**os.environ, "GISMO_AUTO_STARTED": "1"},
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        LOGGER.exception("daemon_autostart_failed source=%s", source)
        _record_autostart_event(
            str(db_file),
            event_type="daemon_autostart_failed",
            message="Background service could not start.",
            payload={
                "source": source,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return BackgroundWorkerStartResult(
            started=False,
            already_running=False,
            healthy=False,
            failed=True,
            source=source,
            reason=str(exc),
            worker_status=get_background_worker_status(str(db_file)),
            event_type="daemon_autostart_failed",
        )

    _record_autostart_event(
        str(db_file),
        event_type="daemon_autostart_started",
        message="Background service started.",
        payload={
            "source": source,
            "pid": int(process.pid) if isinstance(getattr(process, "pid", None), int) else None,
            "argv": argv,
        },
    )
    return BackgroundWorkerStartResult(
        started=True,
        already_running=False,
        healthy=False,
        failed=False,
        source=source,
        worker_status=get_background_worker_status(str(db_file)),
        event_type="daemon_autostart_started",
    )


def ensure_background_worker(db_path: str) -> bool:
    return ensure_background_worker_status(db_path).started


def _record_autostart_event(
    db_path: str,
    *,
    event_type: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    try:
        with StateStore(db_path) as store:
            store.record_event(
                actor="system",
                event_type=event_type,
                message=message,
                json_payload=payload,
            )
    except Exception:
        LOGGER.exception("daemon_autostart_event_failed type=%s", event_type)
