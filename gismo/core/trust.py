"""Trust labels, provenance, and trust transition helpers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable


TRUST_LABEL_USER_AUTHORED = "user_authored"
TRUST_LABEL_GISMO_INFERRED = "gismo_inferred"
TRUST_LABEL_IMPORTED = "imported"
TRUST_LABEL_EXTERNAL = "external"
TRUST_LABEL_VERIFIED = "verified"
TRUST_LABEL_TRUSTED = "trusted"
TRUST_LABELS = {
    TRUST_LABEL_USER_AUTHORED,
    TRUST_LABEL_GISMO_INFERRED,
    TRUST_LABEL_IMPORTED,
    TRUST_LABEL_EXTERNAL,
    TRUST_LABEL_VERIFIED,
    TRUST_LABEL_TRUSTED,
}

VERIFICATION_STATUS_UNVERIFIED = "unverified"
VERIFICATION_STATUS_VERIFIED = "verified"
VERIFICATION_STATUS_REJECTED = "rejected"
VERIFICATION_STATUSES = {
    VERIFICATION_STATUS_UNVERIFIED,
    VERIFICATION_STATUS_VERIFIED,
    VERIFICATION_STATUS_REJECTED,
}

TRUST_STATE_SEEN = "SEEN"
TRUST_STATE_VERIFIED = "VERIFIED"
TRUST_STATE_TRUSTED = "TRUSTED"
TRUST_STATE_REJECTED = "REJECTED"
TRUST_STATES = {
    TRUST_STATE_SEEN,
    TRUST_STATE_VERIFIED,
    TRUST_STATE_TRUSTED,
    TRUST_STATE_REJECTED,
}


class TrustTransitionError(ValueError):
    """Raised when a trust transition is invalid or underspecified."""


@dataclass(frozen=True)
class TrustMetadata:
    source_type: str
    verification_status: str
    trust_labels: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "verification_status": self.verification_status,
            "trust_labels": list(self.trust_labels),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class TrustTransition:
    labels_before: list[str]
    labels_after: list[str]
    verification_before: str
    verification_after: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels_before": list(self.labels_before),
            "labels_after": list(self.labels_after),
            "verification_before": self.verification_before,
            "verification_after": self.verification_after,
            "reason": self.reason,
        }


def ensure_trust_label(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in TRUST_LABELS:
        allowed = ", ".join(sorted(TRUST_LABELS))
        raise ValueError(f"trust label must be one of: {allowed}")
    return normalized


def normalize_trust_labels(values: Iterable[str] | None) -> list[str]:
    seen: list[str] = []
    for value in values or []:
        normalized = ensure_trust_label(str(value))
        if normalized not in seen:
            seen.append(normalized)
    return seen


def ensure_verification_status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in VERIFICATION_STATUSES:
        allowed = ", ".join(sorted(VERIFICATION_STATUSES))
        raise ValueError(f"verification_status must be one of: {allowed}")
    return normalized


def ensure_trust_state(value: str) -> str:
    normalized = (value or "").strip().upper()
    if normalized not in TRUST_STATES:
        allowed = ", ".join(sorted(TRUST_STATES))
        raise ValueError(f"trust_state must be one of: {allowed}")
    return normalized


def summarize_trust_states(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        normalized = ensure_trust_state(value)
        if normalized not in seen:
            seen.append(normalized)
    return seen


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def trust_metadata_for_source(
    *,
    source: str,
    source_type: str | None = None,
    trust_labels: Iterable[str] | None = None,
    verification_status: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> TrustMetadata:
    normalized_source = (source or "").strip().lower()
    normalized_type = (source_type or "").strip().lower()
    labels = normalize_trust_labels(trust_labels)
    status = (
        ensure_verification_status(verification_status)
        if verification_status is not None
        else None
    )
    if normalized_type in {"model_output", "llm_output", "assistant_reply"} or normalized_source == "llm":
        return TrustMetadata(
            source_type=normalized_type or "model_output",
            verification_status=status or VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=labels or [TRUST_LABEL_GISMO_INFERRED],
            provenance=dict(provenance or {}),
        )
    if normalized_type in {"memory_snapshot", "snapshot_import", "file_import", "import"}:
        return TrustMetadata(
            source_type=normalized_type or "import",
            verification_status=status or VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=labels or [TRUST_LABEL_IMPORTED],
            provenance=dict(provenance or {}),
        )
    if normalized_type in {"plugin_output", "api_response", "network", "device_network", "process_output"}:
        return TrustMetadata(
            source_type=normalized_type,
            verification_status=status or VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=labels or [TRUST_LABEL_EXTERNAL],
            provenance=dict(provenance or {}),
        )
    if normalized_type == "user" or normalized_source in {"operator", "user", "manual", "web"} or normalized_source.startswith("role:"):
        return TrustMetadata(
            source_type=normalized_type or "user",
            verification_status=status or VERIFICATION_STATUS_VERIFIED,
            trust_labels=labels or [
                TRUST_LABEL_USER_AUTHORED,
                TRUST_LABEL_VERIFIED,
                TRUST_LABEL_TRUSTED,
            ],
            provenance=dict(provenance or {}),
        )
    if normalized_source in {"system", "agent", "agent_session"}:
        return TrustMetadata(
            source_type=normalized_type or "system",
            verification_status=status or VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=labels or [TRUST_LABEL_GISMO_INFERRED],
            provenance=dict(provenance or {}),
        )
    return TrustMetadata(
        source_type=normalized_type or "local",
        verification_status=status or VERIFICATION_STATUS_UNVERIFIED,
        trust_labels=labels or [TRUST_LABEL_EXTERNAL],
        provenance=dict(provenance or {}),
    )


def trust_metadata_from_state(
    *,
    trust_state: str,
    source_type: str,
    provenance: dict[str, Any] | None = None,
    trust_labels: Iterable[str] | None = None,
) -> TrustMetadata:
    normalized_state = ensure_trust_state(trust_state)
    labels = normalize_trust_labels(trust_labels)
    status = VERIFICATION_STATUS_UNVERIFIED
    if normalized_state == TRUST_STATE_VERIFIED:
        status = VERIFICATION_STATUS_VERIFIED
        labels = labels or [TRUST_LABEL_VERIFIED]
    elif normalized_state == TRUST_STATE_TRUSTED:
        status = VERIFICATION_STATUS_VERIFIED
        labels = labels or [TRUST_LABEL_VERIFIED, TRUST_LABEL_TRUSTED]
    elif normalized_state == TRUST_STATE_REJECTED:
        status = VERIFICATION_STATUS_REJECTED
    return TrustMetadata(
        source_type=(source_type or "").strip().lower() or "external",
        verification_status=status,
        trust_labels=labels,
        provenance=dict(provenance or {}),
    )


def is_trusted(trust_labels: Iterable[str] | None) -> bool:
    return TRUST_LABEL_TRUSTED in normalize_trust_labels(trust_labels)


def is_execution_trusted(
    *,
    trust_labels: Iterable[str] | None,
    verification_status: str | None,
) -> bool:
    if not is_trusted(trust_labels):
        return False
    return ensure_verification_status(verification_status) != VERIFICATION_STATUS_REJECTED


def is_planning_trusted(
    *,
    trust_labels: Iterable[str] | None,
    verification_status: str | None,
) -> bool:
    return is_execution_trusted(
        trust_labels=trust_labels,
        verification_status=verification_status,
    )


def require_transition_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise TrustTransitionError("Trust promotion requires an explicit reason.")
    return text


def prepare_trust_transition(
    *,
    labels_before: Iterable[str] | None,
    labels_after: Iterable[str] | None,
    verification_before: str | None,
    verification_after: str | None,
    reason: str | None,
) -> TrustTransition:
    before = normalize_trust_labels(labels_before)
    after = normalize_trust_labels(labels_after)
    before_status = ensure_verification_status(verification_before)
    after_status = ensure_verification_status(verification_after)
    if before == after and before_status == after_status:
        raise TrustTransitionError("Trust transition did not change labels or verification status.")
    transition_reason = require_transition_reason(reason)
    if TRUST_LABEL_TRUSTED in after and TRUST_LABEL_TRUSTED not in before:
        transition_reason = require_transition_reason(reason)
    if after_status == VERIFICATION_STATUS_VERIFIED and before_status != VERIFICATION_STATUS_VERIFIED:
        transition_reason = require_transition_reason(reason)
    return TrustTransition(
        labels_before=before,
        labels_after=after,
        verification_before=before_status,
        verification_after=after_status,
        reason=transition_reason,
    )


def tool_output_trust_metadata(tool_name: str) -> TrustMetadata:
    normalized = (tool_name or "").strip().lower()
    if normalized in {"echo", "calendar_control"}:
        return TrustMetadata(
            source_type="local_state",
            verification_status=VERIFICATION_STATUS_VERIFIED,
            trust_labels=[TRUST_LABEL_VERIFIED, TRUST_LABEL_TRUSTED],
            provenance={},
        )
    if normalized in {"read_file", "list_dir"}:
        return TrustMetadata(
            source_type="filesystem",
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=[TRUST_LABEL_IMPORTED],
            provenance={},
        )
    if normalized == "run_shell":
        return TrustMetadata(
            source_type="process_output",
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=[TRUST_LABEL_EXTERNAL],
            provenance={},
        )
    if normalized == "device_control":
        return TrustMetadata(
            source_type="device_network",
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            trust_labels=[TRUST_LABEL_EXTERNAL],
            provenance={},
        )
    return TrustMetadata(
        source_type="tool_output",
        verification_status=VERIFICATION_STATUS_UNVERIFIED,
        trust_labels=[],
        provenance={},
    )
