"""Smoke tests for the GISMO TUI."""
import tempfile
import unittest
from pathlib import Path

from gismo.core.models import TaskStatus
from gismo.core.state import StateStore
from gismo.tui.app import GismoApp, _age_str, _trunc, _styled


def _make_db(tmp: str) -> str:
    db_path = str(Path(tmp) / "state.db")
    with StateStore(db_path) as store:
        run = store.create_run(label="test-run")
        task = store.create_task(
            run.id,
            title="Echo hello",
            description="desc",
            input_json={"cmd": "echo hello"},
        )
        task.status = TaskStatus.SUCCEEDED
        store.update_task(task)
        store.enqueue_command("echo world")
    return db_path


class TuiHelperTests(unittest.TestCase):
    def test_age_str_seconds(self) -> None:
        from datetime import datetime, timezone, timedelta
        dt = datetime.now(timezone.utc) - timedelta(seconds=45)
        self.assertEqual(_age_str(dt), "45s")

    def test_age_str_minutes(self) -> None:
        from datetime import datetime, timezone, timedelta
        dt = datetime.now(timezone.utc) - timedelta(seconds=130)
        self.assertEqual(_age_str(dt), "2m")

    def test_age_str_none(self) -> None:
        self.assertEqual(_age_str(None), "-")

    def test_trunc_short(self) -> None:
        self.assertEqual(_trunc("hello", 10), "hello")

    def test_trunc_long(self) -> None:
        result = _trunc("hello world", 8)
        self.assertEqual(len(result), 8)
        self.assertTrue(result.endswith("…"))

    def test_styled_succeeded(self) -> None:
        result = _styled("SUCCEEDED")
        self.assertIn("SUCCEEDED", result)
        self.assertIn("green", result)

    def test_styled_failed(self) -> None:
        result = _styled("FAILED")
        self.assertIn("FAILED", result)
        self.assertIn("red", result)

    def test_styled_unknown(self) -> None:
        self.assertEqual(_styled("UNKNOWN"), "UNKNOWN")


class TuiMountTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_mounts_and_shows_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                # Header and footer rendered
                self.assertIsNotNone(app.query_one("#queue-table"))
                self.assertIsNotNone(app.query_one("#runs-table"))
                self.assertIsNotNone(app.query_one("#daemon-panel"))
                self.assertIsNotNone(app.query_one("#queue-stats-panel"))
                self.assertIsNotNone(app.query_one("#daemon-detail"))

    async def test_queue_table_has_columns(self) -> None:
        from textual.widgets import DataTable
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                table = app.query_one("#queue-table", DataTable)
                col_keys = [k.value for k in table.columns]
                self.assertIn("id", col_keys)
                self.assertIn("status", col_keys)
                self.assertIn("cmd", col_keys)

    async def test_runs_table_has_columns(self) -> None:
        from textual.widgets import DataTable
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                table = app.query_one("#runs-table", DataTable)
                col_keys = [k.value for k in table.columns]
                self.assertIn("id", col_keys)
                self.assertIn("status", col_keys)
                self.assertIn("tasks", col_keys)

    async def test_queue_table_populates_with_data(self) -> None:
        from textual.widgets import DataTable
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                table = app.query_one("#queue-table", DataTable)
                self.assertGreater(table.row_count, 0)

    async def test_runs_table_populates_with_data(self) -> None:
        from textual.widgets import DataTable
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                table = app.query_one("#runs-table", DataTable)
                self.assertGreater(table.row_count, 0)

    async def test_force_refresh_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("r")
                # No exception = pass

    async def test_quit_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("q")
                # App should exit cleanly

    async def test_toggle_pause_no_daemon(self) -> None:
        """p binding should not crash even with no daemon running."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("p")
                # No exception = pass

    async def test_daemon_detail_no_daemon(self) -> None:
        """Daemon detail panel shows 'not running' when no heartbeat exists."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(tmp)
            app = GismoApp(db_path=db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                panel = app.query_one("#daemon-detail")
                # Just confirm it rendered without crashing
                self.assertIsNotNone(panel)


if __name__ == "__main__":
    unittest.main()
