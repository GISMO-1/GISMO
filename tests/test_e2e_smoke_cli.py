import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


class E2ESmokeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def _run_cli(self, args: list[str]) -> None:
        cmd = [sys.executable, "-m", "gismo.cli.main", *args]
        result = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = (
                "CLI command failed: "
                f"{' '.join(cmd)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
            self.fail(message)

    def test_daemon_once_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            self._run_cli(["--db", str(db_path), "enqueue", "echo:smoke-e2e-ok"])
            self._run_cli(
                [
                    "--db",
                    str(db_path),
                    "daemon",
                    "--policy",
                    str(self.policy_path),
                    "--once",
                ]
            )
            with StateStore(str(db_path)) as state_store:
                queued = state_store.list_queue_items_by_status(QueueStatus.QUEUED)
                in_progress = state_store.list_queue_items_by_status(QueueStatus.IN_PROGRESS)
                failed = state_store.list_queue_items_by_status(QueueStatus.FAILED)
                self.assertFalse(queued)
                self.assertFalse(in_progress)
                self.assertFalse(failed)
                self.assertIsNotNone(state_store.get_latest_run())
            try:
                os.remove(db_path)
            except OSError as exc:
                self.fail(f"Expected DB path to be deletable, got error: {exc}")
            self.assertFalse(db_path.exists())
