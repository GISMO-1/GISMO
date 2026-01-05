import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gismo.memory.store import MemoryStore, policy_hash_for_path


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class MemoryRetentionCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_policy(self, policy: dict) -> Path:
        path = Path(self.temp_dir.name) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def _latest_event(self, operation: str) -> tuple[str, dict[str, object]]:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT id, result_meta_json FROM memory_events "
                "WHERE operation = ? "
                "ORDER BY timestamp DESC "
                "LIMIT 1",
                (operation,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0], json.loads(row[1])

    def _insert_item(self, namespace: str, key: str, created_at: str) -> None:
        store = MemoryStore(str(self.db_path))
        store.upsert_item_with_timestamps(
            namespace=namespace,
            key=key,
            kind="note",
            value=key,
            tags=None,
            confidence="low",
            source="operator",
            ttl_seconds=None,
            is_tombstoned=False,
            created_at=created_at,
            updated_at=created_at,
            update_created_at=True,
            actor="operator",
            policy_hash=policy_hash_for_path(None),
            operation="put",
        )

    def test_retention_set_list_show_clear(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.retention.set",
                    "memory.retention.clear",
                ],
                "memory": {
                    "allow": {
                        "memory.retention.set": ["global"],
                        "memory.retention.clear": ["global"],
                    }
                },
            }
        )
        set_result = _run_cli(
            [
                "memory",
                "retention",
                "set",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "global",
                "--max-items",
                "5",
                "--ttl-seconds",
                "3600",
                "--reason",
                "governance",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(set_result.returncode, 0, set_result.stderr)

        list_result = _run_cli(
            [
                "memory",
                "retention",
                "list",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(list_result.returncode, 0, list_result.stderr)
        rules = json.loads(list_result.stdout)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["namespace"], "global")
        self.assertEqual(rules[0]["max_items"], 5)
        self.assertEqual(rules[0]["ttl_seconds"], 3600)

        show_result = _run_cli(
            [
                "memory",
                "retention",
                "show",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
                "global",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(show_result.returncode, 0, show_result.stderr)
        detail = json.loads(show_result.stdout)
        self.assertEqual(detail["namespace"], "global")
        self.assertEqual(detail["item_count"], 0)

        clear_result = _run_cli(
            [
                "memory",
                "retention",
                "clear",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--yes",
                "global",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(clear_result.returncode, 0, clear_result.stderr)

        list_after = _run_cli(
            [
                "memory",
                "retention",
                "list",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(list_after.returncode, 0, list_after.stderr)
        self.assertEqual(json.loads(list_after.stdout), [])

    def test_retention_max_items_eviction_oldest_first_and_linked_audit(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.put",
                    "memory.retention.enforce",
                ],
                "memory": {
                    "allow": {
                        "memory.put": ["global"],
                        "memory.retention.enforce": ["global"],
                    }
                },
            }
        )
        store = MemoryStore(str(self.db_path))
        store.set_retention_rule(
            namespace="global",
            max_items=2,
            ttl_seconds=None,
            policy_source="operator",
        )
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._insert_item("global", "alpha", (base - timedelta(hours=2)).isoformat())
        self._insert_item("global", "beta", (base - timedelta(hours=1)).isoformat())

        put_result = _run_cli(
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
                "gamma",
                "--kind",
                "note",
                "--value-text",
                "new",
                "--confidence",
                "low",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put_result.returncode, 0, put_result.stderr)

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT key, is_tombstoned FROM memory_items WHERE namespace = ?",
                ("global",),
            ).fetchall()
        status = {row[0]: bool(row[1]) for row in rows}
        self.assertTrue(status["alpha"])
        self.assertFalse(status["beta"])
        self.assertFalse(status["gamma"])

        retention_id, meta = self._latest_event("retention.decision")
        evictions = meta["evictions"]
        self.assertEqual(len(evictions), 1)
        self.assertEqual(evictions[0]["key"], "alpha")
        self.assertEqual(evictions[0]["reason"], "max_items")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT result_meta_json FROM memory_events "
                "WHERE operation = ? "
                "ORDER BY timestamp DESC "
                "LIMIT 1",
                ("delete",),
            ).fetchone()
        self.assertIsNotNone(row)
        delete_meta = json.loads(row[0])
        self.assertEqual(delete_meta["retention_event_id"], retention_id)

    def test_retention_ttl_eviction_ordering_is_deterministic(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.put",
                    "memory.retention.enforce",
                ],
                "memory": {
                    "allow": {
                        "memory.put": ["global"],
                        "memory.retention.enforce": ["global"],
                    }
                },
            }
        )
        store = MemoryStore(str(self.db_path))
        store.set_retention_rule(
            namespace="global",
            max_items=None,
            ttl_seconds=3600,
            policy_source="operator",
        )
        now = datetime.now(timezone.utc)
        self._insert_item("global", "oldest", (now - timedelta(hours=3)).isoformat())
        self._insert_item("global", "older", (now - timedelta(hours=2)).isoformat())
        self._insert_item("global", "fresh", (now - timedelta(minutes=10)).isoformat())

        put_result = _run_cli(
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
                "latest",
                "--kind",
                "note",
                "--value-text",
                "new",
                "--confidence",
                "low",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put_result.returncode, 0, put_result.stderr)

        _, meta = self._latest_event("retention.decision")
        evictions = meta["evictions"]
        self.assertEqual([entry["key"] for entry in evictions], ["oldest", "older"])
        self.assertTrue(all(entry["reason"] == "ttl" for entry in evictions))

    def test_retention_confirmation_required_non_interactive_fails_closed(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.put",
                    "memory.retention.enforce",
                ],
                "memory": {
                    "allow": {
                        "memory.put": ["global"],
                        "memory.retention.enforce": ["global"],
                    },
                    "require_confirmation": {
                        "memory.retention.enforce": ["global"],
                    },
                },
            }
        )
        store = MemoryStore(str(self.db_path))
        store.set_retention_rule(
            namespace="global",
            max_items=1,
            ttl_seconds=None,
            policy_source="operator",
        )
        self._insert_item(
            "global",
            "seed",
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )

        put_result = _run_cli(
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
                "new",
                "--confidence",
                "low",
                "--source",
                "operator",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(put_result.returncode, 0)

        with sqlite3.connect(self.db_path) as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM memory_items WHERE is_tombstoned = 0"
            ).fetchone()[0]
        self.assertEqual(active, 1)

        _, meta = self._latest_event("retention.decision")
        self.assertEqual(meta["policy_decision"], "denied")
        self.assertEqual(meta["policy_reason"], "confirmation_required")

    def test_retention_yes_allows_eviction(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [
                    "memory.put",
                    "memory.retention.enforce",
                ],
                "memory": {
                    "allow": {
                        "memory.put": ["global"],
                        "memory.retention.enforce": ["global"],
                    },
                    "require_confirmation": {
                        "memory.retention.enforce": ["global"],
                    },
                },
            }
        )
        store = MemoryStore(str(self.db_path))
        store.set_retention_rule(
            namespace="global",
            max_items=1,
            ttl_seconds=None,
            policy_source="operator",
        )
        self._insert_item(
            "global",
            "seed",
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )

        put_result = _run_cli(
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
                "allowed",
                "--kind",
                "note",
                "--value-text",
                "new",
                "--confidence",
                "low",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(put_result.returncode, 0, put_result.stderr)

        retention_id, meta = self._latest_event("retention.decision")
        self.assertEqual(meta["policy_decision"], "allowed")
        confirmation = meta["confirmation"]
        self.assertTrue(confirmation["provided"])
        self.assertEqual(confirmation["mode"], "yes-flag")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT result_meta_json FROM memory_events "
                "WHERE operation = ? "
                "ORDER BY timestamp DESC "
                "LIMIT 1",
                ("delete",),
            ).fetchone()
        delete_meta = json.loads(row[0])
        self.assertEqual(delete_meta["retention_event_id"], retention_id)


if __name__ == "__main__":
    unittest.main()
