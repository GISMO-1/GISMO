"""Small JSON worker used for process-separated execution paths."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

from gismo.core.device_runtime import run_device_worker
from gismo.core.plugin_runtime import run_plugin_worker


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        _write_json(
            {
                "ok": False,
                "error": "worker name is required",
                "error_type": "ValueError",
            }
        )
        return 2
    worker_name = args[0].strip().lower()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _write_json(
            {
                "ok": False,
                "error": f"invalid worker payload: {exc}",
                "error_type": "JSONDecodeError",
            }
        )
        return 2
    try:
        if worker_name == "shell":
            result = _run_shell(payload)
        elif worker_name == "curl":
            result = _run_curl(payload)
        elif worker_name == "device":
            result = run_device_worker(payload)
        elif worker_name == "ollama_python":
            result = _run_ollama_python(payload)
        elif worker_name == "plugin":
            result = run_plugin_worker(payload)
        else:
            raise ValueError(f"unknown worker: {worker_name}")
    except Exception as exc:  # noqa: BLE001 - worker returns structured errors
        _write_json(
            {
                "ok": False,
                "error": str(exc) or exc.__class__.__name__,
                "error_type": exc.__class__.__name__,
            }
        )
        return 1
    _write_json(result)
    return 0


def _run_shell(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    cwd = payload.get("cwd")
    timeout_seconds = payload.get("timeout_seconds")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise ValueError("command must be a non-empty list of strings")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("cwd must be a non-empty string")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    run_command = list(command)
    run_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "timeout": float(timeout_seconds),
        "check": False,
    }
    if os.name == "nt":
        run_command = ["cmd", "/c", *command]
        run_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        completed = subprocess.run(run_command, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"shell command timed out after {timeout_seconds}s",
            "error_type": "TimeoutExpired",
            "stdout": _decode_timeout_stream(getattr(exc, "stdout", None)),
            "stderr": _decode_timeout_stream(getattr(exc, "stderr", None)),
        }
    return {
        "ok": True,
        "command": list(command),
        "cwd": cwd,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }


def _run_curl(payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    payload_json = payload.get("payload_json")
    timeout_seconds = payload.get("timeout_seconds")
    curl_executable = payload.get("curl_executable")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(payload_json, str):
        raise ValueError("payload_json must be a string")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if not isinstance(curl_executable, str) or not curl_executable.strip():
        raise ValueError("curl_executable must be a non-empty string")

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as temp_file:
            temp_file.write(payload_json)
            temp_path = temp_file.name
        command = [
            curl_executable,
            "-sS",
            url,
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            f"@{temp_path}",
        ]
        curl_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "timeout": float(timeout_seconds),
            "check": False,
        }
        if os.name == "nt":
            curl_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        completed = subprocess.run(command, **curl_kwargs)
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"curl timed out after {timeout_seconds}s",
            "error_type": "TimeoutExpired",
            "stdout": _decode_timeout_stream(getattr(exc, "stdout", None)),
            "stderr": _decode_timeout_stream(getattr(exc, "stderr", None)),
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return {
        "ok": True,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }


def _run_ollama_python(payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    payload_json = payload.get("payload_json")
    timeout_seconds = payload.get("timeout_seconds")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(payload_json, str):
        raise ValueError("payload_json must be a string")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    request = urllib.request.Request(
        url,
        data=payload_json.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else ""
        return {
            "ok": False,
            "error": f"Ollama error {exc.code}. {detail}".strip(),
            "error_type": "HTTPError",
            "status_code": exc.code,
            "stdout": detail,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "error": f"Ollama request failed: {exc}",
            "error_type": exc.__class__.__name__,
        }
    return {
        "ok": True,
        "body": body,
        "exit_code": 0,
    }


def _decode_timeout_stream(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
