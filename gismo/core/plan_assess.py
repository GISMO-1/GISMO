"""Plan assessment heuristics for GISMO ask plans."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gismo.core.risk import PlanRisk, classify_plan_risk


@dataclass(frozen=True)
class PlanAssessment:
    risk_level: str
    risk_flags: list[str]
    rationale: list[str]
    requires_confirmation: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "risk_level": self.risk_level,
            "risk_flags": list(self.risk_flags),
            "rationale": list(self.rationale),
            "requires_confirmation": self.requires_confirmation,
        }


def assess_plan(actions: Iterable[dict[str, object]]) -> PlanAssessment:
    risk = classify_plan_risk(actions)
    return PlanAssessment(
        risk_level=risk.risk_level,
        risk_flags=risk.risk_flags,
        rationale=risk.rationale,
        requires_confirmation=risk.risk_level in {"MEDIUM", "HIGH"},
    )


def expanded_explanation(assessment: PlanAssessment) -> list[str]:
    if assessment.rationale:
        return list(assessment.rationale)
    return ["No additional risk flags detected."]


def assess_plan_risk(actions: Iterable[dict[str, object]]) -> PlanRisk:
    return classify_plan_risk(actions)
