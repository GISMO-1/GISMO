"""Helpers for GISMO sidecar device credentials config."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from gismo.core.paths import resolve_devices_config_path

_IDENTIFIER_FIELDS = (
    "gismo_device_id",
    "device_id",
    "id",
    "ip",
    "name",
    "alias",
    "hostname",
    "label",
)


def load_configured_devices(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    path = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else resolve_devices_config_path(db_path)
    )
    if not path.exists():
        return path, []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in devices config: {path}") from exc

    if isinstance(payload, dict):
        devices = payload.get("devices", [])
    elif isinstance(payload, list):
        devices = payload
    else:
        raise RuntimeError(f"Devices config must be a list or object with a 'devices' list: {path}")

    if not isinstance(devices, list):
        raise RuntimeError(f"Devices config 'devices' value must be a list: {path}")

    return path, [dict(item) for item in devices if isinstance(item, dict)]


def find_configured_device(
    devices: list[dict[str, Any]],
    *,
    identifiers: Iterable[str],
) -> dict[str, Any] | None:
    wanted = {_normalize_identifier(value) for value in identifiers}
    wanted.discard("")
    if not wanted:
        return None

    matches = [dict(device) for device in devices if wanted & _device_identifiers(device)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError("Configured device identity is ambiguous; use a unique gismo_device_id.")
    return None


def normalize_platform_name(entry: dict[str, Any]) -> str:
    for field in ("adapter", "platform", "controller"):
        value = _normalize_identifier(entry.get(field))
        if value:
            return value
    return ""


def ordered_identifiers(*values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = _normalize_identifier(text)
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(text)
    return ordered


def _device_identifiers(device: dict[str, Any]) -> set[str]:
    values = {_normalize_identifier(device.get(field)) for field in _IDENTIFIER_FIELDS}
    values.discard("")
    return values


def _normalize_identifier(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())
