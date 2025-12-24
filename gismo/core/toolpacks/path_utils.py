"""Shared path safety helpers for toolpacks."""
from __future__ import annotations

from pathlib import Path


def resolve_within_base(base_dir: Path, target: str) -> Path:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("path must be a non-empty string")
    base_dir = base_dir.resolve()
    candidate = (base_dir / target).resolve()
    if candidate != base_dir and base_dir not in candidate.parents:
        raise PermissionError("Path is outside the allowed base directory")
    return candidate
