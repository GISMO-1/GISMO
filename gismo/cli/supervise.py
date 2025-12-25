"""Supervisor helpers for running IPC + daemon together."""
from __future__ import annotations

import ctypes
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
from ctypes import wintypes
from typing import Protocol

from gismo.cli import ipc as ipc_cli


@dataclass(frozen=True)
class SupervisorRecord:
    ipc_pid: int
    daemon_pid: int
    ipc_started: bool
    ipc_reused: bool
    daemon_started: bool
    db_path: str
    started_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ipc_pid": self.ipc_pid,
            "daemon_pid": self.daemon_pid,
            "ipc_started": self.ipc_started,
            "ipc_reused": self.ipc_reused,
            "daemon_started": self.daemon_started,
            "db_path": self.db_path,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SupervisorRecord":
        return cls(
            ipc_pid=int(data.get("ipc_pid", 0)),
            daemon_pid=int(data.get("daemon_pid", 0)),
            ipc_started=bool(data.get("ipc_started", True)),
            ipc_reused=bool(data.get("ipc_reused", False)),
            daemon_started=bool(data.get("daemon_started", True)),
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
        if os.name == "nt":
            return _windows_is_running(pid)
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def terminate(self, pid: int) -> None:
        if os.name == "nt":
            _windows_terminate(pid)
            return
        os.kill(pid, signal.SIGTERM)

    def kill(self, pid: int) -> None:
        if os.name == "nt":
            _windows_terminate(pid)
            return
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

    ipc_reachable, ipc_authorized = _probe_ipc_server(token)
    if ipc_reachable and not ipc_authorized:
        print("IPC authorization failed. Ensure the supervisor token matches the running IPC server.")
        raise SystemExit(1)

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

    ipc_proc = None
    ipc_pid = 0
    ipc_started = False
    ipc_reused = False
    if ipc_reachable and ipc_authorized:
        print("[supervise] IPC already running; reusing existing server.")
        ipc_reused = True
    else:
        ipc_proc = process_ops.spawn(ipc_args, env=env)
        ipc_pid = ipc_proc.pid
        ipc_started = True

    daemon_proc = process_ops.spawn(daemon_args, env=env)
    record = SupervisorRecord(
        ipc_pid=ipc_pid,
        daemon_pid=daemon_proc.pid,
        ipc_started=ipc_started,
        ipc_reused=ipc_reused,
        daemon_started=True,
        db_path=db_path,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    save_supervisor_record(pid_path, record)

    stop_event = threading.Event()
    threads = []
    if ipc_proc is not None:
        threads.append(_start_output_thread(ipc_proc, "[ipc]", stop_event))
    threads.append(_start_output_thread(daemon_proc, "[daemon]", stop_event))

    processes = [proc for proc in (ipc_proc, daemon_proc) if proc is not None]
    try:
        while True:
            if not processes:
                break
            if any(proc.poll() is not None for proc in processes):
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if ipc_proc is not None:
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
    status = (
        summarize_supervisor_status(record, process_ops)
        if record is not None
        else SupervisorProcessStatus(ipc_running=False, daemon_running=False)
    )
    pid_suffix = "" if record is not None else " (missing)"
    lines = [
        "GISMO supervise status",
        f"pid_file: {pid_path}{pid_suffix}",
        f"db_path: {record.db_path if record is not None else (db_path or 'unknown')}",
        f"ipc_pid: {_fmt_pid(record, 'ipc_pid')} ({_fmt_pid_running(record, status.ipc_running)})",
        f"daemon_pid: {_fmt_pid(record, 'daemon_pid')} ({_fmt_pid_running(record, status.daemon_running)})",
    ]
    if record is not None:
        lines.append(f"ipc_reused: {record.ipc_reused}")
    if db_path and record is not None and db_path != record.db_path:
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
    lines.append(_fmt_ipc_status(ipc_reachable, status.ipc_running))
    if ipc_reachable:
        daemon_status = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_status", {}, token)
        )
        if daemon_status.ok:
            paused = bool(daemon_status.data and daemon_status.data.get("paused"))
            lines.append(f"daemon_paused: {paused}")
            lines.append(_fmt_daemon_status(paused))
        else:
            lines.append(f"daemon_paused: error ({daemon_status.error})")
            lines.append(f"daemon_status: error ({daemon_status.error})")
    else:
        lines.append(_fmt_daemon_status(None))
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
    if record.ipc_started:
        _terminate_process(record.ipc_pid, process_ops)
    if record.daemon_started:
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


def _fmt_ping(ok: bool, error: str | None) -> str:
    if ok:
        return "ok"
    if error:
        return f"error ({error})"
    return "error"


def _probe_ipc_server(token: str) -> tuple[bool, bool]:
    try:
        response = ipc_cli.parse_ipc_response(ipc_cli.ipc_request("ping", {}, token))
    except ipc_cli.IPCConnectionError:
        return False, False
    if response.ok:
        return True, True
    if response.error == "unauthorized":
        return True, False
    if response.error == "unsupported_action":
        return _probe_ipc_fallback(token)
    return False, False


def _probe_ipc_fallback(token: str) -> tuple[bool, bool]:
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_status", {}, token)
        )
    except ipc_cli.IPCConnectionError:
        return False, False
    if response.ok:
        return True, True
    if response.error == "unauthorized":
        return True, False
    return False, False


def _fmt_pid(record: SupervisorRecord | None, field: str) -> str:
    if record is None:
        return "0"
    return str(getattr(record, field))


def _fmt_pid_running(record: SupervisorRecord | None, running: bool) -> str:
    if record is None:
        return "pid_file_missing"
    return f"pid_file_running={running}"


def _fmt_ipc_status(ipc_reachable: bool, pid_running: bool) -> str:
    if ipc_reachable:
        return "ipc_status: running (observed: ping)"
    return f"ipc_status: unknown (unreachable; pid_file_running={pid_running})"


def _fmt_daemon_status(paused: bool | None) -> str:
    if paused is None:
        return "daemon_status: unknown (ipc_unreachable)"
    return "daemon_status: paused" if paused else "daemon_status: running"


def _windows_is_running(pid: int) -> bool:
    access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_terminate(pid: int) -> None:
    access = 0x0001  # PROCESS_TERMINATE
    handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise OSError("process not found")
    try:
        if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
            raise OSError("terminate failed")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
