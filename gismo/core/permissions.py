"""Permission gating for tools."""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Set
from urllib.parse import urlparse


@dataclass
class FileSystemPolicy:
    base_dir: Path


@dataclass
class ShellPolicy:
    base_dir: Path
    allowlist: list[list[str]] = field(default_factory=list)
    timeout_seconds: float = 10.0


@dataclass
class NetworkRule:
    allow_loopback: bool = False
    allow_private: bool = False
    allow_public: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_cidrs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NetworkDecision:
    component: str
    target: str
    allowed: bool
    reason: str
    matched_rule: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "target": self.target,
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


class NetworkPermissionError(PermissionError):
    def __init__(self, message: str, *, decision: NetworkDecision) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass
class NetworkPolicy:
    default_action: str = "deny"
    components: dict[str, NetworkRule] = field(default_factory=dict)

    def evaluate_scope(self, component: str, scope: str) -> NetworkDecision:
        normalized = scope.strip().lower()
        if normalized not in {"loopback", "private", "public"}:
            raise ValueError("scope must be loopback, private, or public")
        rule = self.components.get(component)
        if rule is not None:
            if normalized == "loopback" and rule.allow_loopback:
                return NetworkDecision(component, normalized, True, "loopback allowed", "allow_loopback")
            if normalized == "private" and rule.allow_private:
                return NetworkDecision(component, normalized, True, "private network allowed", "allow_private")
            if normalized == "public" and rule.allow_public:
                return NetworkDecision(component, normalized, True, "public network allowed", "allow_public")
        if self.default_action == "allow":
            return NetworkDecision(component, normalized, True, "allowed by network default action")
        return NetworkDecision(component, normalized, False, "network egress denied by default")

    def check_scope_allowed(self, component: str, scope: str) -> None:
        decision = self.evaluate_scope(component, scope)
        if not decision.allowed:
            raise NetworkPermissionError(
                f"Network access denied for {component} ({scope}).",
                decision=decision,
            )

    def evaluate_target(self, component: str, target: str) -> NetworkDecision:
        normalized_target = (target or "").strip()
        if not normalized_target:
            raise ValueError("target must be a non-empty string")
        host = _extract_host(normalized_target)
        if host is None:
            if self.default_action == "allow":
                return NetworkDecision(component, normalized_target, True, "allowed by network default action")
            return NetworkDecision(component, normalized_target, False, "unable to resolve network target")
        rule = self.components.get(component)
        if rule is not None:
            decision = _evaluate_rule_for_host(component, normalized_target, host, rule)
            if decision is not None:
                return decision
        if self.default_action == "allow":
            return NetworkDecision(component, normalized_target, True, "allowed by network default action")
        return NetworkDecision(component, normalized_target, False, "network egress denied by default")

    def check_target_allowed(self, component: str, target: str) -> None:
        decision = self.evaluate_target(component, target)
        if not decision.allowed:
            raise NetworkPermissionError(
                f"Network access denied for {component} target {target}.",
                decision=decision,
            )

    def summary_for_component(self, component: str) -> dict[str, object]:
        rule = self.components.get(component)
        if rule is None:
            return {"default_action": self.default_action, "configured": False}
        return {
            "default_action": self.default_action,
            "configured": True,
            "allow_loopback": rule.allow_loopback,
            "allow_private": rule.allow_private,
            "allow_public": rule.allow_public,
            "allowed_hosts": list(rule.allowed_hosts),
            "allowed_cidrs": list(rule.allowed_cidrs),
        }


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
    network: NetworkPolicy = field(default_factory=NetworkPolicy)

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
    network_config = data.get("network", {}) or {}
    fs_base_dir = _resolve_base_dir(repo_root, fs_config.get("base_dir", "."))
    shell_base_dir = _resolve_base_dir(repo_root, shell_config.get("base_dir", "."))
    allowlist = _ensure_command_allowlist(shell_config.get("allowlist", []))
    timeout_seconds = _ensure_timeout(shell_config.get("timeout_seconds", 10))
    memory_allow = _ensure_namespace_map(memory_config.get("allow", {}), "memory.allow")
    memory_confirmation = _ensure_namespace_map(
        memory_config.get("require_confirmation", {}),
        "memory.require_confirmation",
    )
    network_default_action = _ensure_network_default_action(
        network_config.get("default_action", "deny")
    )
    network_components = _ensure_network_components(
        network_config.get("components", {}),
        "network.components",
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
        network=NetworkPolicy(
            default_action=network_default_action,
            components=network_components,
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


def _ensure_network_default_action(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("network.default_action must be a string")
    normalized = value.strip().lower()
    if normalized not in {"allow", "deny"}:
        raise ValueError("network.default_action must be 'allow' or 'deny'")
    return normalized


def _ensure_network_components(
    value: object,
    field_name: str,
) -> dict[str, NetworkRule]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping of component -> rule")
    normalized: dict[str, NetworkRule] = {}
    for component, raw_rule in value.items():
        if not isinstance(component, str) or not component.strip():
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{field_name}.{component} must be an object")
        normalized[component] = NetworkRule(
            allow_loopback=_ensure_bool(
                raw_rule.get("allow_loopback", False),
                f"{field_name}.{component}.allow_loopback",
            ),
            allow_private=_ensure_bool(
                raw_rule.get("allow_private", False),
                f"{field_name}.{component}.allow_private",
            ),
            allow_public=_ensure_bool(
                raw_rule.get("allow_public", False),
                f"{field_name}.{component}.allow_public",
            ),
            allowed_hosts=_ensure_string_list(
                raw_rule.get("allowed_hosts", []),
                f"{field_name}.{component}.allowed_hosts",
            ),
            allowed_cidrs=_ensure_string_list(
                raw_rule.get("allowed_cidrs", []),
                f"{field_name}.{component}.allowed_cidrs",
            ),
        )
        for cidr in normalized[component].allowed_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"{field_name}.{component}.allowed_cidrs contains invalid CIDR {cidr!r}") from exc
    return normalized


def _ensure_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean")


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


def _extract_host(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme and parsed.hostname:
        return parsed.hostname
    if target.lower() == "localhost":
        return "localhost"
    if "://" not in target and target:
        if target.count(":") == 1:
            return target.split(":", 1)[0]
        return target
    return None


def _evaluate_rule_for_host(
    component: str,
    target: str,
    host: str,
    rule: NetworkRule,
) -> NetworkDecision | None:
    lowered = host.lower()
    if lowered == "localhost":
        if rule.allow_loopback:
            return NetworkDecision(component, target, True, "loopback allowed", "allow_loopback")
        return NetworkDecision(component, target, False, "loopback not allowed", "allow_loopback")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        for allowed_host in rule.allowed_hosts:
            if _host_matches(lowered, allowed_host):
                return NetworkDecision(component, target, True, "hostname allowed", allowed_host)
        if rule.allow_public:
            return NetworkDecision(component, target, True, "public network allowed", "allow_public")
        return None
    if ip.is_loopback:
        if rule.allow_loopback:
            return NetworkDecision(component, target, True, "loopback allowed", "allow_loopback")
        return NetworkDecision(component, target, False, "loopback not allowed", "allow_loopback")
    if ip.is_private:
        if rule.allow_private:
            return NetworkDecision(component, target, True, "private network allowed", "allow_private")
        for cidr in rule.allowed_cidrs:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return NetworkDecision(component, target, True, "address allowed", cidr)
        return NetworkDecision(component, target, False, "private network not allowed", "allow_private")
    for cidr in rule.allowed_cidrs:
        if ip in ipaddress.ip_network(cidr, strict=False):
            return NetworkDecision(component, target, True, "address allowed", cidr)
    if rule.allow_public:
        return NetworkDecision(component, target, True, "public network allowed", "allow_public")
    return None


def _host_matches(host: str, pattern: str) -> bool:
    normalized = pattern.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("*."):
        suffix = normalized[1:]
        return host.endswith(suffix)
    return host == normalized
