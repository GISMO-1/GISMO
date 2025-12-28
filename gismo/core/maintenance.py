"""Maintenance helpers for queue hygiene."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from gismo.core.state import StateStore


@dataclass
class MaintenanceSummary:
    stale_minutes: int
    requeued_ids: list[str]
    timestamp: datetime

    @property
    def requeued_count(self) -> int:
        return len(self.requeued_ids)


def run_maintenance_iteration(state_store: StateStore, stale_minutes: int) -> MaintenanceSummary:
    if stale_minutes < 0:
        raise ValueError("stale_minutes must be >= 0")
    now = datetime.now(timezone.utc)
    older_than_seconds = stale_minutes * 60
    stale_ids = state_store.list_stale_in_progress_queue_ids(
        older_than_seconds=older_than_seconds,
        now=now,
    )
    updated = state_store.requeue_stale_in_progress_queue(
        older_than_seconds=older_than_seconds,
        now=now,
    )
    requeued_ids = stale_ids[:updated] if updated < len(stale_ids) else stale_ids
    if updated:
        payload = {
            "stale_minutes": stale_minutes,
            "requeued_count": updated,
            "requeued_ids": requeued_ids,
        }
        state_store.record_event(
            actor="maintain",
            event_type="queue_requeue_stale",
            message=f"Requeued {updated} stale queue item(s).",
            json_payload=payload,
        )
    return MaintenanceSummary(
        stale_minutes=stale_minutes,
        requeued_ids=requeued_ids,
        timestamp=now,
    )
