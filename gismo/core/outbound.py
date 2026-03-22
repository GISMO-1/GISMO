"""Common outbound policy checks with security telemetry."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlparse

from gismo.core.permissions import NetworkDecision, NetworkPermissionError, NetworkPolicy, load_policy
from gismo.core.security_events import append_security_event


def runtime_network_policy() -> NetworkPolicy:
    repo_root = Path(__file__).resolve().parents[2]
    policy_path = repo_root / "policy" / "readonly.json"
    if not policy_path.exists():
        return NetworkPolicy(default_action="deny")
    return load_policy(str(policy_path), repo_root=repo_root).network


def check_outbound_target(
    *,
    component: str,
    target: str,
    actor: str,
    action: str = "connect",
    policy: NetworkPolicy | None = None,
    db_path: str | None = None,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    related_approval_id: str | None = None,
) -> NetworkDecision:
    resolved_policy = policy or runtime_network_policy()
    decision = resolved_policy.evaluate_target(component, target)
    _record_outbound_event(
        event_type="outbound_allowed" if decision.allowed else "outbound_denied",
        actor=actor,
        action=action,
        resource=target,
        db_path=db_path,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
        related_approval_id=related_approval_id,
        payload={
            "component": component,
            "target": target,
            "destination_class": destination_class_for_target(target),
            "decision": decision.to_dict(),
        },
    )
    if not decision.allowed:
        raise NetworkPermissionError(
            f"Network access denied for {component} target {target}.",
            decision=decision,
        )
    return decision


def check_outbound_scope(
    *,
    component: str,
    scope: str,
    actor: str,
    action: str = "connect",
    policy: NetworkPolicy | None = None,
    db_path: str | None = None,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    related_approval_id: str | None = None,
) -> NetworkDecision:
    resolved_policy = policy or runtime_network_policy()
    decision = resolved_policy.evaluate_scope(component, scope)
    _record_outbound_event(
        event_type="outbound_allowed" if decision.allowed else "outbound_denied",
        actor=actor,
        action=action,
        resource=f"scope:{scope}",
        db_path=db_path,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
        related_approval_id=related_approval_id,
        payload={
            "component": component,
            "scope": scope,
            "destination_class": scope,
            "decision": decision.to_dict(),
        },
    )
    if not decision.allowed:
        raise NetworkPermissionError(
            f"Network access denied for {component} ({scope}).",
            decision=decision,
        )
    return decision


def destination_class_for_target(target: str) -> str:
    host = _extract_host(target)
    if host is None:
        return "unknown"
    lowered = host.lower()
    if lowered == "localhost":
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "hostname"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    return "public"


def _record_outbound_event(
    *,
    event_type: str,
    actor: str,
    action: str,
    resource: str,
    payload: dict[str, object],
    db_path: str | None,
    related_run_id: str | None,
    related_task_id: str | None,
    related_plan_id: str | None,
    related_approval_id: str | None,
) -> None:
    if not db_path or not str(db_path).strip():
        return
    append_security_event(
        db_path=db_path,
        event_type=event_type,
        actor=actor,
        action=action,
        resource=resource,
        payload=payload,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
        related_approval_id=related_approval_id,
    )


def _extract_host(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme and parsed.hostname:
        return parsed.hostname
    text = (target or "").strip()
    if not text:
        return None
    if text.lower() == "localhost":
        return "localhost"
    if "://" not in text and text.count(":") == 1:
        return text.split(":", 1)[0]
    if "://" not in text:
        return text
    return None
