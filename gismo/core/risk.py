"""Deterministic plan risk classifier."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Literal


RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass(frozen=True)
class PlanRisk:
    risk_level: RiskLevel
    risk_flags: list[str]
    rationale: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "risk_level": self.risk_level,
            "risk_flags": list(self.risk_flags),
            "rationale": list(self.rationale),
        }


_FLAG_ORDER = [
    "shell",
    "writes",
    "dangerous_tool",
    "memory_modify",
    "supervisor_lifecycle",
    "many_actions",
]

_FLAG_RATIONALE = {
    "shell": "Plan includes shell execution.",
    "writes": "Plan includes write or modify actions.",
    "dangerous_tool": "Plan includes dangerous tool usage.",
    "memory_modify": "Plan modifies memory state.",
    "supervisor_lifecycle": "Plan touches supervisor lifecycle.",
    "many_actions": "Plan contains more than 3 actions.",
}

_WRITE_TOOLS = {"write_note", "write_file"}
_DANGEROUS_TOOLS = {"run_shell"}
_MEMORY_MUTATION_PATTERNS = (
    r"\bmemory\s+(put|delete|retire)\b",
    r"\bmemory\s+retention\s+(set|clear)\b",
    r"\bmemory\s+snapshot\s+import\b",
    r"\bmemory\s+doctor\s+repair\b",
)
_SUPERVISOR_PATTERNS = (
    r"\bsupervise\s+(up|down)\b",
    r"\brecover\b",
)


def classify_plan_risk(actions: Iterable[dict[str, object]]) -> PlanRisk:
    actions_list = list(actions)
    risk_flags: set[str] = set()

    action_count = len(actions_list)
    if action_count > 3:
        risk_flags.add("many_actions")

    for action in actions_list:
        command = action.get("command")
        command_text = command if isinstance(command, str) else str(command) if command else ""
        command_lower = command_text.strip().lower()
        tool_names = _infer_tools_from_command(command_text)

        if "run_shell" in tool_names or command_lower.startswith("shell:"):
            risk_flags.add("shell")

        if _contains_write_tool(tool_names):
            risk_flags.add("writes")

        if any(tool in _DANGEROUS_TOOLS for tool in tool_names):
            risk_flags.add("dangerous_tool")

        if _matches_memory_mutation(command_lower):
            risk_flags.add("memory_modify")

        if _matches_supervisor_lifecycle(command_lower):
            risk_flags.add("supervisor_lifecycle")

    ordered_flags = _order_flags(risk_flags)
    risk_level = _resolve_risk_level(ordered_flags)
    rationale = _build_rationale(ordered_flags)
    return PlanRisk(
        risk_level=risk_level,
        risk_flags=ordered_flags,
        rationale=rationale,
    )


def _resolve_risk_level(flags: list[str]) -> RiskLevel:
    if any(flag in flags for flag in ("shell", "writes", "dangerous_tool")):
        return "HIGH"
    if any(flag in flags for flag in ("many_actions", "memory_modify", "supervisor_lifecycle")):
        return "MEDIUM"
    return "LOW"


def _build_rationale(flags: list[str]) -> list[str]:
    rationale: list[str] = []
    for flag in flags:
        reason = _FLAG_RATIONALE.get(flag)
        if reason:
            rationale.append(reason)
    return rationale


def _order_flags(flags: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    remaining = set(flags)
    for flag in _FLAG_ORDER:
        if flag in remaining:
            ordered.append(flag)
            remaining.remove(flag)
    for flag in sorted(remaining):
        ordered.append(flag)
    return ordered


def _infer_tools_from_command(command: str) -> list[str]:
    if not isinstance(command, str):
        return []
    trimmed = command.strip()
    if not trimmed:
        return []
    lower = trimmed.lower()
    if lower.startswith("echo:"):
        return ["echo"]
    if lower.startswith("note:"):
        return ["write_note"]
    if lower.startswith("shell:") or lower.startswith("run_shell:"):
        return ["run_shell"]
    if lower.startswith("graph:"):
        remainder = trimmed.split(":", 1)[1].strip()
        if not remainder:
            return []
        steps = [part.strip() for part in remainder.split("->")]
        tools: list[str] = []
        for step in steps:
            match = re.match(r"(?i)^(echo|note)\s+(.+)$", step)
            if not match:
                return []
            verb = match.group(1).lower()
            tools.append("echo" if verb == "echo" else "write_note")
        return tools
    return []


def _contains_write_tool(tool_names: Iterable[str]) -> bool:
    return any(tool in _WRITE_TOOLS for tool in tool_names)


def _matches_memory_mutation(command_lower: str) -> bool:
    return any(re.search(pattern, command_lower) for pattern in _MEMORY_MUTATION_PATTERNS)


def _matches_supervisor_lifecycle(command_lower: str) -> bool:
    return any(re.search(pattern, command_lower) for pattern in _SUPERVISOR_PATTERNS)
