"""Minimal Ollama HTTP client for local planning."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "phi3:mini"


def resolve_ollama_host(host: str | None = None) -> str:
    return (host or os.getenv("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")


def resolve_ollama_model(model: str | None = None) -> str:
    return model or os.getenv("GISMO_LLM_MODEL") or DEFAULT_MODEL


def ollama_chat(
    prompt: str,
    system: str,
    model: str | None = None,
    host: str | None = None,
    timeout_s: int = 60,
) -> str:
    """Call Ollama chat API and return assistant content."""
    resolved_host = resolve_ollama_host(host)
    resolved_model = resolve_ollama_model(model)
    url = f"{resolved_host}/api/chat"
    payload = {
        "model": resolved_model,
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
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else ""
        message = f"Ollama error {exc.code} from {resolved_host}. {detail}".strip()
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Ollama not running or unreachable at {resolved_host}. "
            "Start Ollama and ensure the model is pulled."
        ) from exc

    try:
        payload_json: dict[str, Any] = json.loads(body)
        message = payload_json.get("message") or {}
        content = message.get("content")
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Invalid JSON response from Ollama.") from exc
    if not isinstance(content, str):
        raise RuntimeError("Ollama response missing assistant content.")
    return content
