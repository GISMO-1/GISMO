"""Signed capability tokens for zero-trust tool execution."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


_CANONICAL_KWARGS = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}
DEFAULT_CAPABILITY_TTL_SECONDS = 900


class CapabilityError(PermissionError):
    """Raised when a tool capability is missing, invalid, or expired."""


@dataclass(frozen=True)
class CapabilityClaims:
    capability_id: str
    subject: str
    action: str
    resource: str
    constraints: dict[str, Any]
    issued_at: str
    expires_at: str
    run_id: str
    task_id: str
    plan_event_id: str | None = None
    approval_id: str | None = None
    queue_item_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "subject": self.subject,
            "action": self.action,
            "resource": self.resource,
            "constraints": dict(self.constraints),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_event_id": self.plan_event_id,
            "approval_id": self.approval_id,
            "queue_item_id": self.queue_item_id,
        }


@dataclass(frozen=True)
class CapabilityToken:
    claims: CapabilityClaims
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": self.claims.to_dict(),
            "signature": self.signature,
        }

    def serialize(self) -> str:
        return canonical_json(self.to_dict())


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, **_CANONICAL_KWARGS)


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def resource_for_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name in {"read_file", "write_file", "list_dir"}:
        path = str(tool_input.get("path") or ".").strip() or "."
        return f"fs:{path}"
    if tool_name == "run_shell":
        command = tool_input.get("command")
        head = command[0] if isinstance(command, list) and command else "*"
        return f"shell:{head}"
    if tool_name == "device_control":
        action = str(tool_input.get("action") or "*").strip() or "*"
        target = str(tool_input.get("target") or "*").strip() or "*"
        return f"device:{action}:{target}"
    if tool_name == "calendar_control":
        action = str(tool_input.get("action") or "*").strip() or "*"
        return f"calendar:{action}"
    return f"tool:{tool_name}"


def build_tool_capability(
    *,
    secret: str,
    subject: str,
    tool_name: str,
    tool_input: dict[str, Any],
    run_id: str,
    task_id: str,
    ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
    plan_event_id: str | None = None,
    approval_id: str | None = None,
    queue_item_id: str | None = None,
    capability_id: str | None = None,
) -> str:
    now = _utc_now()
    ttl_seconds = max(1, int(ttl_seconds))
    claims = CapabilityClaims(
        capability_id=capability_id or str(uuid4()),
        subject=subject.strip() or "system",
        action="execute",
        resource=resource_for_tool(tool_name, tool_input),
        constraints={
            "tool_name": tool_name,
            "input_sha256": payload_sha256(tool_input),
        },
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        run_id=run_id,
        task_id=task_id,
        plan_event_id=plan_event_id,
        approval_id=approval_id,
        queue_item_id=queue_item_id,
    )
    signature = _sign_claims(claims, secret)
    return CapabilityToken(claims=claims, signature=signature).serialize()


def parse_capability_token(token_text: str) -> CapabilityToken:
    try:
        payload = json.loads(token_text)
    except json.JSONDecodeError as exc:
        raise CapabilityError("Capability token is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise CapabilityError("Capability token must be a JSON object.")
    claims_payload = payload.get("claims")
    signature = payload.get("signature")
    if not isinstance(claims_payload, dict):
        raise CapabilityError("Capability token is missing claims.")
    if not isinstance(signature, str) or not signature:
        raise CapabilityError("Capability token is missing a signature.")
    claims = _claims_from_payload(claims_payload)
    return CapabilityToken(claims=claims, signature=signature)


def verify_tool_capability(
    token_text: str | None,
    *,
    secret: str,
    run_id: str,
    task_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    now: datetime | None = None,
) -> CapabilityClaims:
    if not token_text:
        raise CapabilityError("Missing capability token for tool execution.")
    token = parse_capability_token(token_text)
    expected_signature = _sign_claims(token.claims, secret)
    if not hmac.compare_digest(token.signature, expected_signature):
        raise CapabilityError("Capability signature check failed.")

    claims = token.claims
    current = now or _utc_now()
    expires_at = _parse_dt(claims.expires_at)
    if expires_at <= current:
        raise CapabilityError("Capability has expired.")
    if claims.action != "execute":
        raise CapabilityError("Capability action is not valid for tool execution.")
    if claims.run_id != run_id:
        raise CapabilityError("Capability run scope does not match this run.")
    if claims.task_id != task_id:
        raise CapabilityError("Capability task scope does not match this task.")

    expected_resource = resource_for_tool(tool_name, tool_input)
    if claims.resource != expected_resource:
        raise CapabilityError("Capability resource does not match this tool call.")
    if claims.constraints.get("tool_name") != tool_name:
        raise CapabilityError("Capability tool constraint does not match this tool.")
    expected_input_hash = payload_sha256(tool_input)
    if claims.constraints.get("input_sha256") != expected_input_hash:
        raise CapabilityError("Capability input scope does not match this tool input.")
    return claims


def capability_summary(claims: CapabilityClaims, *, valid: bool, reason: str | None = None) -> dict[str, Any]:
    payload = {
        "capability_id": claims.capability_id,
        "subject": claims.subject,
        "action": claims.action,
        "resource": claims.resource,
        "constraints": dict(claims.constraints),
        "issued_at": claims.issued_at,
        "expires_at": claims.expires_at,
        "run_id": claims.run_id,
        "task_id": claims.task_id,
        "plan_event_id": claims.plan_event_id,
        "approval_id": claims.approval_id,
        "queue_item_id": claims.queue_item_id,
        "valid": valid,
    }
    if reason:
        payload["reason"] = reason
    return payload


def capability_summary_from_token(
    token_text: str | None,
    *,
    valid: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    if not token_text:
        return {"valid": valid, "reason": reason or "missing"}
    try:
        token = parse_capability_token(token_text)
    except CapabilityError as exc:
        return {"valid": valid, "reason": reason or str(exc)}
    return capability_summary(token.claims, valid=valid, reason=reason)


def _claims_from_payload(payload: dict[str, Any]) -> CapabilityClaims:
    capability_id = _require_str(payload.get("capability_id"), "capability_id")
    subject = _require_str(payload.get("subject"), "subject")
    action = _require_str(payload.get("action"), "action")
    resource = _require_str(payload.get("resource"), "resource")
    constraints = payload.get("constraints")
    if not isinstance(constraints, dict):
        raise CapabilityError("Capability constraints must be an object.")
    issued_at = _require_str(payload.get("issued_at"), "issued_at")
    expires_at = _require_str(payload.get("expires_at"), "expires_at")
    run_id = _require_str(payload.get("run_id"), "run_id")
    task_id = _require_str(payload.get("task_id"), "task_id")
    return CapabilityClaims(
        capability_id=capability_id,
        subject=subject,
        action=action,
        resource=resource,
        constraints=dict(constraints),
        issued_at=issued_at,
        expires_at=expires_at,
        run_id=run_id,
        task_id=task_id,
        plan_event_id=_optional_str(payload.get("plan_event_id")),
        approval_id=_optional_str(payload.get("approval_id")),
        queue_item_id=_optional_str(payload.get("queue_item_id")),
    )


def _sign_claims(claims: CapabilityClaims, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_json(claims.to_dict()).encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"Capability {field} must be a non-empty string.")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    raise CapabilityError("Capability optional string fields must be strings when present.")
