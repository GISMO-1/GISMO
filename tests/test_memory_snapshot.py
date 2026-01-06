import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

from gismo.memory.snapshot import canonical_json, canonical_value_json


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _snapshot_path(base: Path, name: str) -> Path:
    return base / name


def _item_hash(item: dict[str, object]) -> str:
    payload = {
        "namespace": item["namespace"],
        "key": item["key"],
        "kind": item["kind"],
        "value_json": item["value_json"],
        "confidence": item["confidence"],
        "source": item["source"],
        "tags": item["tags"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "is_tombstoned": item["is_tombstoned"],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class MemorySnapshotCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"
        self.snapshot_dir = Path(self.temp_dir.name) / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _put_item(self, namespace: str, key: str, value: str) -> None:
        result = _run_cli(
            [
                "memory",
                "put",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                namespace,
                "--key",
                key,
                "--kind",
                "note",
                "--value-text",
                value,
                "--confidence",
                "high",
                "--source",
                "operator",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_export_is_deterministic(self) -> None:
        self._put_item("project:alpha", "b", "two")
        self._put_item("global", "a", "one")
        out_one = _snapshot_path(self.snapshot_dir, "one.json")
        out_two = _snapshot_path(self.snapshot_dir, "two.json")

        export_one = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_one),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export_one.returncode, 0, export_one.stderr)

        export_two = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_two),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export_two.returncode, 0, export_two.stderr)

        snapshot_one = json.loads(out_one.read_text(encoding="utf-8"))
        snapshot_two = json.loads(out_two.read_text(encoding="utf-8"))
        self.assertEqual(snapshot_one["items"], snapshot_two["items"])
        self.assertEqual(snapshot_one["snapshot_hash"], snapshot_two["snapshot_hash"])

        items = snapshot_one["items"]
        ordered_keys = [(item["namespace"], item["key"]) for item in items]
        self.assertEqual(sorted(ordered_keys), ordered_keys)
        for item in items:
            canonical_value = canonical_value_json(json.loads(item["value_json"]))
            item["value_json"] = canonical_value
            item["tags"] = sorted(item["tags"])
            self.assertEqual(_item_hash(item), item["item_hash"])
        computed_snapshot_hash = hashlib.sha256(
            "".join(item["item_hash"] for item in items).encode("utf-8")
        ).hexdigest()
        self.assertEqual(snapshot_one["snapshot_hash"], computed_snapshot_hash)

    def test_snapshot_import_rejects_tampered_payload(self) -> None:
        self._put_item("global", "alpha", "one")
        out_path = _snapshot_path(self.snapshot_dir, "tamper.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
        snapshot["items"][0]["value_json"] = json.dumps("tampered")
        out_path.write_text(json.dumps(snapshot), encoding="utf-8")

        fresh_db = Path(self.temp_dir.name) / "fresh.db"
        import_result = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(fresh_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(import_result.returncode, 0)
        self.assertIn("Invalid snapshot", import_result.stderr)

    def test_snapshot_import_modes(self) -> None:
        self._put_item("global", "alpha", "snapshot")
        out_path = _snapshot_path(self.snapshot_dir, "modes.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
        snapshot_item = snapshot["items"][0]

        merge_db = Path(self.temp_dir.name) / "merge.db"
        self.db_path = merge_db
        self._put_item("global", "alpha", "local")
        local_item = json.loads(
            _run_cli(
                [
                    "memory",
                    "get",
                    "--db",
                    str(merge_db),
                    "--policy",
                    str(self.policy_path),
                    "--namespace",
                    "global",
                    "--json",
                    "alpha",
                ],
                cwd=self.repo_root,
            ).stdout
        )
        merge_import = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(merge_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--mode",
                "merge",
                "--yes",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(merge_import.returncode, 0, merge_import.stderr)
        merged_item = json.loads(
            _run_cli(
                [
                    "memory",
                    "get",
                    "--db",
                    str(merge_db),
                    "--policy",
                    str(self.policy_path),
                    "--namespace",
                    "global",
                    "--json",
                    "alpha",
                ],
                cwd=self.repo_root,
            ).stdout
        )
        self.assertEqual(merged_item["value"], "snapshot")
        self.assertEqual(merged_item["created_at"], local_item["created_at"])

        overwrite_db = Path(self.temp_dir.name) / "overwrite.db"
        self.db_path = overwrite_db
        self._put_item("global", "alpha", "local")
        overwrite_import = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(overwrite_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--mode",
                "overwrite",
                "--yes",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(overwrite_import.returncode, 0, overwrite_import.stderr)
        overwritten_item = json.loads(
            _run_cli(
                [
                    "memory",
                    "get",
                    "--db",
                    str(overwrite_db),
                    "--policy",
                    str(self.policy_path),
                    "--namespace",
                    "global",
                    "--json",
                    "alpha",
                ],
                cwd=self.repo_root,
            ).stdout
        )
        self.assertEqual(overwritten_item["value"], "snapshot")
        self.assertEqual(overwritten_item["created_at"], snapshot_item["created_at"])

        skip_db = Path(self.temp_dir.name) / "skip.db"
        self.db_path = skip_db
        self._put_item("global", "alpha", "local")
        skip_import = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(skip_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--mode",
                "skip-existing",
                "--yes",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(skip_import.returncode, 0, skip_import.stderr)
        skipped_item = json.loads(
            _run_cli(
                [
                    "memory",
                    "get",
                    "--db",
                    str(skip_db),
                    "--policy",
                    str(self.policy_path),
                    "--namespace",
                    "global",
                    "--json",
                    "alpha",
                ],
                cwd=self.repo_root,
            ).stdout
        )
        self.assertEqual(skipped_item["value"], "local")

    def test_snapshot_import_requires_confirmation(self) -> None:
        self._put_item("global", "alpha", "snapshot")
        out_path = _snapshot_path(self.snapshot_dir, "confirm.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        fresh_db = Path(self.temp_dir.name) / "confirm.db"
        import_result = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(fresh_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--mode",
                "merge",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(import_result.returncode, 0)
        with contextlib.closing(sqlite3.connect(fresh_db)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_items"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_snapshot_diff_classifies_changes(self) -> None:
        self._put_item("global", "alpha", "same")
        self._put_item("global", "bravo", "old")
        self._put_item("global", "tomb", "live")
        out_path = _snapshot_path(self.snapshot_dir, "diff.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)

        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
        items = snapshot["items"]
        alpha = next(item for item in items if item["key"] == "alpha")
        bravo = next(item for item in items if item["key"] == "bravo")
        tomb = next(item for item in items if item["key"] == "tomb")

        bravo["value_json"] = canonical_value_json("new")
        bravo["item_hash"] = _item_hash(bravo)

        tomb["is_tombstoned"] = True
        tomb["item_hash"] = _item_hash(tomb)

        added = dict(alpha)
        added["key"] = "charlie"
        added["value_json"] = canonical_value_json("fresh")
        added["item_hash"] = _item_hash(added)
        items.append(added)

        items.sort(key=lambda item: (item["namespace"], item["key"]))
        snapshot["snapshot_hash"] = hashlib.sha256(
            "".join(item["item_hash"] for item in items).encode("utf-8")
        ).hexdigest()
        out_path.write_text(json.dumps(snapshot), encoding="utf-8")

        diff_result = _run_cli(
            [
                "memory",
                "snapshot",
                "diff",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(diff_result.returncode, 0, diff_result.stderr)
        payload = json.loads(diff_result.stdout)
        self.assertEqual(payload["summary"], {
            "adds": 1,
            "updates": 1,
            "tombstones": 1,
            "unchanged": 1,
        })
        self.assertEqual({item["key"] for item in payload["adds"]}, {"charlie"})
        self.assertEqual({item["key"] for item in payload["updates"]}, {"bravo"})
        self.assertEqual({item["key"] for item in payload["tombstones"]}, {"tomb"})
        self.assertEqual({item["key"] for item in payload["unchanged"]}, {"alpha"})

    def test_snapshot_import_dry_run_records_audit_only(self) -> None:
        self._put_item("global", "alpha", "snapshot")
        out_path = _snapshot_path(self.snapshot_dir, "dry-run.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)

        dry_run_db = Path(self.temp_dir.name) / "dry-run.db"
        dry_run = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(dry_run_db),
                "--policy",
                str(self.policy_path),
                "--in",
                str(out_path),
                "--mode",
                "merge",
                "--yes",
                "--non-interactive",
                "--dry-run",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        with contextlib.closing(sqlite3.connect(dry_run_db)) as connection:
            memory_event_count = connection.execute(
                "SELECT COUNT(*) FROM memory_events"
            ).fetchone()[0]
            item_count = connection.execute(
                "SELECT COUNT(*) FROM memory_items"
            ).fetchone()[0]
            events = connection.execute(
                "SELECT json_payload FROM events"
            ).fetchall()
        self.assertEqual(memory_event_count, 0)
        self.assertEqual(item_count, 0)
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0][0])
        self.assertTrue(payload["dry_run"])

        os.remove(dry_run_db)
        self.assertFalse(dry_run_db.exists())

    def test_snapshot_import_dry_run_denies_policy(self) -> None:
        self._put_item("global", "alpha", "snapshot")
        out_path = _snapshot_path(self.snapshot_dir, "deny.json")
        export = _run_cli(
            [
                "memory",
                "snapshot",
                "export",
                "--db",
                str(self.db_path),
                "--policy",
                str(self.policy_path),
                "--namespace",
                "*",
                "--out",
                str(out_path),
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(export.returncode, 0, export.stderr)

        readonly_policy = self.repo_root / "policy" / "readonly.json"
        dry_run = _run_cli(
            [
                "memory",
                "snapshot",
                "import",
                "--db",
                str(Path(self.temp_dir.name) / "deny.db"),
                "--policy",
                str(readonly_policy),
                "--in",
                str(out_path),
                "--mode",
                "merge",
                "--yes",
                "--non-interactive",
                "--dry-run",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(dry_run.returncode, 0)
        self.assertIn("denied=1", dry_run.stdout)


if __name__ == "__main__":
    unittest.main()
