"""Permission gating for tools."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Set


@dataclass
class FileSystemPolicy:
    base_dir: Path


@dataclass
class ShellPolicy:
    base_dir: Path
    allowlist: list[list[str]] = field(default_factory=list)
    timeout_seconds: float = 10.0


@dataclass
class MemoryPolicy:
    allow: dict[str, list[str]] = field(default_factory=dict)
    require_confirmation: dict[str, list[str]] = field(default_factory=dict)

    def is_allowed(self, action: str, namespace: str) -> bool:
        allowed = self.allow.get(action, [])
        return _matches_namespace(namespace, allowed)

    def requires_confirmation(self, action: str, namespace: str) -> bool:
        required = self.require_confirmation.get(action, [])
        return _matches_namespace(namespace, required)


@dataclass
class PermissionPolicy:
    allowed_tools: Set[str] = field(default_factory=set)
    fs: FileSystemPolicy = field(default_factory=lambda: FileSystemPolicy(Path(".")))
    shell: ShellPolicy = field(default_factory=lambda: ShellPolicy(Path(".")))
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)

    def allow(self, tool_name: str) -> None:
        self.allowed_tools.add(tool_name)

    def revoke(self, tool_name: str) -> None:
        self.allowed_tools.discard(tool_name)

    def check_tool_allowed(self, tool_name: str) -> None:
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"Tool '{tool_name}' is not allowed")


def load_policy(
    policy_path: str | None,
    *,
    repo_root: Path,
    default_allowed_tools: Iterable[str] = (),
) -> PermissionPolicy:
    repo_root = repo_root.resolve()
    if policy_path is None:
        return PermissionPolicy(
            allowed_tools=set(default_allowed_tools),
            fs=FileSystemPolicy(base_dir=repo_root),
            shell=ShellPolicy(base_dir=repo_root),
        )

    data = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    allowed_tools = _ensure_string_list(data.get("allowed_tools", []), "allowed_tools")
    fs_config = data.get("fs", {}) or {}
    shell_config = data.get("shell", {}) or {}
    memory_config = data.get("memory", {}) or {}
    fs_base_dir = _resolve_base_dir(repo_root, fs_config.get("base_dir", "."))
    shell_base_dir = _resolve_base_dir(repo_root, shell_config.get("base_dir", "."))
    allowlist = _ensure_command_allowlist(shell_config.get("allowlist", []))
    timeout_seconds = _ensure_timeout(shell_config.get("timeout_seconds", 10))
    memory_allow = _ensure_namespace_map(memory_config.get("allow", {}), "memory.allow")
    memory_confirmation = _ensure_namespace_map(
        memory_config.get("require_confirmation", {}),
        "memory.require_confirmation",
    )
    return PermissionPolicy(
        allowed_tools=set(allowed_tools),
        fs=FileSystemPolicy(base_dir=fs_base_dir),
        shell=ShellPolicy(
            base_dir=shell_base_dir,
            allowlist=allowlist,
            timeout_seconds=timeout_seconds,
        ),
        memory=MemoryPolicy(
            allow=memory_allow,
            require_confirmation=memory_confirmation,
        ),
    )


def _ensure_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return value


def _ensure_command_allowlist(value: object) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("shell.allowlist must be a list of command lists")
    commands: list[list[str]] = []
    for entry in value:
        if not isinstance(entry, list) or not entry or not all(
            isinstance(part, str) and part for part in entry
        ):
            raise ValueError("shell.allowlist entries must be non-empty string lists")
        commands.append(entry)
    return commands


def _ensure_timeout(value: object) -> float:
    if isinstance(value, int | float):
        if value <= 0:
            raise ValueError("shell.timeout_seconds must be positive")
        return float(value)
    raise ValueError("shell.timeout_seconds must be a number")


def _ensure_namespace_map(value: object, field_name: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping of action -> namespaces")
    normalized: dict[str, list[str]] = {}
    for key, namespaces in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{field_name} keys must be non-empty strings")
        normalized[key] = _ensure_string_list(namespaces, f"{field_name}.{key}")
    return normalized


def _matches_namespace(namespace: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            if namespace.startswith(pattern[:-1]):
                return True
            continue
        if namespace == pattern:
            return True
    return False


def _resolve_base_dir(repo_root: Path, base_dir_value: object) -> Path:
    if not isinstance(base_dir_value, str) or not base_dir_value.strip():
        raise ValueError("base_dir must be a non-empty string")
    base_path = Path(base_dir_value)
    if not base_path.is_absolute():
        base_path = repo_root / base_path
    resolved = base_path.resolve()
    if resolved != repo_root and repo_root not in resolved.parents:
        raise PermissionError("base_dir must be within the repository root")
    return resolved
