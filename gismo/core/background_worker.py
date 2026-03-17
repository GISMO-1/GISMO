"""Helpers for ensuring the GISMO worker process is running."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from gismo.core.state import StateStore

STALE_SECONDS = 30


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


def _worker_is_healthy(db_path: str) -> bool:
    with StateStore(db_path) as store:
        heartbeat = store.get_daemon_heartbeat()
    if heartbeat is None:
        return False
    last_seen = heartbeat.last_seen
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - last_seen).total_seconds()))
    if age_seconds <= STALE_SECONDS:
        return True
    return _pid_is_running(heartbeat.pid)


def ensure_background_worker(db_path: str) -> bool:
    """Start the GISMO worker if no healthy worker is already running."""
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    if _worker_is_healthy(str(db_file)):
        return False

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
    subprocess.Popen(argv, **kwargs)
    return True
