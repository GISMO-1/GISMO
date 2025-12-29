import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.core.models import EVENT_TYPE_ASK_FAILED, EVENT_TYPE_LLM_PLAN
from gismo.core.state import StateStore


class AskCliTest(unittest.TestCase):
    def test_ask_dry_run_writes_event_and_prints_plan(self) -> None:
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
            with mock.patch.dict(
                os.environ,
                {
                    "GISMO_OLLAMA_MODEL": "",
                    "GISMO_OLLAMA_TIMEOUT_S": "",
                    "GISMO_OLLAMA_URL": "",
                    "GISMO_LLM_MODEL": "",
                    "OLLAMA_HOST": "",
                },
                clear=False,
            ):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        cli_main.run_ask(
                            db_path,
                            "say hello",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                        )
            output = buffer.getvalue()
            self.assertIn("LLM: phi3:mini url=http://127.0.0.1:11434 timeout=120s", output)
            self.assertIn("=== GISMO LLM Plan ===", output)
            self.assertIn("Intent: greet", output)

            state_store = StateStore(db_path)
            events = state_store.list_events()
            self.assertTrue(events)
            event = events[0]
            self.assertEqual(event.event_type, EVENT_TYPE_LLM_PLAN)
            payload = event.json_payload
            assert payload is not None
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["enqueue"])

    def test_ask_enqueue_enqueues_items_and_writes_event(self) -> None:
        response = json.dumps(
            {
                "intent": "queue",
                "assumptions": [],
                "actions": [
                    {
                        "type": "enqueue",
                        "command": "note: queued",
                        "timeout_seconds": 15,
                        "retries": 1,
                        "why": "record",
                        "risk": "low",
                    }
                ],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(
                os.environ,
                {
                    "GISMO_OLLAMA_MODEL": "",
                    "GISMO_OLLAMA_TIMEOUT_S": "",
                    "GISMO_OLLAMA_URL": "",
                    "GISMO_LLM_MODEL": "",
                    "OLLAMA_HOST": "",
                },
                clear=False,
            ):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        cli_main.run_ask(
                            db_path,
                            "enqueue a note",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=True,
                            dry_run=False,
                            max_actions=10,
                        )
            output = buffer.getvalue()
            self.assertIn("Enqueued items:", output)

            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].command_text, "note: queued")
            self.assertEqual(items[0].max_retries, 1)
            self.assertEqual(items[0].timeout_seconds, 15)

            events = state_store.list_events()
            self.assertTrue(events)
            self.assertEqual(events[0].event_type, EVENT_TYPE_LLM_PLAN)

    def test_ask_invalid_json_fails_cleanly(self) -> None:
        response = "not json"
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(
                os.environ,
                {
                    "GISMO_OLLAMA_MODEL": "",
                    "GISMO_OLLAMA_TIMEOUT_S": "",
                    "GISMO_OLLAMA_URL": "",
                    "GISMO_LLM_MODEL": "",
                    "OLLAMA_HOST": "",
                },
                clear=False,
            ):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stderr(buffer):
                        with self.assertRaises(ValueError):
                            cli_main.run_ask(
                                db_path,
                                "bad",
                                model=None,
                                host=None,
                                timeout_s=None,
                                enqueue=False,
                                dry_run=True,
                                max_actions=10,
                            )
            self.assertIn("not json", buffer.getvalue())

            state_store = StateStore(db_path)
            events = state_store.list_events()
            self.assertTrue(events)
            self.assertEqual(events[0].event_type, EVENT_TYPE_LLM_PLAN)

    def test_ask_env_defaults_used_for_model_and_timeout(self) -> None:
        response = json.dumps(
            {"intent": "ping", "assumptions": [], "actions": [], "notes": []}
        )
        env = {
            "GISMO_OLLAMA_MODEL": "custom-model",
            "GISMO_OLLAMA_TIMEOUT_S": "42",
            "GISMO_OLLAMA_URL": "http://127.0.0.1:11434",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(
                    cli_main, "ollama_chat", return_value=response
                ) as ollama_mock:
                    cli_main.run_ask(
                        db_path,
                        "ping",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=5,
                    )
                    _, kwargs = ollama_mock.call_args
                    self.assertEqual(kwargs["model"], "custom-model")
                    self.assertEqual(kwargs["timeout_s"], 42)

    def test_ask_timeout_override_beats_env(self) -> None:
        response = json.dumps(
            {"intent": "ping", "assumptions": [], "actions": [], "notes": []}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, {"GISMO_OLLAMA_TIMEOUT_S": "120"}, clear=False):
                with mock.patch.object(
                    cli_main, "ollama_chat", return_value=response
                ) as ollama_mock:
                    cli_main.run_ask(
                        db_path,
                        "ping",
                        model=None,
                        host=None,
                        timeout_s=5,
                        enqueue=False,
                        dry_run=True,
                        max_actions=5,
                    )
                    _, kwargs = ollama_mock.call_args
                    self.assertEqual(kwargs["timeout_s"], 5)

    def test_ask_failure_writes_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(
                os.environ,
                {
                    "GISMO_OLLAMA_MODEL": "",
                    "GISMO_OLLAMA_TIMEOUT_S": "",
                    "GISMO_OLLAMA_URL": "",
                    "GISMO_LLM_MODEL": "",
                    "OLLAMA_HOST": "",
                },
                clear=False,
            ):
                with mock.patch.object(
                    cli_main,
                    "ollama_chat",
                    side_effect=RuntimeError("Ollama request failed (timeout/connection)."),
                ):
                    with self.assertRaises(RuntimeError):
                        cli_main.run_ask(
                            db_path,
                            "ping",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=5,
                        )
            state_store = StateStore(db_path)
            events = state_store.list_events()
            self.assertTrue(events)
            self.assertEqual(events[0].event_type, EVENT_TYPE_ASK_FAILED)

    def test_normalize_plan_drops_ungrounded_assumptions(self) -> None:
        plan = {
            "intent": "enqueue echo",
            "assumptions": ["GISMO can generate audio messages"],
            "actions": [
                {
                    "type": "enqueue",
                    "command": "echo: hello",
                    "timeout_seconds": 30,
                    "retries": 0,
                    "why": "Operator requested echo",
                    "risk": "low",
                }
            ],
            "notes": [],
        }
        normalized = cli_main._normalize_llm_plan(plan, max_actions=5)
        self.assertEqual(normalized["assumptions"], [])
        self.assertEqual(normalized["actions"][0]["command"], "echo: hello")


if __name__ == "__main__":
    unittest.main()
