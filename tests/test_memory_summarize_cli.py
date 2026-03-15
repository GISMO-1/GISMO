"""Tests for `gismo memory summarize run` CLI command."""
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.core.models import Task, TaskStatus
from gismo.core.state import StateStore


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def _latest_event(db_path: Path, operation: str) -> dict | None:
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute(
            "SELECT result_meta_json FROM memory_events "
            "WHERE operation = ? ORDER BY timestamp DESC LIMIT 1",
            (operation,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _setup_run_with_tasks(
    db_path: Path,
    *,
    label: str = "test-run",
    task_count: int = 2,
    succeeded: int = 1,
    failed: int = 1,
) -> tuple[str, list[str]]:
    """Create a run with tasks in the state store. Returns (run_id, task_ids)."""
    with StateStore(str(db_path)) as store:
        run = store.create_run(label=label)
        task_ids: list[str] = []
        for i in range(task_count):
            task = store.create_task(
                run.id,
                title=f"Task {i}",
                description=f"Description {i}",
                input_json={"step": i},
            )
            # Manually update status
            if i < succeeded:
                task.status = TaskStatus.SUCCEEDED
            elif i < succeeded + failed:
                task.status = TaskStatus.FAILED
                task.error = "something went wrong"
            store.update_task(task)
            task_ids.append(task.id)
        return run.id, task_ids


class MemorySummarizeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_dry_run_shows_plan(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--dry-run",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertIn("1 memory item", result.stdout)
        self.assertIn(f"run/{run_id}/summary", result.stdout)

    def test_dry_run_json_output(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--dry-run",
                "--json",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["namespace"], "project:test")
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["kind"], "summary")
        self.assertEqual(item["source"], "system")
        self.assertIn("run_id", item["value"])
        self.assertIn("task_count", item["value"])

    def test_apply_writes_memory_item(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(run_id, result.stdout)
        self.assertIn("1 memory item", result.stdout)

        # Verify the item was actually written
        get_result = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--json",
                f"run/{run_id}/summary",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(get_result.returncode, 0, get_result.stderr)
        item = json.loads(get_result.stdout)
        self.assertEqual(item["kind"], "summary")
        self.assertEqual(item["source"], "system")
        self.assertEqual(item["confidence"], "medium")
        self.assertIn("run_summary", item["tags"])
        value = item["value"]
        self.assertEqual(value["run_id"], run_id)
        self.assertEqual(value["task_count"], 2)

    def test_apply_with_include_outputs(self) -> None:
        run_id, task_ids = _setup_run_with_tasks(
            self.db_path, task_count=2, succeeded=2, failed=0
        )
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--include-outputs",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 memory item", result.stdout)

        # Check task items were written
        for task_id in task_ids:
            get_result = _run_cli(
                [
                    "memory",
                    "get",
                    "--db",
                    str(self.db_path),
                    "--namespace",
                    "project:test",
                    "--json",
                    f"run/{run_id}/task/{task_id}",
                ],
                cwd=self.repo_root,
            )
            self.assertEqual(get_result.returncode, 0, get_result.stderr)
            item = json.loads(get_result.stdout)
            self.assertEqual(item["kind"], "summary")
            self.assertIn("task_output", item["tags"])

    def test_json_output_on_apply(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--yes",
                "--json",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["item_count"], 1)

    def test_audit_events_recorded(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        event = _latest_event(self.db_path, "memory.summarize.run")
        self.assertIsNotNone(event)
        self.assertEqual(event["status"], "applied")
        self.assertEqual(event["item_count"], 1)

    def test_dry_run_records_audit_event(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--dry-run",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        event = _latest_event(self.db_path, "memory.summarize.run")
        self.assertIsNotNone(event)
        self.assertEqual(event["status"], "dry_run")

    def test_run_not_found(self) -> None:
        # Initialize the DB by creating a run (so tables exist), then use a fake ID
        _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                "nonexistent-run-id",
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Run not found", result.stderr)

    def test_confirmation_required_non_interactive(self) -> None:
        """global namespace requires confirmation; non-interactive should fail closed."""
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--non-interactive",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Confirmation required", result.stderr)

    def test_high_confidence_flag(self) -> None:
        run_id, _ = _setup_run_with_tasks(self.db_path)
        result = _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--confidence",
                "high",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        get_result = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--json",
                f"run/{run_id}/summary",
            ],
            cwd=self.repo_root,
        )
        item = json.loads(get_result.stdout)
        self.assertEqual(item["confidence"], "high")

    def test_status_counts_in_summary(self) -> None:
        run_id, _ = _setup_run_with_tasks(
            self.db_path, task_count=3, succeeded=2, failed=1
        )
        _run_cli(
            [
                "memory",
                "summarize",
                "run",
                run_id,
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--yes",
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        get_result = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "project:test",
                "--json",
                f"run/{run_id}/summary",
            ],
            cwd=self.repo_root,
        )
        item = json.loads(get_result.stdout)
        value = item["value"]
        self.assertEqual(value["task_count"], 3)
        self.assertEqual(value["succeeded"], 2)
        self.assertEqual(value["failed"], 1)


if __name__ == "__main__":
    unittest.main()
