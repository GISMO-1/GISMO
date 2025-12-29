"""Minimal Ollama HTTP client for local planning."""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "phi3:mini"
DEFAULT_OLLAMA_TIMEOUT_S = 120


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str
    timeout_s: int


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
    env_value = os.getenv("GISMO_OLLAMA_TIMEOUT_S")
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
    )


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
    payload = {
        "model": config.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else ""
        message = f"Ollama error {exc.code} from {config.url}. {detail}".strip()
        raise OllamaError(
            message,
            timeout_s=config.timeout_s,
            url=config.url,
            status_code=exc.code,
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise OllamaError(
            "Ollama request failed (timeout/connection) after "
            f"{config.timeout_s}s. Verify `ollama ps` and that {config.url} is "
            "reachable. Consider a smaller model or increase --timeout-s.",
            timeout_s=config.timeout_s,
            url=config.url,
        ) from exc

    try:
        payload_json: dict[str, Any] = json.loads(body)
        message = payload_json.get("message") or {}
        content = message.get("content")
    except (json.JSONDecodeError, TypeError) as exc:
        raise OllamaError(
            "Invalid JSON response from Ollama.",
            timeout_s=config.timeout_s,
            url=config.url,
        ) from exc
    if not isinstance(content, str):
        raise OllamaError(
            "Ollama response missing assistant content.",
            timeout_s=config.timeout_s,
            url=config.url,
        )
    return content
