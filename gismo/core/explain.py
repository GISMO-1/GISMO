"""Explain artifact builder for plans."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gismo.core.policy_summary import PolicySummary
from gismo.core.risk import PlanRisk


MemoryInjectionStatus = Literal["none", "memory", "profile"]


@dataclass(frozen=True)
class PlanExplain:
    summary: str
    risk_level: str
    risk_flags: list[str]
    rationale: list[str]
    allowed_tools_summary: str
    allowed_tools: list[str]
    shell_allowlist_summary: str
    write_permissions: list[str]
    memory_injection: MemoryInjectionStatus
    memory_suggestions: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "risk_level": self.risk_level,
            "risk_flags": list(self.risk_flags),
            "rationale": list(self.rationale),
            "allowed_tools_summary": self.allowed_tools_summary,
            "allowed_tools": list(self.allowed_tools),
            "shell_allowlist_summary": self.shell_allowlist_summary,
            "write_permissions": list(self.write_permissions),
            "memory_injection": self.memory_injection,
            "memory_suggestions": dict(self.memory_suggestions),
        }


def build_plan_explain(
    *,
    plan: dict,
    risk: PlanRisk,
    policy_summary: PolicySummary,
    memory_injection: MemoryInjectionStatus,
    memory_suggestions_count: int,
) -> PlanExplain:
    summary = _plan_summary(plan)
    suggestions_payload = {
        "exists": memory_suggestions_count > 0,
        "count": memory_suggestions_count,
        "advisory": True,
    }
    return PlanExplain(
        summary=summary,
        risk_level=risk.risk_level,
        risk_flags=risk.risk_flags,
        rationale=risk.rationale,
        allowed_tools_summary=policy_summary.explain_summary(),
        allowed_tools=policy_summary.allowed_tools,
        shell_allowlist_summary=policy_summary.shell_allowlist_summary,
        write_permissions=policy_summary.write_tools,
        memory_injection=memory_injection,
        memory_suggestions=suggestions_payload,
    )


def _plan_summary(plan: dict) -> str:
    intent = plan.get("intent") or "unspecified"
    actions = plan.get("actions") or []
    return f"intent={intent} actions={len(actions)}"
