"""Shared Windows command helpers for GISMO."""
from __future__ import annotations


def quote_windows_arg(value: str) -> str:
    if not value:
        return "\"\""
    if any(ch in value for ch in (" ", "\t", "\"")):
        escaped = value.replace("\"", "\\\"")
        return f"\"{escaped}\""
    return value
