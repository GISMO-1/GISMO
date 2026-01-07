import contextlib
import gc
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.core.models import ToolReceipt, ToolReceiptStatus
from gismo.core.state import StateStore
from gismo.memory import policy_hash_for_path
from gismo.memory.snapshot import export_snapshot
from gismo.memory.store import (
    create_profile as memory_create_profile,
    put_item as memory_put_item,
    set_retention_rule as memory_set_retention_rule,
)


class DbHandleGuardrailsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def _run_cli(self, args: list[str]) -> None:
        with mock.patch.object(sys, "argv", ["gismo", *args]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli_main.main()

    def _run_subprocess_cli(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, "-m", "gismo.cli.main", *args]
        return subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )

    def _run_ask(self, db_path: Path, args: list[str], response: str) -> None:
        env = {
            "GISMO_OLLAMA_MODEL": "",
            "GISMO_OLLAMA_TIMEOUT_S": "",
            "GISMO_OLLAMA_URL": "",
            "GISMO_LLM_MODEL": "",
            "OLLAMA_HOST": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                self._run_cli(["--db", str(db_path), "ask", *args])

    def _write_policy(self, tmpdir: str, policy: dict[str, object]) -> Path:
        path = Path(tmpdir) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def _assert_db_deletable(self, db_path: Path) -> None:
        self.assertTrue(db_path.exists())
        try:
            os.remove(db_path)
        except OSError as exc:
            self.fail(f"Expected DB path to be deletable, got error: {exc}")
        self.assertFalse(db_path.exists())

    def test_ask_dry_run_releases_db_handle(self) -> None:
        response = json.dumps(
            {
                "intent": "greet",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_ask(db_path, ["--dry-run", "say", "hello"], response)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_ask_memory_dry_run_releases_db_handle(self) -> None:
        response = json.dumps(
            {
                "intent": "recall",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_ask(
                    db_path,
                    ["--memory", "--dry-run", "remember", "context"],
                    response,
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_ask_memory_profile_dry_run_releases_db_handle(self) -> None:
        response = json.dumps(
            {
                "intent": "recall",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            profile = memory_create_profile(
                str(db_path),
                name="profile",
                description=None,
                include_namespaces=["global"],
                exclude_namespaces=None,
                include_kinds=["fact"],
                exclude_kinds=None,
                max_items=None,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_ask(
                    db_path,
                    ["--memory-profile", profile.name, "--dry-run", "remember", "context"],
                    response,
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only handle release check")
    def test_windows_snapshot_cli_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_put_item(
                str(db_path),
                namespace="global",
                key="snapshot",
                kind="fact",
                value="ok",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            snapshot_path = Path(tmpdir) / "snapshot.json"
            self._run_cli(
                [
                    "memory",
                    "snapshot",
                    "export",
                    "--db",
                    str(db_path),
                    "--policy",
                    str(self.policy_path),
                    "--namespace",
                    "*",
                    "--out",
                    str(snapshot_path),
                ]
            )
            self._run_cli(
                [
                    "memory",
                    "snapshot",
                    "diff",
                    "--db",
                    str(db_path),
                    "--policy",
                    str(self.policy_path),
                    "--in",
                    str(snapshot_path),
                    "--json",
                ]
            )
            gc.collect()
            self._assert_db_deletable(db_path)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only handle release check")
    def test_windows_memory_explain_subprocess_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with StateStore(str(db_path)) as state_store:
                run = state_store.create_run(label="guardrail", metadata={})
            result = self._run_subprocess_cli(
                [
                    "--db",
                    str(db_path),
                    "memory",
                    "explain",
                    "--run",
                    run.id,
                    "--json",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            gc.collect()
            self._assert_db_deletable(db_path)

    def test_memory_put_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "memory",
                        "put",
                        "--db",
                        str(db_path),
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
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_get_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_put_item(
                str(db_path),
                namespace="global",
                key="default_model",
                kind="preference",
                value="phi3:mini",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "memory",
                        "get",
                        "--db",
                        str(db_path),
                        "--policy",
                        str(self.policy_path),
                        "--namespace",
                        "global",
                        "--json",
                        "default_model",
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_agent_session_list_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "agent",
                        "session",
                        "list",
                        "--db",
                        str(db_path),
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_ask_apply_memory_suggestions_releases_db_handle(self) -> None:
        response = json.dumps(
            {
                "intent": "remember",
                "assumptions": [],
                "actions": [],
                "notes": [],
                "memory_suggestions": [
                    {
                        "namespace": "global",
                        "key": "default_model",
                        "kind": "preference",
                        "value_json": "\"phi3:mini\"",
                        "confidence": "high",
                        "why": "Operator prefers the default model.",
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {"allow": {"memory.put": ["global"]}},
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_ask(
                    db_path,
                    [
                        "--apply-memory-suggestions",
                        "--yes",
                        "--dry-run",
                        "--policy",
                        str(policy_path),
                        "remember",
                        "model",
                    ],
                    response,
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_doctor_check_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_put_item(
                str(db_path),
                namespace="global",
                key="doctor_check",
                kind="fact",
                value="ok",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                try:
                    self._run_cli(
                        [
                            "memory",
                            "doctor",
                            "check",
                            "--db",
                            str(db_path),
                            "--policy",
                            str(self.policy_path),
                            "--json",
                        ]
                    )
                except SystemExit as exc:
                    self.assertEqual(exc.code, 0)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_doctor_repair_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_put_item(
                str(db_path),
                namespace="global",
                key="doctor_repair",
                kind="fact",
                value="ok",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": [],
                    "memory": {"allow": {"memory.doctor.reindex": ["global"]}},
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "memory",
                        "doctor",
                        "repair",
                        "--db",
                        str(db_path),
                        "--policy",
                        str(policy_path),
                        "--dry-run",
                        "--reindex",
                        "--yes",
                        "--non-interactive",
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_retention_set_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": [],
                    "memory": {"allow": {"memory.retention.set": ["global"]}},
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                try:
                    self._run_cli(
                        [
                            "memory",
                            "retention",
                            "set",
                            "--db",
                            str(db_path),
                            "--policy",
                            str(policy_path),
                            "global",
                            "--max-items",
                            "1",
                            "--reason",
                            "test",
                            "--yes",
                            "--non-interactive",
                        ]
                    )
                except SystemExit as exc:
                    self.assertEqual(exc.code, 2)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_retention_clear_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            memory_set_retention_rule(
                str(db_path),
                namespace="global",
                max_items=1,
                ttl_seconds=None,
                policy_source="operator",
            )
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": [],
                    "memory": {"allow": {"memory.retention.clear": ["global"]}},
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                try:
                    self._run_cli(
                        [
                            "memory",
                            "retention",
                            "clear",
                            "--db",
                            str(db_path),
                            "--policy",
                            str(policy_path),
                            "global",
                            "--yes",
                            "--non-interactive",
                        ]
                    )
                except SystemExit as exc:
                    self.assertEqual(exc.code, 2)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_retention_enforce_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_set_retention_rule(
                str(db_path),
                namespace="global",
                max_items=1,
                ttl_seconds=None,
                policy_source="operator",
            )
            memory_put_item(
                str(db_path),
                namespace="global",
                key="retention_base",
                kind="fact",
                value="base",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {
                        "allow": {
                            "memory.put": ["global"],
                            "memory.retention.enforce": ["global"],
                        }
                    },
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                try:
                    self._run_cli(
                        [
                            "memory",
                            "put",
                            "--db",
                            str(db_path),
                            "--policy",
                            str(policy_path),
                            "--namespace",
                            "global",
                            "--key",
                            "retention_new",
                            "--kind",
                            "fact",
                            "--value-text",
                            "value",
                            "--confidence",
                            "high",
                            "--source",
                            "operator",
                            "--yes",
                        ]
                    )
                except SystemExit as exc:
                    self.assertEqual(exc.code, 2)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_memory_snapshot_import_dry_run_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            policy_hash = policy_hash_for_path(str(self.policy_path))
            memory_put_item(
                str(db_path),
                namespace="global",
                key="snapshot",
                kind="fact",
                value="ok",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash=policy_hash,
            )
            snapshot_payload = export_snapshot(str(db_path), namespace_filter="*")
            snapshot_path = Path(tmpdir) / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": [],
                    "memory": {"allow": {"memory.put": ["global"], "memory.delete": ["global"]}},
                },
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                try:
                    self._run_cli(
                        [
                            "memory",
                            "snapshot",
                            "import",
                            "--db",
                            str(db_path),
                            "--in",
                            str(snapshot_path),
                            "--dry-run",
                            "--mode",
                            "merge",
                            "--yes",
                            "--non-interactive",
                            "--policy",
                            str(policy_path),
                        ]
                    )
                except SystemExit as exc:
                    self.assertEqual(exc.code, 2)
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_tool_receipts_list_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with StateStore(str(db_path)) as state_store:
                run = state_store.create_run(label="tool-receipts", metadata={})
                started_at = datetime.now(timezone.utc)
                receipt = ToolReceipt(
                    run_id=run.id,
                    tool_name="echo",
                    tool_kind="system",
                    request_payload_json="{}",
                    response_payload_json="{}",
                    status=ToolReceiptStatus.SUCCESS,
                    started_at=started_at,
                    finished_at=started_at,
                    duration_ms=0,
                    request_sha256="req",
                    response_sha256="resp",
                )
                state_store.record_tool_receipt(receipt)
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "tools",
                        "receipts",
                        "list",
                        "--db",
                        str(db_path),
                        "--run",
                        run.id,
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_tool_receipts_show_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with StateStore(str(db_path)) as state_store:
                run = state_store.create_run(label="tool-receipts", metadata={})
                started_at = datetime.now(timezone.utc)
                receipt = ToolReceipt(
                    run_id=run.id,
                    tool_name="echo",
                    tool_kind="system",
                    request_payload_json="{}",
                    response_payload_json="{}",
                    status=ToolReceiptStatus.SUCCESS,
                    started_at=started_at,
                    finished_at=started_at,
                    duration_ms=0,
                    request_sha256="req",
                    response_sha256="resp",
                )
                state_store.record_tool_receipt(receipt)
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "tools",
                        "receipts",
                        "show",
                        "--db",
                        str(db_path),
                        receipt.id,
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)

    def test_agent_session_show_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with StateStore(str(db_path)) as state_store:
                session = state_store.create_agent_session(
                    goal="test",
                    role_id=None,
                    role_name=None,
                    profile_id=None,
                    profile_name=None,
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                self._run_cli(
                    [
                        "agent",
                        "session",
                        "show",
                        "--db",
                        str(db_path),
                        session.session_id,
                    ]
                )
                gc.collect()
                self._assert_db_deletable(db_path)
