import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.core.models import EVENT_TYPE_LLM_PLAN
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
            with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    cli_main.run_ask(
                        db_path,
                        "say hello",
                        model=None,
                        host=None,
                        timeout_s=1,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                    )
            output = buffer.getvalue()
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
            with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    cli_main.run_ask(
                        db_path,
                        "enqueue a note",
                        model=None,
                        host=None,
                        timeout_s=1,
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
            with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                buffer = io.StringIO()
                with contextlib.redirect_stderr(buffer):
                    with self.assertRaises(ValueError):
                        cli_main.run_ask(
                            db_path,
                            "bad",
                            model=None,
                            host=None,
                            timeout_s=1,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                        )
            self.assertIn("not json", buffer.getvalue())

            state_store = StateStore(db_path)
            events = state_store.list_events()
            self.assertTrue(events)
            self.assertEqual(events[0].event_type, EVENT_TYPE_LLM_PLAN)


if __name__ == "__main__":
    unittest.main()
