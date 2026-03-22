"""Execution boundary helpers for trust-zone-aware runtime isolation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from gismo.core.security_events import SecurityEvent, append_security_event
from gismo.core.trust_zones import (
    BOUNDARY_PROCESS,
    BOUNDARY_SANDBOX,
    BOUNDARY_TRUSTED_CORE,
    EXECUTION_MODE_IN_PROCESS,
    EXECUTION_MODE_ISOLATED_SUBPROCESS,
    EXECUTION_MODE_SANDBOXED,
    STATE_ACCESS_DIRECT,
    ComponentTrustBinding,
    get_component_binding,
)


EXECUTION_EVENT_TYPES = (
    "execution_mode_selected",
    "isolated_execution_started",
    "isolated_execution_finished",
    "isolated_execution_failed",
    "trust_zone_violation_blocked",
)
STRICT_STATE_BOUNDARY_COMPONENTS = {
    "device_control",
    "web_device_discovery",
    "ollama_client",
    "plugin_loader",
    "plugin_runtime",
}
BLOCKED_INPUT_KEYS = {"db_path", "state_store", "connection", "cursor"}
_WINDOWS_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    component: str
    action: str
    actor: str
    resource: str
    zone: str
    mode: str
    boundary_kind: str
    state_access: str
    related_run_id: str | None = None
    related_task_id: str | None = None
    related_plan_id: str | None = None
    db_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "component": self.component,
            "action": self.action,
            "actor": self.actor,
            "resource": self.resource,
            "zone": self.zone,
            "mode": self.mode,
            "boundary_kind": self.boundary_kind,
            "state_access": self.state_access,
            "related_run_id": self.related_run_id,
            "related_task_id": self.related_task_id,
            "related_plan_id": self.related_plan_id,
            "db_path": self.db_path,
        }


@dataclass(frozen=True)
class SandboxProfile:
    name: str
    working_directory: str
    environment: dict[str, str]
    guarantees: list[str]
    limitations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "working_directory": self.working_directory,
            "environment_keys": sorted(self.environment),
            "guarantees": list(self.guarantees),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class ExecutionReport:
    execution_id: str
    component: str
    zone: str
    mode: str
    boundary_kind: str
    state_access: str
    action: str
    resource: str
    process_isolated: bool
    result: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    failure_reason: str | None = None
    sandbox_profile: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "component": self.component,
            "zone": self.zone,
            "mode": self.mode,
            "boundary_kind": self.boundary_kind,
            "state_access": self.state_access,
            "action": self.action,
            "resource": self.resource,
            "process_isolated": self.process_isolated,
            "result": self.result,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "failure_reason": self.failure_reason,
            "sandbox_profile": self.sandbox_profile,
        }


@dataclass(frozen=True)
class ExecutionHistoryEntry:
    execution_id: str
    timestamp: str
    component: str
    zone: str
    mode: str
    action: str
    resource: str
    result: str
    related_run_id: str | None
    related_task_id: str | None
    related_plan_id: str | None
    selected_event_id: str | None = None
    finished_event_id: str | None = None
    failure_event_id: str | None = None
    blocked_event_id: str | None = None
    sandbox_profile: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "timestamp": self.timestamp,
            "component": self.component,
            "zone": self.zone,
            "mode": self.mode,
            "action": self.action,
            "resource": self.resource,
            "result": self.result,
            "related_run_id": self.related_run_id,
            "related_task_id": self.related_task_id,
            "related_plan_id": self.related_plan_id,
            "selected_event_id": self.selected_event_id,
            "finished_event_id": self.finished_event_id,
            "failure_event_id": self.failure_event_id,
            "blocked_event_id": self.blocked_event_id,
            "sandbox_profile": self.sandbox_profile,
        }


class ExecutionBoundaryError(RuntimeError):
    def __init__(self, message: str, *, report: ExecutionReport) -> None:
        super().__init__(message)
        self.execution_report = report


class TrustZoneViolationError(PermissionError):
    def __init__(self, message: str, *, report: ExecutionReport) -> None:
        super().__init__(message)
        self.execution_report = report


def build_execution_request(
    *,
    component: str,
    action: str,
    actor: str,
    resource: str | None = None,
    mode: str | None = None,
    state_access: str | None = None,
    db_path: str | None = None,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    execution_id: str | None = None,
) -> ExecutionRequest:
    binding = get_component_binding(component)
    selected_mode = mode or binding.default_execution_mode
    return ExecutionRequest(
        execution_id=execution_id or str(uuid4()),
        component=component,
        action=action,
        actor=actor,
        resource=resource or _default_resource(binding),
        zone=binding.zone,
        mode=selected_mode,
        boundary_kind=_boundary_for_mode(binding, selected_mode),
        state_access=state_access or binding.state_access,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
        db_path=db_path,
    )


def build_sandbox_profile(
    *,
    component: str,
    db_path: str | None = None,
    working_directory: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    extra_python_paths: list[str] | None = None,
) -> SandboxProfile:
    repo_root = Path(__file__).resolve().parents[2]
    sandbox_root = (
        Path(working_directory)
        if working_directory is not None
        else _default_sandbox_root(component=component, db_path=db_path)
    )
    sandbox_root.mkdir(parents=True, exist_ok=True)
    temp_dir = sandbox_root / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {}
    for key in _WINDOWS_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    python_paths = [str(repo_root)]
    for path in extra_python_paths or []:
        text = str(path).strip()
        if text and text not in python_paths:
            python_paths.append(text)

    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    if extra_env:
        for key, value in extra_env.items():
            if key and value is not None:
                env[str(key)] = str(value)

    return SandboxProfile(
        name=f"{component}-sandbox",
        working_directory=str(sandbox_root),
        environment=env,
        guarantees=[
            "process separation",
            "explicit working directory",
            "reduced inherited environment",
            "captured stdout/stderr",
            "explicit timeout",
            "marshaled input/output only",
        ],
        limitations=[
            "not an OS or kernel sandbox",
            "no filesystem virtualization",
            "no container or VM boundary",
            "network use inside child code is not kernel-enforced",
        ],
    )


def build_worker_command(worker_name: str) -> list[str]:
    return [sys.executable, "-m", "gismo.core.isolation_worker", worker_name]


def record_execution_mode_selected(
    request: ExecutionRequest,
    *,
    details: dict[str, Any] | None = None,
    sandbox_profile: SandboxProfile | None = None,
) -> ExecutionReport:
    report = ExecutionReport(
        execution_id=request.execution_id,
        component=request.component,
        zone=request.zone,
        mode=request.mode,
        boundary_kind=request.boundary_kind,
        state_access=request.state_access,
        action=request.action,
        resource=request.resource,
        process_isolated=request.mode in {EXECUTION_MODE_ISOLATED_SUBPROCESS, EXECUTION_MODE_SANDBOXED},
        result="selected",
        sandbox_profile=sandbox_profile.to_dict() if sandbox_profile is not None else None,
    )
    payload = report.to_dict()
    if details:
        payload["details"] = dict(details)
    _record_security_event(
        request=request,
        event_type="execution_mode_selected",
        payload=payload,
    )
    return report


def run_isolated_process(
    request: ExecutionRequest,
    *,
    worker_command: list[str],
    worker_input: dict[str, Any],
    timeout_s: float,
    event_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _run_marshaled_process(
        request,
        worker_command=worker_command,
        worker_input=worker_input,
        timeout_s=timeout_s,
        event_details=event_details,
        sandbox_profile=None,
    )


def run_sandboxed_process(
    request: ExecutionRequest,
    *,
    worker_command: list[str],
    worker_input: dict[str, Any],
    timeout_s: float,
    sandbox_profile: SandboxProfile | None = None,
    event_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = sandbox_profile or build_sandbox_profile(
        component=request.component,
        db_path=request.db_path,
    )
    return _run_marshaled_process(
        request,
        worker_command=worker_command,
        worker_input=worker_input,
        timeout_s=timeout_s,
        event_details=event_details,
        sandbox_profile=profile,
    )


def summarize_execution_events(
    events: list[SecurityEvent],
    *,
    limit: int = 25,
    mode: str | None = None,
    zone: str | None = None,
    component: str | None = None,
) -> list[ExecutionHistoryEntry]:
    groups: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: item.seq, reverse=True):
        if event.event_type not in EXECUTION_EVENT_TYPES:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        execution_id = payload.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id.strip():
            continue
        entry = groups.setdefault(
            execution_id,
            {
                "execution_id": execution_id,
                "timestamp": event.timestamp.isoformat(),
                "component": str(payload.get("component") or "-"),
                "zone": str(payload.get("zone") or "-"),
                "mode": str(payload.get("mode") or "-"),
                "action": str(payload.get("action") or event.action),
                "resource": str(payload.get("resource") or event.resource),
                "result": "selected",
                "related_run_id": event.related_run_id,
                "related_task_id": event.related_task_id,
                "related_plan_id": event.related_plan_id,
                "selected_event_id": None,
                "finished_event_id": None,
                "failure_event_id": None,
                "blocked_event_id": None,
                "sandbox_profile": payload.get("sandbox_profile"),
            },
        )
        if event.event_type == "execution_mode_selected" and entry["selected_event_id"] is None:
            entry["selected_event_id"] = event.id
        elif event.event_type == "isolated_execution_finished" and entry["finished_event_id"] is None:
            entry["finished_event_id"] = event.id
            entry["result"] = "succeeded"
        elif event.event_type == "isolated_execution_failed" and entry["failure_event_id"] is None:
            entry["failure_event_id"] = event.id
            entry["result"] = "failed"
        elif event.event_type == "trust_zone_violation_blocked" and entry["blocked_event_id"] is None:
            entry["blocked_event_id"] = event.id
            entry["result"] = "blocked"
        elif event.event_type == "isolated_execution_started" and entry["result"] == "selected":
            entry["result"] = "running"

    items = [ExecutionHistoryEntry(**payload) for payload in groups.values()]
    filtered = [
        item
        for item in items
        if (mode is None or item.mode == mode)
        and (zone is None or item.zone == zone)
        and (component is None or item.component == component)
    ]
    filtered.sort(key=lambda item: item.timestamp, reverse=True)
    return filtered[: max(limit, 1)]


def select_execution_events(
    events: list[SecurityEvent],
    *,
    execution_id: str,
) -> list[SecurityEvent]:
    selected: list[SecurityEvent] = []
    for event in sorted(events, key=lambda item: item.seq):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("execution_id") == execution_id:
            selected.append(event)
    return selected


def _run_marshaled_process(
    request: ExecutionRequest,
    *,
    worker_command: list[str],
    worker_input: dict[str, Any],
    timeout_s: float,
    event_details: dict[str, Any] | None,
    sandbox_profile: SandboxProfile | None,
) -> dict[str, Any]:
    if request.mode not in {EXECUTION_MODE_ISOLATED_SUBPROCESS, EXECUTION_MODE_SANDBOXED}:
        raise ValueError("marshaled process execution requires isolated_subprocess or sandboxed mode")
    _validate_boundary_request(request, worker_input)

    selected = record_execution_mode_selected(
        request,
        details=event_details,
        sandbox_profile=sandbox_profile,
    )
    start_payload = {
        **selected.to_dict(),
        "worker_command": list(worker_command),
    }
    _record_security_event(
        request=request,
        event_type="isolated_execution_started",
        payload=start_payload,
    )

    started = time.monotonic()
    kwargs: dict[str, Any] = {
        "input": json.dumps(worker_input, ensure_ascii=False),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "timeout": max(timeout_s, 1.0) + 2.0,
        "check": False,
    }
    if sandbox_profile is not None:
        kwargs["cwd"] = sandbox_profile.working_directory
        kwargs["env"] = dict(sandbox_profile.environment)
        flags = _creation_flags()
        if flags:
            kwargs["creationflags"] = flags

    try:
        completed = subprocess.run(worker_command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        report = _finalize_report(
            request,
            started=started,
            failure_reason=f"worker timed out after {max(timeout_s, 1.0) + 2.0:.1f}s",
            stderr_excerpt=_summarize(getattr(exc, "stderr", None)),
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(str(report.failure_reason), report=report) from exc
    except OSError as exc:
        report = _finalize_report(
            request,
            started=started,
            failure_reason=f"worker launch failed: {exc}",
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(str(report.failure_reason), report=report) from exc

    if completed.returncode != 0:
        report = _finalize_report(
            request,
            started=started,
            exit_code=completed.returncode,
            stdout_excerpt=_summarize(completed.stdout),
            stderr_excerpt=_summarize(completed.stderr),
            failure_reason=f"worker exited with code {completed.returncode}",
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(str(report.failure_reason), report=report)

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        report = _finalize_report(
            request,
            started=started,
            stdout_excerpt=_summarize(completed.stdout),
            stderr_excerpt=_summarize(completed.stderr),
            failure_reason="worker returned invalid JSON",
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(str(report.failure_reason), report=report) from exc

    if not isinstance(payload, dict):
        report = _finalize_report(
            request,
            started=started,
            stdout_excerpt=_summarize(completed.stdout),
            stderr_excerpt=_summarize(completed.stderr),
            failure_reason="worker returned a non-object payload",
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(str(report.failure_reason), report=report)

    if not payload.get("ok", False):
        reason = str(payload.get("error") or "isolated worker failed")
        report = _finalize_report(
            request,
            started=started,
            exit_code=_coerce_int(payload.get("exit_code")),
            stdout_excerpt=_summarize(payload.get("stdout")),
            stderr_excerpt=_summarize(payload.get("stderr")),
            failure_reason=reason,
            sandbox_profile=sandbox_profile,
        )
        _record_isolated_failure(request, report)
        raise ExecutionBoundaryError(reason, report=report)

    report = _finalize_report(
        request,
        started=started,
        exit_code=_coerce_int(payload.get("exit_code")),
        stdout_excerpt=_summarize(payload.get("stdout")),
        stderr_excerpt=_summarize(payload.get("stderr")),
        sandbox_profile=sandbox_profile,
    )
    _record_security_event(
        request=request,
        event_type="isolated_execution_finished",
        payload=report.to_dict(),
    )
    payload["execution"] = report.to_dict()
    return payload


def _validate_boundary_request(
    request: ExecutionRequest,
    worker_input: dict[str, Any],
) -> None:
    if request.state_access == STATE_ACCESS_DIRECT:
        _block_request(
            request,
            reason="direct trusted-state handle requested across isolated boundary",
            detail={"requested_state_access": request.state_access},
        )
    _assert_marshaled_value(request, worker_input, path="input")


def _assert_marshaled_value(
    request: ExecutionRequest,
    value: Any,
    *,
    path: str,
) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_marshaled_value(request, item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _block_request(
                    request,
                    reason="non-string key in marshaled payload",
                    detail={"path": path, "key_type": type(key).__name__},
                )
            if request.component in STRICT_STATE_BOUNDARY_COMPONENTS and key in BLOCKED_INPUT_KEYS:
                _block_request(
                    request,
                    reason="trusted-state locator blocked for isolated runtime",
                    detail={"path": f"{path}.{key}", "key": key},
                )
            _assert_marshaled_value(request, item, path=f"{path}.{key}")
        return
    _block_request(
        request,
        reason="non-marshaled value blocked at execution boundary",
        detail={"path": path, "value_type": type(value).__name__},
    )


def _block_request(
    request: ExecutionRequest,
    *,
    reason: str,
    detail: dict[str, Any],
) -> None:
    report = ExecutionReport(
        execution_id=request.execution_id,
        component=request.component,
        zone=request.zone,
        mode=request.mode,
        boundary_kind=request.boundary_kind,
        state_access=request.state_access,
        action=request.action,
        resource=request.resource,
        process_isolated=request.mode in {EXECUTION_MODE_ISOLATED_SUBPROCESS, EXECUTION_MODE_SANDBOXED},
        result="blocked",
        failure_reason=reason,
    )
    payload = {
        **report.to_dict(),
        **detail,
    }
    _record_security_event(
        request=request,
        event_type="trust_zone_violation_blocked",
        payload=payload,
    )
    raise TrustZoneViolationError(reason, report=report)


def _record_isolated_failure(request: ExecutionRequest, report: ExecutionReport) -> None:
    _record_security_event(
        request=request,
        event_type="isolated_execution_failed",
        payload=report.to_dict(),
    )


def _record_security_event(
    *,
    request: ExecutionRequest,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if not request.db_path or not str(request.db_path).strip():
        return
    append_security_event(
        db_path=request.db_path,
        event_type=event_type,
        actor=request.actor,
        action=request.action,
        resource=request.resource,
        payload=payload,
        related_run_id=request.related_run_id,
        related_task_id=request.related_task_id,
        related_plan_id=request.related_plan_id,
    )


def _boundary_for_mode(binding: ComponentTrustBinding, mode: str) -> str:
    if mode == EXECUTION_MODE_ISOLATED_SUBPROCESS:
        return BOUNDARY_PROCESS
    if mode == EXECUTION_MODE_SANDBOXED:
        return BOUNDARY_SANDBOX
    if mode == EXECUTION_MODE_IN_PROCESS:
        return binding.boundary_kind
    return binding.boundary_kind


def _default_resource(binding: ComponentTrustBinding) -> str:
    prefix = "tool" if binding.component in {"run_shell", "device_control", "plugin_runtime"} else "component"
    return f"{prefix}:{binding.component}"


def _finalize_report(
    request: ExecutionRequest,
    *,
    started: float,
    exit_code: int | None = None,
    stdout_excerpt: str | None = None,
    stderr_excerpt: str | None = None,
    failure_reason: str | None = None,
    sandbox_profile: SandboxProfile | None = None,
) -> ExecutionReport:
    return ExecutionReport(
        execution_id=request.execution_id,
        component=request.component,
        zone=request.zone,
        mode=request.mode,
        boundary_kind=request.boundary_kind,
        state_access=request.state_access,
        action=request.action,
        resource=request.resource,
        process_isolated=request.mode in {EXECUTION_MODE_ISOLATED_SUBPROCESS, EXECUTION_MODE_SANDBOXED},
        result="failed" if failure_reason else "succeeded",
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        exit_code=exit_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        failure_reason=failure_reason,
        sandbox_profile=sandbox_profile.to_dict() if sandbox_profile is not None else None,
    )


def _default_sandbox_root(*, component: str, db_path: str | None) -> Path:
    if db_path:
        return Path(db_path).resolve().parent / "runtime_sandbox" / component
    return (Path.cwd() / "tmp" / "runtime_sandbox" / component).resolve()


def _creation_flags() -> int:
    flags = 0
    if os.name == "nt":
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def _summarize(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
