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
from gismo.memory.store import (
    create_profile as memory_create_profile,
    policy_hash_for_path as memory_policy_hash_for_path,
    retire_namespace as memory_retire_namespace,
    upsert_item_with_timestamps as memory_upsert_item_with_timestamps,
)


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

    def test_agent_uses_memory_profile_filters_and_records_audit(self) -> None:
        response = json.dumps(
            {
                "intent": "recall",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy_hash = memory_policy_hash_for_path(None)
            memory_upsert_item_with_timestamps(
                db_path,
                namespace="global",
                key="alpha",
                kind="fact",
                value="alpha",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                is_tombstoned=False,
                created_at="2024-01-01T00:00:00+00:00",
                updated_at="2024-01-03T00:00:00+00:00",
                update_created_at=True,
                actor="test",
                policy_hash=policy_hash,
                operation="put",
            )
            memory_upsert_item_with_timestamps(
                db_path,
                namespace="global",
                key="beta",
                kind="note",
                value="beta",
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                is_tombstoned=False,
                created_at="2024-01-01T00:00:00+00:00",
                updated_at="2024-01-02T00:00:00+00:00",
                update_created_at=True,
                actor="test",
                policy_hash=policy_hash,
                operation="put",
            )
            profile = memory_create_profile(
                db_path,
                name="agent-profile",
                description=None,
                include_namespaces=["global"],
                exclude_namespaces=None,
                include_kinds=["fact"],
                exclude_kinds=None,
                max_items=1,
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    cli_main.run_agent(
                        db_path,
                        "recall context",
                        policy_path=None,
                        once=True,
                        max_cycles=1,
                        yes=False,
                        dry_run=True,
                        memory_profile=profile.name,
                    )
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            payload = event.json_payload
            assert payload is not None
            injected_keys = payload.get("memory_injected_keys")
            self.assertEqual(injected_keys, [{"namespace": "global", "key": "alpha"}])
            profile_payload = payload.get("memory_profile") or {}
            self.assertEqual(profile_payload.get("profile_id"), profile.profile_id)
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT request_json FROM memory_events "
                    "WHERE operation = ? "
                    "ORDER BY timestamp DESC "
                    "LIMIT 1",
                    ("memory.profile.use",),
                ).fetchone()
            self.assertIsNotNone(row)
            request = json.loads(row[0])
            self.assertEqual(request.get("profile_id"), profile.profile_id)

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

    def test_agent_apply_memory_suggestions_denied_for_retired_namespace(self) -> None:
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
            memory_retire_namespace(
                db_path,
                namespace="global",
                reason="policy-freeze",
            )
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {"allow": {"memory.put": ["global"]}},
                },
            )
            with mock.patch.dict(os.environ, self._mock_env(), clear=False):
                with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                    cli_main.run_agent(
                        db_path,
                        "remember preference",
                        policy_path=policy_path,
                        once=True,
                        max_cycles=1,
                        yes=True,
                        dry_run=True,
                        apply_memory_suggestions=True,
                    )
            with sqlite3.connect(db_path) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
                event_row = connection.execute(
                    "SELECT result_meta_json FROM memory_events "
                    "WHERE operation = ? "
                    "ORDER BY timestamp DESC "
                    "LIMIT 1",
                    ("put",),
                ).fetchone()
            self.assertEqual(item_count, 0)
            self.assertIsNotNone(event_row)
            result_meta = json.loads(event_row[0])
            self.assertEqual(result_meta["policy_action"], "memory.put.retired")
            self.assertEqual(result_meta["policy_decision"], "denied")
            self.assertTrue(result_meta["namespace_retired"])
