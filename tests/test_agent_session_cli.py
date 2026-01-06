import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import agent_session as agent_session_cli
from gismo.cli import main as cli_main
from gismo.core.models import AgentSessionStatus
from gismo.core.state import StateStore


class AgentSessionCliTest(unittest.TestCase):
    def _mock_env(self) -> dict[str, str]:
        return {
            "GISMO_OLLAMA_MODEL": "",
            "GISMO_OLLAMA_TIMEOUT_S": "",
            "GISMO_OLLAMA_URL": "",
            "GISMO_LLM_MODEL": "",
            "OLLAMA_HOST": "",
        }

    def _start_session(self, db_path: str, goal: str) -> str:
        args = argparse.Namespace(
            db_path=db_path,
            goal=goal,
            role=None,
            max_steps=3,
            json=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent_session_cli.run_agent_session_start(args)
        payload = json.loads(buffer.getvalue())
        return payload["session"]["session_id"]

    def test_agent_session_start_list_show_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            session_id = self._start_session(db_path, "plan a report")

            list_args = argparse.Namespace(db_path=db_path, json=True)
            list_buffer = io.StringIO()
            with contextlib.redirect_stdout(list_buffer):
                agent_session_cli.run_agent_session_list(list_args)
            list_payload = json.loads(list_buffer.getvalue())
            self.assertEqual(list_payload["schema_version"], 1)
            self.assertEqual(list_payload["sessions"][0]["session_id"], session_id)

            show_args = argparse.Namespace(
                db_path=db_path,
                session_id=session_id,
                json=True,
            )
            show_buffer = io.StringIO()
            with contextlib.redirect_stdout(show_buffer):
                agent_session_cli.run_agent_session_show(show_args)
            show_payload = json.loads(show_buffer.getvalue())
            self.assertEqual(show_payload["session"]["session_id"], session_id)

    def test_session_resume_non_interactive_fails_closed(self) -> None:
        response = json.dumps(
            {
                "intent": "risky",
                "assumptions": [],
                "actions": [
                    {
                        "type": "enqueue",
                        "command": "shell: echo risky",
                        "timeout_seconds": 15,
                        "retries": 0,
                        "why": "record",
                        "risk": "high",
                    }
                ],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            session_id = self._start_session(db_path, "enqueue a note")
            args = argparse.Namespace(
                db_path=db_path,
                session_id=session_id,
                policy=None,
                yes=False,
                non_interactive=True,
                dry_run=False,
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with self.assertRaises(SystemExit) as exc:
                        agent_session_cli.run_agent_session_resume(
                            args,
                            cli_main._agent_session_dependencies(),
                        )
            self.assertEqual(exc.exception.code, 2)
            state_store = StateStore(db_path)
            try:
                self.assertEqual(state_store.list_queue_items(limit=10), [])
                session = state_store.get_agent_session(session_id)
                assert session is not None
                self.assertEqual(session.status, AgentSessionStatus.PAUSED)
                self.assertEqual(session.step_count, 1)
                self.assertIsNotNone(session.last_plan_event_id)
            finally:
                state_store.close()

    def test_session_resume_confirmation_declined(self) -> None:
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
            session_id = self._start_session(db_path, "do risky thing")
            args = argparse.Namespace(
                db_path=db_path,
                session_id=session_id,
                policy=None,
                yes=False,
                non_interactive=False,
                dry_run=False,
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    with mock.patch.object(cli_main, "_is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value="n"):
                            with self.assertRaises(SystemExit) as exc:
                                agent_session_cli.run_agent_session_resume(
                                    args,
                                    cli_main._agent_session_dependencies(),
                                )
            self.assertEqual(exc.exception.code, 2)
            state_store = StateStore(db_path)
            try:
                self.assertEqual(state_store.list_queue_items(limit=10), [])
                session = state_store.get_agent_session(session_id)
                assert session is not None
                self.assertEqual(session.status, AgentSessionStatus.PAUSED)
            finally:
                state_store.close()

    def test_session_resume_completes_without_actions(self) -> None:
        response = json.dumps(
            {
                "intent": "done",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            session_id = self._start_session(db_path, "no actions")
            args = argparse.Namespace(
                db_path=db_path,
                session_id=session_id,
                policy=None,
                yes=True,
                non_interactive=False,
                dry_run=False,
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        agent_session_cli.run_agent_session_resume(
                            args,
                            cli_main._agent_session_dependencies(),
                        )
            state_store = StateStore(db_path)
            try:
                session = state_store.get_agent_session(session_id)
                assert session is not None
                self.assertEqual(session.status, AgentSessionStatus.COMPLETED)
                self.assertEqual(session.step_count, 1)
                self.assertEqual(state_store.list_queue_items(limit=10), [])
            finally:
                state_store.close()


if __name__ == "__main__":
    unittest.main()
