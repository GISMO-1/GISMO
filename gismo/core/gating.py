"""Centralized confirmation gating for plan execution."""
from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Callable, Literal

from gismo.core.policy_summary import PolicySummary
from gismo.core.risk import PlanRisk


CommandContext = Literal["ask", "agent", "agent_session"]


@dataclass(frozen=True)
class ConfirmationDecision:
    required: bool
    confirmed: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "required": self.required,
            "confirmed": self.confirmed,
            "reason": self.reason,
        }


def confirm_plan_gate(
    risk: PlanRisk,
    *,
    yes: bool,
    non_interactive: bool,
    dry_run: bool,
    context: CommandContext,
    policy_summary: PolicySummary | None,
    is_interactive_tty: Callable[[], bool],
) -> ConfirmationDecision:
    required = risk.risk_level in {"MEDIUM", "HIGH"}
    if dry_run:
        return ConfirmationDecision(required=required, confirmed=True, reason="dry-run")
    if not required:
        return ConfirmationDecision(required=False, confirmed=True, reason="not-required")
    if yes:
        return ConfirmationDecision(required=True, confirmed=True, reason="yes")

    summary_hint = _policy_hint(policy_summary)
    prompt = (
        f"This {context} plan is {risk.risk_level} risk{summary_hint}. Proceed? [y/N]:"
    )
    if non_interactive or not is_interactive_tty():
        print(
            "Refusing to enqueue without confirmation in non-interactive mode. "
            "Use --yes to override.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    response = input(prompt)
    if response.strip().lower() not in {"y", "yes"}:
        print("Confirmation declined; plan not enqueued.", file=sys.stderr)
        raise SystemExit(2)
    return ConfirmationDecision(required=True, confirmed=True, reason="prompt")


def _policy_hint(policy_summary: PolicySummary | None) -> str:
    if policy_summary is None:
        return ""
    allowed_count = len(policy_summary.allowed_tools)
    return f" (policy allows {allowed_count} tool(s))"
