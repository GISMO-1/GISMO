"""Shared plan helpers — usable from both CLI and web API without circular imports."""
from __future__ import annotations

from typing import TYPE_CHECKING

from gismo.cli.operator import normalize_command
from gismo.core.operator_plan import (
    OperatorPlanValidationError,
    build_operator_plan_binding,
)

if TYPE_CHECKING:
    from gismo.core.state import StateStore


def enqueue_plan_actions(
    state_store: "StateStore",
    plan: dict,
    *,
    run_id: str | None = None,
    approval_id: str | None = None,
) -> tuple[list[str], list[str]]:
    """Iterate *plan['actions']*, validate, and enqueue each as a queue item.

    Returns ``(enqueued_ids, skipped_messages)``.
    This is the canonical enqueue path shared by CLI approval, web API approval,
    and the original ``run_ask`` / ``run_agent`` code.
    """
    from gismo.cli.operator import parse_command  # no circular dep — operator.py is stdlib-only

    enqueued_ids: list[str] = []
    skipped: list[str] = []

    for action in plan.get("actions", []):
        if action.get("type") != "enqueue":
            continue
        command_text = action.get("command") or ""
        action_metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
        operator_plan = action_metadata.get("operator_plan") if isinstance(action_metadata, dict) else None
        if not command_text.strip():
            skipped.append("Skipped enqueue action with empty command.")
            continue
        if operator_plan is None:
            try:
                parse_command(command_text)
            except ValueError as exc:
                skipped.append(f"Skipped invalid command '{command_text}': {exc}")
                continue
        metadata: dict[str, object] = {}
        if approval_id:
            metadata["approval_id"] = approval_id
        if isinstance(action_metadata, dict):
            metadata.update(action_metadata)
        try:
            if operator_plan is not None:
                if "operator_plan_binding" in action_metadata:
                    raise OperatorPlanValidationError(
                        "operator_plan_binding must be generated internally"
                    )
                normalized_command = normalize_command(command_text)
                provided_normalized = str(action_metadata.get("normalized_command") or "").strip()
                if provided_normalized and provided_normalized != normalized_command:
                    raise OperatorPlanValidationError("normalized_command does not match the visible command")
                canonical_plan, binding = build_operator_plan_binding(
                    visible_command=command_text,
                    operator_plan=operator_plan,
                )
                metadata["operator_plan"] = canonical_plan
                metadata["normalized_command"] = normalized_command
                metadata["operator_plan_binding"] = binding
        except OperatorPlanValidationError as exc:
            skipped.append(f"Skipped invalid operator plan for '{command_text}': {exc}")
            state_store.record_security_event(
                event_type="operator_plan_binding_failed",
                actor="planner",
                action="bind",
                resource="queue_item",
                payload={
                    "reason": str(exc),
                    "command_text": str(command_text).strip(),
                },
                related_run_id=run_id,
                related_approval_id=approval_id,
            )
            continue
        item = state_store.enqueue_command(
            command_text=command_text,
            run_id=run_id,
            max_retries=int(action.get("retries") or 0),
            timeout_seconds=int(action.get("timeout_seconds") or 30),
            metadata=metadata or None,
        )
        enqueued_ids.append(item.id)

    return enqueued_ids, skipped
