import contextlib
import io
import json
import os
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
