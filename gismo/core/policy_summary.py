"""Policy summary helpers for planner prompts and explain artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gismo.core.permissions import PermissionPolicy


_WRITE_TOOL_NAMES = {"write_note", "write_file"}


@dataclass(frozen=True)
class PolicySummary:
    allowed_tools: list[str]
    write_tools: list[str]
    shell_allowed: bool
    shell_allowlist_summary: str
    fs_base_dir: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "write_tools": list(self.write_tools),
            "shell_allowed": self.shell_allowed,
            "shell_allowlist_summary": self.shell_allowlist_summary,
            "fs_base_dir": self.fs_base_dir,
        }

    def prompt_lines(self) -> list[str]:
        allowed = ", ".join(self.allowed_tools) if self.allowed_tools else "(none)"
        write_tools = ", ".join(self.write_tools) if self.write_tools else "(none)"
        shell_status = "allowed" if self.shell_allowed else "blocked"
        return [
            "Policy constraints (deny-by-default):",
            f"- allowed_tools: {allowed}",
            f"- write_tools_allowed: {write_tools}",
            f"- shell: {shell_status}; {self.shell_allowlist_summary}",
            f"- fs_base_dir: {self.fs_base_dir}",
        ]

    def explain_summary(self) -> str:
        allowed = ", ".join(self.allowed_tools) if self.allowed_tools else "none"
        return f"allowed_tools=[{allowed}] (deny-by-default); {self.shell_allowlist_summary}"


def summarize_policy(policy: PermissionPolicy) -> PolicySummary:
    allowed_tools = sorted(policy.allowed_tools)
    write_tools = sorted(tool for tool in allowed_tools if tool in _WRITE_TOOL_NAMES)
    shell_allowed = "run_shell" in policy.allowed_tools
    allowlist_summary = _shell_allowlist_summary(policy.shell.allowlist)
    return PolicySummary(
        allowed_tools=allowed_tools,
        write_tools=write_tools,
        shell_allowed=shell_allowed,
        shell_allowlist_summary=allowlist_summary,
        fs_base_dir=str(policy.fs.base_dir),
    )


def _shell_allowlist_summary(allowlist: Iterable[list[str]]) -> str:
    entries = list(allowlist)
    if not entries:
        return "shell allowlist: (empty)"
    command_names = sorted({entry[0] for entry in entries if entry})
    command_preview = ", ".join(command_names[:3]) if command_names else "(unknown)"
    suffix = "" if len(command_names) <= 3 else "…"
    return f"shell allowlist: {len(entries)} entry(s), commands={command_preview}{suffix}"
