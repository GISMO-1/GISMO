from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gismo.core.maintenance import run_maintenance_iteration
from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


def test_run_maintenance_iteration_allows_zero_stale_minutes(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    state_store = StateStore(str(db_path))
    item = state_store.enqueue_command("echo: stale")
    stale_start = datetime.now(timezone.utc) - timedelta(seconds=1)
    with state_store._connection() as connection:  # pylint: disable=protected-access
        connection.execute(
            """
            UPDATE queue_items
            SET status = ?, started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                QueueStatus.IN_PROGRESS.value,
                stale_start.isoformat(),
                stale_start.isoformat(),
                item.id,
            ),
        )
        connection.commit()

    summary = run_maintenance_iteration(state_store, stale_minutes=0)

    assert summary.stale_minutes == 0
    assert summary.requeued_count == 1
    assert summary.dry_run is False
    updated = state_store.get_queue_item(item.id)
    assert updated is not None
    assert updated.status == QueueStatus.QUEUED
