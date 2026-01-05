import contextlib
import gc
import io
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.memory import policy_hash_for_path
from gismo.memory.store import put_item as memory_put_item


class DbHandleGuardrailsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def _run_cli(self, args: list[str]) -> None:
        with mock.patch.object(sys, "argv", ["gismo", *args]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli_main.main()

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
