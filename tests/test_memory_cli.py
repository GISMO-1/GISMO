import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class MemoryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_memory_put_get_search_delete_and_events(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--key",
                "default_model",
                "--kind",
                "preference",
                "--value-text",
                "phi3:mini",
                "--confidence",
                "high",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)

        get = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--json",
                "default_model",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(get.returncode, 0, get.stderr)
        item = json.loads(get.stdout)
        self.assertEqual(item["value"], "phi3:mini")

        search = _run_cli(
            [
                "memory",
                "search",
                "phi3",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        results = json.loads(search.stdout)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "default_model")

        delete = _run_cli(
            [
                "memory",
                "delete",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "default_model",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(delete.returncode, 0, delete.stderr)

        get_missing = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "default_model",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(get_missing.returncode, 0)
        self.assertIn("not found", get_missing.stdout.lower())

        get_tombstoned = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--include-tombstoned",
                "--json",
                "default_model",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(get_tombstoned.returncode, 0, get_tombstoned.stderr)
        tombstoned_item = json.loads(get_tombstoned.stdout)
        self.assertTrue(tombstoned_item["is_tombstoned"])

        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.cursor()
            count = cursor.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        self.assertEqual(count, 6)

    def test_memory_upsert_and_ordering(self) -> None:
        first_put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--key",
                "alpha",
                "--kind",
                "note",
                "--value-text",
                "first",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(first_put.returncode, 0, first_put.stderr)

        second_put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--key",
                "beta",
                "--kind",
                "note",
                "--value-text",
                "second",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(second_put.returncode, 0, second_put.stderr)

        upsert = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--key",
                "alpha",
                "--kind",
                "note",
                "--value-text",
                "updated",
                "--confidence",
                "medium",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(upsert.returncode, 0, upsert.stderr)

        get_alpha = _run_cli(
            [
                "memory",
                "get",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--json",
                "alpha",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(get_alpha.returncode, 0, get_alpha.stderr)
        alpha_item = json.loads(get_alpha.stdout)
        self.assertEqual(alpha_item["value"], "updated")
        self.assertEqual(alpha_item["confidence"], "medium")

        search = _run_cli(
            [
                "memory",
                "search",
                "",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        results = json.loads(search.stdout)
        keys = [item["key"] for item in results]
        self.assertEqual(keys[0], "alpha")
        self.assertEqual(keys[1], "beta")

    def test_memory_db_file_released_after_cli(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--namespace",
                "global",
                "--key",
                "lock_check",
                "--kind",
                "note",
                "--value-text",
                "released",
                "--confidence",
                "high",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        self.assertTrue(self.db_path.exists())
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
