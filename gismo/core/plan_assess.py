"""Plan assessment heuristics for GISMO ask plans."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Literal

from gismo.core.permissions import PermissionPolicy


ConfidenceLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class PlanAssessment:
    confidence: ConfidenceLevel
    risk_flags: list[str]
    explanation: str
    requires_confirmation: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "confidence": self.confidence,
            "risk_flags": list(self.risk_flags),
            "explanation": self.explanation,
            "requires_confirmation": self.requires_confirmation,
        }


_DESTRUCTIVE_TOKENS = (
    "rm ",
    "del ",
    "rmdir",
    "format",
    "shutdown",
    "reboot",
    "reg delete",
    "diskpart",
)
_WRITE_PREFIXES = ("write:", "file_write:", "append:", "note:")
_SHELL_PREFIX = "shell:"
_FLAG_ORDER = [
    "shell",
    "writes",
    "many_steps",
    "too_many_steps",
    "destructive_intent",
    "policy_denied_in_plan",
]
_FLAG_DETAILS = {
    "shell": "Includes shell commands.",
    "writes": "Includes write actions.",
    "many_steps": "Plan has more than 5 actions.",
    "too_many_steps": "Plan has more than 12 actions.",
    "destructive_intent": "Command text contains destructive keywords.",
    "policy_denied_in_plan": "Policy would deny one or more tools.",
}
_WRITE_TOOLS = {"write_note"}


def assess_plan(
    actions: Iterable[dict[str, object]],
    *,
    policy: PermissionPolicy | None = None,
) -> PlanAssessment:
    actions_list = list(actions)
    risk_flags: set[str] = set()
    confidence: ConfidenceLevel = "high"

    action_count = len(actions_list)
    if action_count > 12:
        risk_flags.add("too_many_steps")
        confidence = "low"
    elif action_count > 5:
        risk_flags.add("many_steps")
        confidence = _cap_confidence(confidence, "medium")

    for action in actions_list:
        command = action.get("command")
        command_text = command if isinstance(command, str) else str(command) if command else ""
        command_lower = command_text.strip().lower()
        if command_lower.startswith(_SHELL_PREFIX):
            risk_flags.add("shell")
            confidence = _cap_confidence(confidence, "medium")
        if command_lower.startswith(_WRITE_PREFIXES):
            risk_flags.add("writes")
            confidence = _cap_confidence(confidence, "medium")
        if any(token in command_lower for token in _DESTRUCTIVE_TOKENS):
            risk_flags.add("destructive_intent")
            confidence = "low"
        tool_names = _infer_tools_from_command(command_text)
        if _contains_write_tool(tool_names):
            risk_flags.add("writes")
            confidence = _cap_confidence(confidence, "medium")
        if policy is not None and tool_names:
            for tool_name in tool_names:
                if _tool_denied(policy, tool_name):
                    risk_flags.add("policy_denied_in_plan")
                    confidence = "low"
                    break

    ordered_flags = _order_flags(risk_flags)
    requires_confirmation = _requires_confirmation(confidence, ordered_flags)
    explanation = _build_explanation(confidence, ordered_flags, action_count)
    return PlanAssessment(
        confidence=confidence,
        risk_flags=ordered_flags,
        explanation=explanation,
        requires_confirmation=requires_confirmation,
    )


def expanded_explanation(assessment: PlanAssessment) -> list[str]:
    if not assessment.risk_flags:
        return ["No additional risk flags detected."]
    details: list[str] = []
    for flag in assessment.risk_flags:
        detail = _FLAG_DETAILS.get(flag)
        if detail:
            details.append(detail)
    return details


def _cap_confidence(current: ConfidenceLevel, cap: ConfidenceLevel) -> ConfidenceLevel:
    if current == "low":
        return "low"
    if cap == "medium" and current == "high":
        return "medium"
    return current


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


def _requires_confirmation(confidence: ConfidenceLevel, flags: list[str]) -> bool:
    if confidence == "low":
        return True
    return any(
        flag in flags
        for flag in ("destructive_intent", "policy_denied_in_plan", "too_many_steps")
    )


def _build_explanation(confidence: ConfidenceLevel, flags: list[str], action_count: int) -> str:
    if not flags:
        return (
            f"Plan has {action_count} action(s) with no flagged risks; "
            f"confidence is {confidence}."
        )
    phrases = []
    for flag in flags:
        phrase = _flag_phrase(flag)
        if phrase:
            phrases.append(phrase)
    reasons = ", ".join(phrases)
    return (
        f"Plan has {action_count} action(s) and {reasons}, "
        f"so confidence is {confidence}."
    )


def _flag_phrase(flag: str) -> str | None:
    mapping = {
        "shell": "includes shell commands",
        "writes": "includes write actions",
        "many_steps": "has more than 5 actions",
        "too_many_steps": "has more than 12 actions",
        "destructive_intent": "contains destructive keywords",
        "policy_denied_in_plan": "appears to violate policy",
    }
    return mapping.get(flag)


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


def _tool_denied(policy: PermissionPolicy, tool_name: str) -> bool:
    try:
        policy.check_tool_allowed(tool_name)
    except PermissionError:
        return True
    except Exception:
        return False
    return False
