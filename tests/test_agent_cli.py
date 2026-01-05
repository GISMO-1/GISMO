import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


class AgentCliTest(unittest.TestCase):
    def _mock_env(self) -> dict[str, str]:
        return {
            "GISMO_OLLAMA_MODEL": "",
            "GISMO_OLLAMA_TIMEOUT_S": "",
            "GISMO_OLLAMA_URL": "",
            "GISMO_LLM_MODEL": "",
            "OLLAMA_HOST": "",
        }

    def _write_policy(self, tmpdir: str, policy: dict) -> str:
        path = Path(tmpdir) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return str(path)

    def test_agent_dry_run_does_not_enqueue(self) -> None:
        response = json.dumps(
            {
                "intent": "greet",
                "assumptions": [],
                "actions": [
                    {
                        "type": "enqueue",
                        "command": "echo: hello",
                        "timeout_seconds": 30,
                        "retries": 0,
                        "why": "acknowledge",
                        "risk": "low",
                    }
                ],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        cli_main.run_agent(
                            db_path,
                            "say hello",
                            policy_path=None,
                            once=True,
                            max_cycles=1,
                            yes=False,
                            dry_run=True,
                        )
            output = buffer.getvalue()
            self.assertIn("=== Agent Summary ===", output)
            self.assertIn("Final status: dry-run", output)

            state_store = StateStore(db_path)
            self.assertEqual(state_store.list_queue_items(limit=10), [])

    def test_agent_once_enqueues_and_executes(self) -> None:
        response = json.dumps(
            {
                "intent": "queue",
                "assumptions": [],
                "actions": [
                    {
                        "type": "enqueue",
                        "command": "echo: queued",
                        "timeout_seconds": 15,
                        "retries": 0,
                        "why": "record",
                        "risk": "low",
                    }
                ],
                "notes": [],
            }
        )

        def _fake_run_daemon_once(db_path: str, policy_path: str | None) -> None:
            state_store = StateStore(db_path)
            for item in state_store.list_queue_items(limit=10):
                if item.status == QueueStatus.QUEUED:
                    state_store.mark_queue_item_succeeded(item.id)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with mock.patch.object(cli_main, "_run_daemon_once", _fake_run_daemon_once):
                        buffer = io.StringIO()
                        with contextlib.redirect_stdout(buffer):
                            cli_main.run_agent(
                                db_path,
                                "enqueue a note",
                                policy_path=None,
                                once=True,
                                max_cycles=1,
                                yes=True,
                                dry_run=False,
                            )
            output = buffer.getvalue()
            self.assertIn("Final status: succeeded", output)

            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].status, QueueStatus.SUCCEEDED)

    def test_agent_requires_confirmation_for_shell(self) -> None:
        response = json.dumps(
            {
                "intent": "risky",
                "assumptions": [],
                "actions": [
                    {
                        "type": "enqueue",
                        "command": "shell: echo risky",
                        "timeout_seconds": 30,
                        "retries": 0,
                        "why": "test",
                        "risk": "high",
                    }
                ],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with mock.patch.object(cli_main, "_is_interactive_tty", return_value=False):
                        with self.assertRaises(SystemExit) as exc:
                            cli_main.run_agent(
                                db_path,
                                "do risky thing",
                                policy_path=None,
                                once=True,
                                max_cycles=1,
                                yes=False,
                                dry_run=False,
                            )
            self.assertEqual(exc.exception.code, 2)
            state_store = StateStore(db_path)
            self.assertEqual(state_store.list_queue_items(limit=10), [])

    def test_agent_memory_suggestions_are_advisory_by_default(self) -> None:
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
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        cli_main.run_agent(
                            db_path,
                            "remember defaults",
                            policy_path=None,
                            once=True,
                            max_cycles=1,
                            yes=False,
                            dry_run=True,
                        )
            output = buffer.getvalue()
            self.assertIn("Suggested memory updates (advisory only):", output)
            self.assertIn("gismo memory put", output)
            with sqlite3.connect(db_path) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            self.assertEqual(item_count, 0)

    def test_agent_apply_memory_suggestions_requires_confirmation(self) -> None:
        response = json.dumps(
            {
                "intent": "remember",
                "assumptions": [],
                "actions": [],
                "notes": [],
                "memory_suggestions": [
                    {
                        "namespace": "global",
                        "key": "operator_pref",
                        "kind": "preference",
                        "value_json": "\"fast\"",
                        "confidence": "high",
                        "why": "Operator prefers speed.",
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {
                        "allow": {"memory.put": ["global"]},
                        "require_confirmation": {"memory.put": ["global"]},
                    },
                },
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with mock.patch("builtins.input", return_value="y"), mock.patch(
                        "sys.stdin.isatty",
                        return_value=True,
                    ), mock.patch("sys.stdout.isatty", return_value=True):
                        cli_main.run_agent(
                            db_path,
                            "remember preference",
                            policy_path=policy_path,
                            once=True,
                            max_cycles=1,
                            yes=False,
                            dry_run=True,
                            apply_memory_suggestions=True,
                        )
            with sqlite3.connect(db_path) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
                event_row = connection.execute(
                    "SELECT related_ask_event_id FROM memory_events"
                ).fetchone()
            self.assertEqual(item_count, 1)
            self.assertIsNotNone(event_row)
            related_event_id = event_row[0]
            self.assertIsNotNone(related_event_id)
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            self.assertEqual(event.id, related_event_id)

    def test_agent_apply_memory_suggestions_non_interactive_fails_closed(self) -> None:
        response = json.dumps(
            {
                "intent": "remember",
                "assumptions": [],
                "actions": [],
                "notes": [],
                "memory_suggestions": [
                    {
                        "namespace": "global",
                        "key": "operator_pref",
                        "kind": "preference",
                        "value_json": "\"safe\"",
                        "confidence": "high",
                        "why": "Operator prefers safety.",
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {
                        "allow": {"memory.put": ["global"]},
                        "require_confirmation": {"memory.put": ["global"]},
                    },
                },
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with self.assertRaises(SystemExit):
                        cli_main.run_agent(
                            db_path,
                            "remember preference",
                            policy_path=policy_path,
                            once=True,
                            max_cycles=1,
                            yes=False,
                            dry_run=True,
                            apply_memory_suggestions=True,
                            non_interactive=True,
                        )
            with sqlite3.connect(db_path) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            self.assertEqual(item_count, 0)
