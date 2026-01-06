import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gismo.memory.store import policy_hash_for_path
from gismo.memory.store import put_item as memory_put_item
from gismo.memory.store import tombstone_item as memory_tombstone_item


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class MemoryDoctorCliTest(unittest.TestCase):
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

    def _init_db(self) -> None:
        policy_hash = policy_hash_for_path(None)
        memory_put_item(
            str(self.db_path),
            namespace="global",
            key="doctor_seed",
            kind="note",
            value="seed",
            tags=None,
            confidence="high",
            source="operator",
            ttl_seconds=None,
            actor="test",
            policy_hash=policy_hash,
        )

    def _drop_index(self, name: str) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(f"DROP INDEX IF EXISTS {name}")
            connection.commit()

    def _doctor_policy(self, require_confirmation: bool = False) -> Path:
        policy = {
            "allowed_tools": [],
            "fs": {"base_dir": "."},
            "memory": {
                "allow": {
                    "memory.doctor.rebuild_indexes": ["global"],
                    "memory.doctor.purge_tombstones": ["global"],
                    "memory.doctor.vacuum": ["global"],
                    "memory.doctor.reindex": ["global"],
                    "memory.doctor.enforce_foreign_keys": ["global"],
                },
                "require_confirmation": {
                    "memory.doctor.rebuild_indexes": ["global"]
                }
                if require_confirmation
                else {},
            },
        }
        return self._write_policy(policy)

    def test_check_clean_db_exit_zero_json_schema(self) -> None:
        self._init_db()
        result = _run_cli(
            [
                "memory",
                "doctor",
                "check",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "clean")
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("summary", payload)
        self.assertIn("checks", payload)
        self.assertIn("integrity", payload["checks"])

    def test_check_missing_index_detection(self) -> None:
        self._init_db()
        self._drop_index("idx_memory_items_kind")
        result = _run_cli(
            [
                "memory",
                "doctor",
                "check",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("idx_memory_items_kind", payload["checks"]["indexes"]["missing"])

    def test_rebuild_indexes_repair_cleans_check(self) -> None:
        self._init_db()
        self._drop_index("idx_memory_items_kind")
        policy_path = self._doctor_policy()
        repair = _run_cli(
            [
                "memory",
                "doctor",
                "repair",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--rebuild-indexes",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(repair.returncode, 0, repair.stderr)
        check = _run_cli(
            [
                "memory",
                "doctor",
                "check",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_purge_tombstones_respects_limit(self) -> None:
        policy_hash = policy_hash_for_path(None)
        memory_put_item(
            str(self.db_path),
            namespace="global",
            key="active_item",
            kind="note",
            value="active",
            tags=None,
            confidence="high",
            source="operator",
            ttl_seconds=None,
            actor="test",
            policy_hash=policy_hash,
        )
        for key in ("old_tombstone_a", "old_tombstone_b"):
            memory_put_item(
                str(self.db_path),
                namespace="global",
                key=key,
                kind="note",
                value="to_delete",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            memory_tombstone_item(
                str(self.db_path),
                namespace="global",
                key=key,
                actor="test",
                policy_hash=policy_hash,
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE memory_items SET updated_at = ? WHERE is_tombstoned = 1",
                (cutoff,),
            )
            connection.commit()

        policy_path = self._doctor_policy()
        repair = _run_cli(
            [
                "memory",
                "doctor",
                "repair",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--purge-tombstones",
                "--namespace",
                "global",
                "--older-than-seconds",
                "1",
                "--limit",
                "1",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(repair.returncode, 0, repair.stderr)
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            tombstones = connection.execute(
                "SELECT COUNT(*) FROM memory_items WHERE is_tombstoned = 1"
            ).fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(*) FROM memory_items WHERE is_tombstoned = 0"
            ).fetchone()[0]
        self.assertEqual(tombstones, 1)
        self.assertEqual(active, 1)

    def test_non_interactive_fail_closed(self) -> None:
        self._init_db()
        self._drop_index("idx_memory_items_kind")
        policy_path = self._doctor_policy(require_confirmation=True)
        repair = _run_cli(
            [
                "memory",
                "doctor",
                "repair",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--rebuild-indexes",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(repair.returncode, 2, repair.stderr)
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("idx_memory_items_kind",),
            ).fetchone()
        self.assertIsNone(row)

    def test_doctor_check_releases_db_handle(self) -> None:
        self._init_db()
        result = _run_cli(
            [
                "memory",
                "doctor",
                "check",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        os.remove(self.db_path)
        self.assertFalse(self.db_path.exists())

    def test_doctor_repair_releases_db_handle(self) -> None:
        self._init_db()
        self._drop_index("idx_memory_items_kind")
        policy_path = self._doctor_policy()
        result = _run_cli(
            [
                "memory",
                "doctor",
                "repair",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--rebuild-indexes",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        os.remove(self.db_path)
        self.assertFalse(self.db_path.exists())
