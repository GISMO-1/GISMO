"""Windows Task Scheduler service management for GISMO daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gismo.cli.windows_tasks import (
    WindowsTaskConfig,
    build_daemon_command,
    build_schtasks_delete_args,
    install_windows_task,
)
from gismo.core.background_worker import get_background_worker_status
from gismo.core.security_events import append_security_event

GISMO_TASK_NAME = "GISMO-Daemon"


def _pythonw_exe() -> str:
    """Return the pythonw.exe path for the current interpreter."""
    exe = sys.executable
    base = os.path.basename(exe).lower()
    if base.startswith("python") and not base.startswith("pythonw"):
        return os.path.join(os.path.dirname(exe), base.replace("python", "pythonw", 1))
    return exe


def _task_exists(task_name: str) -> bool:
    """Check whether a Windows Task Scheduler task exists."""
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _query_task(task_name: str) -> subprocess.CompletedProcess[str] | None:
    """Query task details, returning the CompletedProcess or None."""
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result
    except FileNotFoundError:
        return None


def run_service_install(
    db_path: str,
    *,
    policy_path: str | None = None,
    run_subprocess: object = None,
) -> None:
    """Install the GISMO daemon as a Windows Task Scheduler task.

    Policy-gated: requires ``service.install`` in allowed_tools.
    Emits a ``service_install`` security event on success.
    """
    _ensure_windows()
    _check_policy(policy_path, "service.install")

    if _task_exists(GISMO_TASK_NAME):
        print(f"Task '{GISMO_TASK_NAME}' already exists.")
        print("Run 'gismo service uninstall' first, then re-install.")
        return

    pythonw = _pythonw_exe()
    command = build_daemon_command(pythonw, db_path)
    config = WindowsTaskConfig(
        name=GISMO_TASK_NAME,
        db_path=db_path,
        python_exe=pythonw,
        force=False,
    )

    install_windows_task(config)

    schtasks_args = ["schtasks.exe", "/Create", "/TN", GISMO_TASK_NAME, "/XML", "<generated>"]
    append_security_event(
        event_type="service_install",
        actor="operator",
        action="install",
        resource=GISMO_TASK_NAME,
        payload={
            "task_name": GISMO_TASK_NAME,
            "db_path": db_path,
            "python_exe": pythonw,
            "command": command,
            "schtasks_args": schtasks_args,
        },
        db_path=db_path,
    )
    print(f"Installed scheduled task '{GISMO_TASK_NAME}'.")


def run_service_uninstall(
    db_path: str,
    *,
    policy_path: str | None = None,
    run_subprocess: object = None,
) -> None:
    """Uninstall the GISMO daemon scheduled task.

    Policy-gated: requires ``service.uninstall`` in allowed_tools.
    Emits a ``service_uninstall`` security event on success.
    """
    _ensure_windows()
    _check_policy(policy_path, "service.uninstall")

    if not _task_exists(GISMO_TASK_NAME):
        print(f"Task '{GISMO_TASK_NAME}' is not installed.")
        return

    delete_args = build_schtasks_delete_args(GISMO_TASK_NAME)
    subprocess.run(delete_args, check=True, capture_output=True, text=True)

    append_security_event(
        event_type="service_uninstall",
        actor="operator",
        action="uninstall",
        resource=GISMO_TASK_NAME,
        payload={
            "task_name": GISMO_TASK_NAME,
            "schtasks_args": delete_args,
        },
        db_path=db_path,
    )
    print(f"Removed scheduled task '{GISMO_TASK_NAME}'.")


def run_service_status(db_path: str) -> None:
    """Show scheduled task status and daemon heartbeat."""
    result = _query_task(GISMO_TASK_NAME)
    if result is None:
        print(f"Task '{GISMO_TASK_NAME}' is not installed.")
    else:
        print(f"Task '{GISMO_TASK_NAME}' is installed.")
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            print(f"  {line}")

    worker = get_background_worker_status(db_path)
    if worker.running:
        state = "running"
    elif worker.stale:
        state = "stale"
    else:
        state = "offline"
    print(f"Daemon heartbeat: {state}")
    if worker.pid is not None:
        print(f"  PID: {worker.pid}")
    if worker.last_seen is not None:
        print(f"  Last seen: {worker.last_seen}")


def _ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Service commands are only supported on Windows.")


def _check_policy(policy_path: str | None, tool_name: str) -> None:
    """Deny-by-default policy gate for service operations."""
    if policy_path is None:
        raise PermissionError(
            f"No policy loaded; '{tool_name}' is denied by default. "
            f"Provide a policy that includes '{tool_name}' in allowed_tools."
        )
    from gismo.core.permissions import load_policy
    policy = load_policy(policy_path, repo_root=Path("."))
    policy.check_tool_allowed(tool_name)
