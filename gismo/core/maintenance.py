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
    dry_run: bool

    @property
    def requeued_count(self) -> int:
        return len(self.requeued_ids)


def run_maintenance_iteration(
    state_store: StateStore,
    stale_minutes: int,
    *,
    dry_run: bool = False,
) -> MaintenanceSummary:
    if stale_minutes < 0:
        raise ValueError("stale_minutes must be >= 0")
    now = datetime.now(timezone.utc)
    older_than_seconds = stale_minutes * 60
    stale_ids = state_store.list_stale_in_progress_queue_ids(
        older_than_seconds=older_than_seconds,
        now=now,
    )
    if dry_run:
        payload = {
            "stale_minutes": stale_minutes,
            "stale_count": len(stale_ids),
            "requeued_count": 0,
            "requeued_ids": [],
            "dry_run": True,
        }
        state_store.record_event(
            actor="maintain",
            event_type="maintenance_check",
            message="Maintenance dry run completed.",
            json_payload=payload,
        )
        return MaintenanceSummary(
            stale_minutes=stale_minutes,
            requeued_ids=stale_ids,
            timestamp=now,
            dry_run=True,
        )

    updated = state_store.requeue_stale_in_progress_queue(
        older_than_seconds=older_than_seconds,
        now=now,
    )
    requeued_ids = stale_ids[:updated] if updated < len(stale_ids) else stale_ids
    payload = {
        "stale_minutes": stale_minutes,
        "stale_count": len(stale_ids),
        "requeued_count": updated,
        "requeued_ids": requeued_ids,
        "dry_run": False,
    }
    if updated:
        state_store.record_event(
            actor="maintain",
            event_type="queue_requeue_stale",
            message=f"Requeued {updated} stale queue item(s).",
            json_payload=payload,
        )
    else:
        state_store.record_event(
            actor="maintain",
            event_type="maintenance_check",
            message="No stale queue items to requeue.",
            json_payload=payload,
        )
    return MaintenanceSummary(
        stale_minutes=stale_minutes,
        requeued_ids=requeued_ids,
        timestamp=now,
        dry_run=False,
    )
