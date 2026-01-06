import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import contextlib
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
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_policy(self, policy: dict) -> Path:
        path = Path(self.temp_dir.name) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def _latest_event_meta(self, operation: str) -> dict[str, object]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT result_meta_json FROM memory_events "
                "WHERE operation = ? "
                "ORDER BY timestamp DESC "
                "LIMIT 1",
                (operation,),
            ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_memory_put_get_search_delete_and_events(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
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
                "--yes",
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
                "--policy",
                str(self.policy_path),
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
                "--policy",
                str(self.policy_path),
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
                "--policy",
                str(self.policy_path),
                "--namespace",
                "global",
                "default_model",
                "--yes",
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
                "--policy",
                str(self.policy_path),
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
                "--policy",
                str(self.policy_path),
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

        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
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
                "--policy",
                str(self.policy_path),
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
                "--yes",
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
                "--policy",
                str(self.policy_path),
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
                "--yes",
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
                "--policy",
                str(self.policy_path),
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
                "--yes",
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
                "--policy",
                str(self.policy_path),
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
                "--policy",
                str(self.policy_path),
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

    def test_memory_put_requires_confirmation_in_non_interactive(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "global",
                "--key",
                "requires_confirm",
                "--kind",
                "note",
                "--value-text",
                "blocked",
                "--confidence",
                "high",
                "--source",
                "operator",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(put.returncode, 0)
        self.assertIn("Confirmation required", put.stderr)

        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM memory_items"
            ).fetchone()[0]
        self.assertEqual(item_count, 0)
        meta = self._latest_event_meta("put")
        self.assertEqual(meta["policy_decision"], "denied")
        self.assertEqual(meta["policy_reason"], "confirmation_required")

    def test_memory_put_yes_records_confirmation(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "global",
                "--key",
                "confirmed",
                "--kind",
                "note",
                "--value-text",
                "allowed",
                "--confidence",
                "high",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        meta = self._latest_event_meta("put")
        self.assertEqual(meta["policy_decision"], "allowed")
        confirmation = meta["confirmation"]
        self.assertTrue(confirmation["required"])
        self.assertTrue(confirmation["provided"])
        self.assertEqual(confirmation["mode"], "yes-flag")

    def test_memory_delete_yes_records_confirmation(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "global",
                "--key",
                "to_delete",
                "--kind",
                "note",
                "--value-text",
                "removable",
                "--confidence",
                "high",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)

        delete = _run_cli(
            [
                "memory",
                "delete",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "global",
                "to_delete",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(delete.returncode, 0, delete.stderr)
        meta = self._latest_event_meta("delete")
        self.assertEqual(meta["policy_decision"], "allowed")
        confirmation = meta["confirmation"]
        self.assertTrue(confirmation["required"])
        self.assertTrue(confirmation["provided"])
        self.assertEqual(confirmation["mode"], "yes-flag")

    def test_memory_put_run_namespace_allowed(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "run:abc123",
                "--key",
                "run_note",
                "--kind",
                "note",
                "--value-text",
                "run-ok",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        meta = self._latest_event_meta("put")
        self.assertEqual(meta["policy_decision"], "allowed")
        confirmation = meta["confirmation"]
        self.assertFalse(confirmation["required"])

    def test_memory_db_file_released_after_cli(self) -> None:
        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
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
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        self.assertTrue(self.db_path.exists())
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

    def test_memory_namespace_list_show_retire_and_ordering(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.put",
                    "memory.delete",
                    "memory.namespace.retire",
                ],
                "memory": {
                    "allow": {
                        "memory.put": ["global", "project:alpha"],
                        "memory.delete": ["global", "project:alpha"],
                        "memory.namespace.retire": ["project:alpha"],
                    }
                },
            }
        )
        put_global = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--namespace",
                "global",
                "--key",
                "alpha",
                "--kind",
                "note",
                "--value-text",
                "one",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put_global.returncode, 0, put_global.stderr)

        delete_global = _run_cli(
            [
                "memory",
                "delete",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--namespace",
                "global",
                "alpha",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(delete_global.returncode, 0, delete_global.stderr)

        put_project = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--namespace",
                "project:alpha",
                "--key",
                "beta",
                "--kind",
                "note",
                "--value-text",
                "two",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put_project.returncode, 0, put_project.stderr)

        retire = _run_cli(
            [
                "memory",
                "namespace",
                "retire",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "project:alpha",
                "--reason",
                "governance",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(retire.returncode, 0, retire.stderr)

        namespace_list = _run_cli(
            [
                "memory",
                "namespace",
                "list",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(namespace_list.returncode, 0, namespace_list.stderr)
        namespaces = json.loads(namespace_list.stdout)
        self.assertEqual([entry["namespace"] for entry in namespaces], ["global", "project:alpha"])
        global_entry = namespaces[0]
        self.assertEqual(global_entry["item_count"], 0)
        self.assertEqual(global_entry["tombstone_count"], 1)
        self.assertIsNotNone(global_entry["last_write_at"])
        project_entry = namespaces[1]
        self.assertEqual(project_entry["item_count"], 1)
        self.assertEqual(project_entry["tombstone_count"], 0)
        self.assertTrue(project_entry["retired"])

        namespace_show = _run_cli(
            [
                "memory",
                "namespace",
                "show",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
                "project:alpha",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(namespace_show.returncode, 0, namespace_show.stderr)
        detail = json.loads(namespace_show.stdout)
        self.assertTrue(detail["retired"])
        self.assertEqual(detail["retired_reason"], "governance")

    def test_memory_namespace_retire_non_interactive_fails_closed(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": ["memory.namespace.retire"],
                "memory": {
                    "allow": {"memory.namespace.retire": ["global"]},
                    "require_confirmation": {"memory.namespace.retire": ["global"]},
                },
            }
        )
        retire = _run_cli(
            [
                "memory",
                "namespace",
                "retire",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--non-interactive",
                "global",
                "--reason",
                "freeze",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(retire.returncode, 0)
        self.assertIn("Confirmation required", retire.stderr)

        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT retired_at FROM memory_namespaces WHERE namespace = ?",
                ("global",),
            ).fetchone()
        self.assertIsNone(row)
        meta = self._latest_event_meta("namespace.retire")
        self.assertEqual(meta["policy_decision"], "denied")
        self.assertEqual(meta["policy_reason"], "confirmation_required")

    def test_memory_namespace_retire_idempotent(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": ["memory.namespace.retire"],
                "memory": {"allow": {"memory.namespace.retire": ["global"]}},
            }
        )
        first = _run_cli(
            [
                "memory",
                "namespace",
                "retire",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "global",
                "--reason",
                "end-of-life",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = _run_cli(
            [
                "memory",
                "namespace",
                "retire",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "global",
                "--reason",
                "end-of-life",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already retired", second.stdout)

    def test_memory_put_denied_for_retired_namespace(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": ["memory.put", "memory.namespace.retire"],
                "memory": {
                    "allow": {
                        "memory.put": ["global"],
                        "memory.namespace.retire": ["global"],
                    }
                },
            }
        )
        retire = _run_cli(
            [
                "memory",
                "namespace",
                "retire",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "global",
                "--reason",
                "compliance",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(retire.returncode, 0, retire.stderr)

        put = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--namespace",
                "global",
                "--key",
                "blocked",
                "--kind",
                "note",
                "--value-text",
                "nope",
                "--confidence",
                "low",
                "--source",
                "operator",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(put.returncode, 0)
        self.assertIn("namespace is retired", put.stderr)
        meta = self._latest_event_meta("put")
        self.assertEqual(meta["policy_action"], "memory.put.retired")
        self.assertEqual(meta["policy_decision"], "denied")


if __name__ == "__main__":
    unittest.main()
