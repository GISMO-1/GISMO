from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _event_table_exists(db_path: Path) -> bool:
    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
    return row is not None


def test_maintain_once_no_stale_items(repo_root: Path, db_path: Path) -> None:
    proc = _run_cli(
        ["maintain", "--db", str(db_path), "--once", "--stale-minutes", "10"],
        cwd=repo_root,
    )
    assert proc.returncode == 0, proc.stderr
    assert "maintain: no stale items (stale_minutes=10)" in proc.stdout
    assert _event_table_exists(db_path)

    state_store = StateStore(str(db_path))
    events = state_store.list_events()
    assert events == []


def test_maintain_once_requeues_stale_items(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    item = state_store.enqueue_command("echo: stale")
    stale_start = datetime.now(timezone.utc) - timedelta(minutes=20)
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

    proc = _run_cli(
        ["maintain", "--db", str(db_path), "--once", "--stale-minutes", "10"],
        cwd=repo_root,
    )
    assert proc.returncode == 0, proc.stderr
    assert "maintain: requeued 1 stale items (stale_minutes=10)" in proc.stdout

    updated = state_store.get_queue_item(item.id)
    assert updated is not None
    assert updated.status == QueueStatus.QUEUED
    assert updated.attempt_count == 1

    events = state_store.list_events()
    assert len(events) == 1
    event = events[0]
    assert event.actor == "maintain"
    assert event.event_type == "queue_requeue_stale"
    assert event.json_payload is not None
    assert item.id in event.json_payload["requeued_ids"]
