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
                            yes=False,
                            explain=False,
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
                        "command": "echo: queued",
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
                            yes=False,
                            explain=False,
                        )
            output = buffer.getvalue()
            self.assertIn("Enqueued items:", output)

            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].command_text, "echo: queued")
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
                                yes=False,
                                explain=False,
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
                        yes=False,
                        explain=False,
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
                        yes=False,
                        explain=False,
                    )
                    _, kwargs = ollama_mock.call_args
                    self.assertEqual(kwargs["timeout_s"], 5)

    def test_ask_timeout_override_printed_in_llm_line(self) -> None:
        response = json.dumps(
            {"intent": "ping", "assumptions": [], "actions": [], "notes": []}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            with mock.patch.dict(os.environ, {"GISMO_OLLAMA_TIMEOUT_S": "120"}, clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        cli_main.run_ask(
                            db_path,
                            "ping",
                            model=None,
                            host=None,
                            timeout_s=2,
                            enqueue=False,
                            dry_run=True,
                            max_actions=5,
                            yes=False,
                            explain=False,
                        )
            output = buffer.getvalue()
            self.assertIn("timeout=2s", output)

    def test_ask_coerces_echo_action_type_to_enqueue(self) -> None:
        response = json.dumps(
            {
                "intent": "queue echo",
                "assumptions": [],
                "actions": [
                    {
                        "type": "echo: hello from GISMO",
                        "command": "",
                        "timeout_seconds": 10,
                        "retries": 2,
                        "why": "respond",
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
                    cli_main.run_ask(
                        db_path,
                        "enqueue an echo",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
                    )
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            payload = event.json_payload
            assert payload is not None
            plan = payload["plan"]
            actions = plan["actions"]
            self.assertEqual(actions[0]["type"], "enqueue")
            self.assertEqual(actions[0]["command"], "echo: hello from GISMO")
            notes = plan["notes"]
            self.assertFalse(any("Ignored unsupported action types" in note for note in notes))

    def test_ask_unsupported_action_still_reported(self) -> None:
        response = json.dumps(
            {
                "intent": "cleanup",
                "assumptions": [],
                "actions": [
                    {
                        "type": "delete_files",
                        "command": "rm -rf /",
                        "timeout_seconds": 30,
                        "retries": 0,
                        "why": "cleanup",
                        "risk": "high",
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
                    cli_main.run_ask(
                        db_path,
                        "delete files",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
                    )
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            payload = event.json_payload
            assert payload is not None
            plan = payload["plan"]
            self.assertIn(
                "Ignored unsupported action types: delete_files.",
                plan["notes"],
            )

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
                    side_effect=cli_main.OllamaError(
                        "Ollama request failed (timeout/connection) after 120s. "
                        "Verify `ollama ps` and that http://127.0.0.1:11434 is "
                        "reachable. Consider a smaller model or increase --timeout-s."
                    ),
                ):
                    stdout_buffer = io.StringIO()
                    stderr_buffer = io.StringIO()
                    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(
                        stderr_buffer
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            cli_main.run_ask(
                                db_path,
                                "ping",
                                model=None,
                                host=None,
                                timeout_s=None,
                                enqueue=False,
                                dry_run=True,
                                max_actions=5,
                                yes=False,
                                explain=False,
                            )
                    self.assertNotEqual(exc.exception.code, 0)
                    stderr_output = stderr_buffer.getvalue()
                    self.assertIn("ERROR: Ollama request failed", stderr_output)
                    self.assertNotIn("Traceback", stderr_output)
                    self.assertNotIn("Traceback", stdout_buffer.getvalue())
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

    def test_ask_enqueue_requires_confirmation_interactive_decline(self) -> None:
        actions = [
            {
                "type": "enqueue",
                "command": f"note: step {index}",
                "timeout_seconds": 30,
                "retries": 0,
                "why": "test",
                "risk": "low",
            }
            for index in range(13)
        ]
        response = json.dumps(
            {"intent": "queue notes", "assumptions": [], "actions": actions, "notes": []}
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
                    with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
                        "sys.stdout.isatty", return_value=True
                    ), mock.patch("builtins.input", return_value="n"):
                        with self.assertRaises(SystemExit) as exc:
                            cli_main.run_ask(
                                db_path,
                                "enqueue notes",
                                model=None,
                                host=None,
                                timeout_s=None,
                                enqueue=True,
                                dry_run=False,
                                max_actions=20,
                                yes=False,
                                explain=False,
                            )
                        self.assertEqual(exc.exception.code, 2)
            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=20)
            self.assertEqual(len(items), 0)

    def test_ask_enqueue_requires_confirmation_interactive_accept(self) -> None:
        actions = [
            {
                "type": "enqueue",
                "command": f"note: step {index}",
                "timeout_seconds": 30,
                "retries": 0,
                "why": "test",
                "risk": "low",
            }
            for index in range(13)
        ]
        response = json.dumps(
            {"intent": "queue notes", "assumptions": [], "actions": actions, "notes": []}
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
                    with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
                        "sys.stdout.isatty", return_value=True
                    ), mock.patch("builtins.input", return_value="y"):
                        cli_main.run_ask(
                            db_path,
                            "enqueue notes",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=True,
                            dry_run=False,
                            max_actions=20,
                            yes=False,
                            explain=False,
                        )
            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=20)
            self.assertEqual(len(items), 13)

    def test_ask_enqueue_requires_confirmation_non_interactive(self) -> None:
        actions = [
            {
                "type": "enqueue",
                "command": f"note: step {index}",
                "timeout_seconds": 30,
                "retries": 0,
                "why": "test",
                "risk": "low",
            }
            for index in range(13)
        ]
        response = json.dumps(
            {"intent": "queue notes", "assumptions": [], "actions": actions, "notes": []}
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
                    with contextlib.redirect_stderr(buffer):
                        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch(
                            "sys.stdout.isatty", return_value=False
                        ):
                            with self.assertRaises(SystemExit) as exc:
                                cli_main.run_ask(
                                    db_path,
                                    "enqueue notes",
                                    model=None,
                                    host=None,
                                    timeout_s=None,
                                    enqueue=True,
                                    dry_run=False,
                                    max_actions=20,
                                    yes=False,
                                    explain=False,
                                )
                            self.assertEqual(exc.exception.code, 2)
                    self.assertIn("--yes", buffer.getvalue())
            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=20)
            self.assertEqual(len(items), 0)

    def test_ask_enqueue_requires_confirmation_yes_override(self) -> None:
        actions = [
            {
                "type": "enqueue",
                "command": f"note: step {index}",
                "timeout_seconds": 30,
                "retries": 0,
                "why": "test",
                "risk": "low",
            }
            for index in range(13)
        ]
        response = json.dumps(
            {"intent": "queue notes", "assumptions": [], "actions": actions, "notes": []}
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
                    with mock.patch("sys.stdin.isatty", return_value=False), mock.patch(
                        "sys.stdout.isatty", return_value=False
                    ):
                        cli_main.run_ask(
                            db_path,
                            "enqueue notes",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=True,
                            dry_run=False,
                            max_actions=20,
                            yes=True,
                            explain=False,
                        )
            state_store = StateStore(db_path)
            items = state_store.list_queue_items(limit=20)
            self.assertEqual(len(items), 13)


if __name__ == "__main__":
    unittest.main()
