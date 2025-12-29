"""Minimal Ollama HTTP client for local planning."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "phi3:mini"
DEFAULT_OLLAMA_TIMEOUT_S = 120
DEFAULT_OLLAMA_KEEP_ALIVE = "10m"


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str
    timeout_s: int
    transport: str


class OllamaError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        timeout_s: int | None = None,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.timeout_s = timeout_s
        self.url = url
        self.status_code = status_code


def _coerce_timeout(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        timeout = int(value)
    except ValueError:
        return default
    return timeout if timeout > 0 else default


def resolve_ollama_url(url: str | None = None) -> str:
    return (
        url
        or os.getenv("GISMO_OLLAMA_URL")
        or os.getenv("OLLAMA_HOST")
        or DEFAULT_OLLAMA_URL
    ).rstrip("/")


def resolve_ollama_host(host: str | None = None) -> str:
    return resolve_ollama_url(host)


def resolve_ollama_model(model: str | None = None) -> str:
    return (
        model
        or os.getenv("GISMO_OLLAMA_MODEL")
        or os.getenv("GISMO_LLM_MODEL")
        or DEFAULT_OLLAMA_MODEL
    )


def resolve_ollama_timeout(timeout_s: int | None = None) -> int:
    if timeout_s is not None and timeout_s > 0:
        return timeout_s
    env_value = os.getenv("GISMO_OLLAMA_TIMEOUT_S") or os.getenv("GISMO_LLM_TIMEOUT_S")
    return _coerce_timeout(env_value, DEFAULT_OLLAMA_TIMEOUT_S)


def resolve_ollama_config(
    *,
    url: str | None = None,
    model: str | None = None,
    timeout_s: int | None = None,
) -> OllamaConfig:
    return OllamaConfig(
        url=resolve_ollama_url(url),
        model=resolve_ollama_model(model),
        timeout_s=resolve_ollama_timeout(timeout_s),
        transport=resolve_ollama_transport(),
    )


def build_ollama_chat_payload(
    prompt: str,
    system: str,
    *,
    model: str,
    keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE,
    temperature: float = 0,
) -> dict[str, Any]:
    return {
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": keep_alive,
        "options": {"temperature": temperature},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }


def resolve_ollama_transport() -> str:
    env_value = os.getenv("GISMO_OLLAMA_TRANSPORT")
    if env_value:
        normalized = env_value.strip().lower()
        if normalized in {"python", "curl"}:
            return normalized
        return "python"
    if _is_windows() and _curl_available_windows():
        return "curl"
    return "python"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _curl_available_windows() -> bool:
    try:
        result = subprocess.run(
            ["where.exe", "curl"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _resolve_curl_executable() -> str | None:
    if _is_windows():
        try:
            result = subprocess.run(
                ["where.exe", "curl"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip().splitlines()[0]
    return shutil.which("curl")


def ollama_chat(
    prompt: str,
    system: str,
    model: str | None = None,
    host: str | None = None,
    timeout_s: int | None = None,
) -> str:
    """Call Ollama chat API and return assistant content."""
    config = resolve_ollama_config(url=host, model=model, timeout_s=timeout_s)
    url = f"{config.url}/api/chat"
    payload = build_ollama_chat_payload(
        prompt,
        system,
        model=config.model,
    )
    payload_json = json.dumps(payload)
    if config.transport == "curl":
        curl_executable = _resolve_curl_executable()
        if curl_executable:
            try:
                return _ollama_chat_via_curl(
                    url,
                    payload_json,
                    timeout_s=config.timeout_s,
                    config=config,
                    curl_executable=curl_executable,
                )
            except OllamaError:
                pass
    return _ollama_chat_via_urllib(
        url,
        payload_json,
        timeout_s=config.timeout_s,
        config=config,
    )


def _ollama_chat_via_urllib(
    url: str,
    payload_json: str,
    *,
    timeout_s: int,
    config: OllamaConfig,
) -> str:
    data = payload_json.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else ""
        message = f"Ollama error {exc.code} from {config.url}. {detail}".strip()
        raise OllamaError(
            message,
            timeout_s=timeout_s,
            url=config.url,
            status_code=exc.code,
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise OllamaError(
            "Ollama request failed (timeout/connection) after "
            f"{timeout_s}s. Verify `ollama ps` and that {config.url} is "
            "reachable. Consider a smaller model or increase --timeout-s.",
            timeout_s=timeout_s,
            url=config.url,
        ) from exc
    return _extract_message_content(body, timeout_s=timeout_s, config=config)


def _ollama_chat_via_curl(
    url: str,
    payload_json: str,
    *,
    timeout_s: int,
    config: OllamaConfig,
    curl_executable: str,
) -> str:
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OllamaError(
            f"curl failed after {timeout_s}s.",
            timeout_s=timeout_s,
            url=config.url,
        ) from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    if result.returncode != 0:
        stderr = result.stderr.strip()
        message = (
            f"curl failed with exit code {result.returncode} "
            f"after {timeout_s}s. {stderr}"
        ).strip()
        raise OllamaError(
            message,
            timeout_s=timeout_s,
            url=config.url,
        )
    return _extract_message_content(result.stdout, timeout_s=timeout_s, config=config)


def _extract_message_content(body: str, *, timeout_s: int, config: OllamaConfig) -> str:
    try:
        payload_json: dict[str, Any] = json.loads(body)
        message = payload_json.get("message") or {}
        content = message.get("content")
    except (json.JSONDecodeError, TypeError) as exc:
        raise OllamaError(
            "Invalid JSON response from Ollama.",
            timeout_s=timeout_s,
            url=config.url,
        ) from exc
    if not isinstance(content, str):
        raise OllamaError(
            "Ollama response missing assistant content.",
            timeout_s=timeout_s,
            url=config.url,
        )
    return content
