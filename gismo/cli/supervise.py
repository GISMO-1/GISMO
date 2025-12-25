"""Supervisor helpers for running IPC + daemon together."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from gismo.cli import ipc as ipc_cli


@dataclass(frozen=True)
class SupervisorRecord:
    ipc_pid: int
    daemon_pid: int
    db_path: str
    started_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ipc_pid": self.ipc_pid,
            "daemon_pid": self.daemon_pid,
            "db_path": self.db_path,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SupervisorRecord":
        return cls(
            ipc_pid=int(data["ipc_pid"]),
            daemon_pid=int(data["daemon_pid"]),
            db_path=str(data["db_path"]),
            started_at=str(data["started_at"]),
        )


@dataclass(frozen=True)
class SupervisorProcessStatus:
    ipc_running: bool
    daemon_running: bool


class ProcessOps(Protocol):
    def spawn(self, argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        """Spawn a child process."""

    def is_running(self, pid: int) -> bool:
        """Check if a PID is running."""

    def terminate(self, pid: int) -> None:
        """Terminate a PID."""

    def kill(self, pid: int) -> None:
        """Kill a PID."""


class DefaultProcessOps:
    def spawn(self, argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        kwargs: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(argv, **kwargs)

    def is_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def terminate(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    def kill(self, pid: int) -> None:
        sig = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
        os.kill(pid, sig)


def default_pid_path() -> Path:
    return Path(".gismo") / "supervise.json"


def load_supervisor_record(path: Path) -> SupervisorRecord | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Supervisor PID file is invalid.")
    return SupervisorRecord.from_dict(data)


def save_supervisor_record(path: Path, record: SupervisorRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")


def summarize_supervisor_status(
    record: SupervisorRecord,
    process_ops: ProcessOps,
) -> SupervisorProcessStatus:
    return SupervisorProcessStatus(
        ipc_running=process_ops.is_running(record.ipc_pid),
        daemon_running=process_ops.is_running(record.daemon_pid),
    )


def run_supervise_up(
    db_path: str,
    token: str,
    *,
    pid_path: Path | None = None,
    process_ops: ProcessOps | None = None,
) -> None:
    process_ops = process_ops or DefaultProcessOps()
    pid_path = pid_path or default_pid_path()
    existing = load_supervisor_record(pid_path)
    if existing is not None:
        status = summarize_supervisor_status(existing, process_ops)
        if status.ipc_running or status.daemon_running:
            print("Supervisor already running.")
            return
        pid_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["GISMO_IPC_TOKEN"] = token
    env["PYTHONUNBUFFERED"] = "1"
    ipc_args = [
        sys.executable,
        "-m",
        "gismo.cli.main",
        "ipc",
        "serve",
        "--db",
        db_path,
        "--token",
        token,
    ]
    daemon_args = [
        sys.executable,
        "-m",
        "gismo.cli.main",
        "daemon",
        "--db",
        db_path,
    ]

    ipc_proc = process_ops.spawn(ipc_args, env=env)
    daemon_proc = process_ops.spawn(daemon_args, env=env)
    record = SupervisorRecord(
        ipc_pid=ipc_proc.pid,
        daemon_pid=daemon_proc.pid,
        db_path=db_path,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    save_supervisor_record(pid_path, record)

    stop_event = threading.Event()
    threads = [
        _start_output_thread(ipc_proc, "[ipc]", stop_event),
        _start_output_thread(daemon_proc, "[daemon]", stop_event),
    ]

    try:
        while True:
            if ipc_proc.poll() is not None or daemon_proc.poll() is not None:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        _terminate_process(ipc_proc.pid, process_ops)
        _terminate_process(daemon_proc.pid, process_ops)
        pid_path.unlink(missing_ok=True)
        for thread in threads:
            thread.join(timeout=1.0)


def run_supervise_status(
    token: str,
    *,
    db_path: str | None = None,
    pid_path: Path | None = None,
    process_ops: ProcessOps | None = None,
) -> None:
    process_ops = process_ops or DefaultProcessOps()
    pid_path = pid_path or default_pid_path()
    record = load_supervisor_record(pid_path)
    if record is None:
        print("not running")
        return
    status = summarize_supervisor_status(record, process_ops)
    lines = [
        "GISMO supervise status",
        f"pid_file: {pid_path}",
        f"db_path: {record.db_path}",
        f"ipc_pid: {record.ipc_pid} ({_fmt_running(status.ipc_running)})",
        f"daemon_pid: {record.daemon_pid} ({_fmt_running(status.daemon_running)})",
    ]
    if db_path and db_path != record.db_path:
        lines.append(f"db_path_mismatch: requested={db_path}")
    ipc_reachable = False
    ipc_error = None
    try:
        ping = ipc_cli.parse_ipc_response(ipc_cli.ipc_request("ping", {}, token))
        ipc_reachable = ping.ok
        if not ping.ok:
            ipc_error = ping.error or "unknown"
    except ipc_cli.IPCConnectionError:
        ipc_error = "connection_failed"
    lines.append(f"ipc_ping: {_fmt_ping(ipc_reachable, ipc_error)}")
    if ipc_reachable:
        daemon_status = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_status", {}, token)
        )
        if daemon_status.ok:
            paused = bool(daemon_status.data and daemon_status.data.get("paused"))
            lines.append(f"daemon_paused: {paused}")
        else:
            lines.append(f"daemon_paused: error ({daemon_status.error})")
    print("\n".join(lines))


def run_supervise_down(
    *,
    pid_path: Path | None = None,
    process_ops: ProcessOps | None = None,
) -> None:
    process_ops = process_ops or DefaultProcessOps()
    pid_path = pid_path or default_pid_path()
    record = load_supervisor_record(pid_path)
    if record is None:
        print("not running")
        return
    _terminate_process(record.ipc_pid, process_ops)
    _terminate_process(record.daemon_pid, process_ops)
    pid_path.unlink(missing_ok=True)
    print("stopped")


def _terminate_process(pid: int, process_ops: ProcessOps, timeout_seconds: float = 5.0) -> None:
    if not process_ops.is_running(pid):
        return
    try:
        process_ops.terminate(pid)
    except OSError:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_ops.is_running(pid):
            return
        time.sleep(0.1)
    try:
        process_ops.kill(pid)
    except OSError:
        return


def _start_output_thread(
    process: subprocess.Popen[str],
    prefix: str,
    stop_event: threading.Event,
) -> threading.Thread:
    def _runner() -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            if stop_event.is_set():
                break
            print(f"{prefix} {line.rstrip()}")
        stream.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


def _fmt_running(value: bool) -> str:
    return "running" if value else "stopped"


def _fmt_ping(ok: bool, error: str | None) -> str:
    if ok:
        return "ok"
    if error:
        return f"error ({error})"
    return "error"
