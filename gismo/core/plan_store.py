"""Shared plan helpers — usable from both CLI and web API without circular imports."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gismo.core.state import StateStore


def enqueue_plan_actions(
    state_store: "StateStore",
    plan: dict,
    *,
    run_id: str | None = None,
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
        if not command_text.strip():
            skipped.append("Skipped enqueue action with empty command.")
            continue
        try:
            parse_command(command_text)
        except ValueError as exc:
            skipped.append(f"Skipped invalid command '{command_text}': {exc}")
            continue
        item = state_store.enqueue_command(
            command_text=command_text,
            run_id=run_id,
            max_retries=int(action.get("retries") or 0),
            timeout_seconds=int(action.get("timeout_seconds") or 30),
        )
        enqueued_ids.append(item.id)

    return enqueued_ids, skipped
