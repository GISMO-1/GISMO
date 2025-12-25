import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


class StateStoreTest(unittest.TestCase):
    def test_daemon_control_state_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            self.assertFalse(state_store.get_daemon_paused())
            state_store.set_daemon_paused(True)
            self.assertTrue(state_store.get_daemon_paused())

            reloaded = StateStore(db_path)
            self.assertTrue(reloaded.get_daemon_paused())
            reloaded.set_daemon_paused(False)
            self.assertFalse(reloaded.get_daemon_paused())

    def test_requeue_stale_in_progress_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            now = datetime.now(timezone.utc)
            stale_start = now - timedelta(minutes=15)
            recent_start = now - timedelta(minutes=2)

            stale_item = state_store.enqueue_command("echo: stale")
            recent_item = state_store.enqueue_command("echo: recent")

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
                        stale_item.id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE queue_items
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        QueueStatus.IN_PROGRESS.value,
                        recent_start.isoformat(),
                        recent_start.isoformat(),
                        recent_item.id,
                    ),
                )
                connection.commit()

            updated = state_store.requeue_stale_in_progress_queue(
                older_than_seconds=10 * 60,
                limit=1,
                now=now,
            )
            self.assertEqual(updated, 1)

            stale = state_store.get_queue_item(stale_item.id)
            recent = state_store.get_queue_item(recent_item.id)
            assert stale is not None
            assert recent is not None
            self.assertEqual(stale.status, QueueStatus.QUEUED)
            self.assertIsNone(stale.started_at)
            self.assertEqual(stale.attempt_count, 1)
            self.assertEqual(recent.status, QueueStatus.IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
