import contextlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.core.models import EVENT_TYPE_ASK_FAILED, EVENT_TYPE_LLM_PLAN
from gismo.core.state import StateStore
from gismo.memory.store import (
    create_profile as memory_create_profile,
    policy_hash_for_path as memory_policy_hash_for_path,
    retire_namespace as memory_retire_namespace,
    retire_profile as memory_retire_profile,
    tombstone_item as memory_tombstone_item,
    upsert_item_with_timestamps as memory_upsert_item_with_timestamps,
    put_item as memory_put_item,
)


class AskCliTest(unittest.TestCase):
    def _write_policy(self, tmpdir: str, policy: dict) -> str:
        path = Path(tmpdir) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return str(path)

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

    def test_ask_includes_memory_suggestions_in_plan(self) -> None:
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
                            "remember the model",
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
            self.assertIn("Suggested memory updates (advisory only):", output)
            self.assertIn("gismo memory put", output)

            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            payload = event.json_payload
            assert payload is not None
            plan = payload["plan"]
            suggestions = plan.get("memory_suggestions")
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0]["key"], "default_model")

    def test_ask_uses_memory_profile_filters_and_records_audit(self) -> None:
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
                name="operator-profile",
                description="Operator default",
                include_namespaces=["global"],
                exclude_namespaces=None,
                include_kinds=["fact", "preference"],
                exclude_kinds=None,
                max_items=1,
            )
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
                        "recall context",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
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
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
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

    def test_ask_rejects_retired_memory_profile(self) -> None:
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
            profile = memory_create_profile(
                db_path,
                name="retired-profile",
                description=None,
                include_namespaces=["global"],
                exclude_namespaces=None,
                include_kinds=["fact"],
                exclude_kinds=None,
                max_items=None,
            )
            memory_retire_profile(db_path, profile_id=profile.profile_id)
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
                    with self.assertRaises(SystemExit) as exc:
                        cli_main.run_ask(
                            db_path,
                            "recall context",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                            yes=False,
                            explain=False,
                            memory_profile=profile.name,
                        )
            self.assertEqual(exc.exception.code, 2)

    def test_ask_invalid_memory_suggestions_are_dropped_and_capped(self) -> None:
        response = json.dumps(
            {
                "intent": "remember",
                "assumptions": [],
                "actions": [],
                "notes": [],
                "memory_suggestions": [
                    {
                        "namespace": "global",
                        "key": "",
                        "kind": "note",
                        "value_json": "\"oops\"",
                        "confidence": "low",
                        "why": "missing key",
                    },
                    {
                        "namespace": "global",
                        "key": "one",
                        "kind": "fact",
                        "value_json": "\"one\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                    {
                        "namespace": "global",
                        "key": "two",
                        "kind": "fact",
                        "value_json": "\"two\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                    {
                        "namespace": "global",
                        "key": "three",
                        "kind": "fact",
                        "value_json": "\"three\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                    {
                        "namespace": "global",
                        "key": "four",
                        "kind": "fact",
                        "value_json": "\"four\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                    {
                        "namespace": "global",
                        "key": "five",
                        "kind": "fact",
                        "value_json": "\"five\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                    {
                        "namespace": "global",
                        "key": "six",
                        "kind": "fact",
                        "value_json": "\"six\"",
                        "confidence": "high",
                        "why": "valid",
                    },
                ],
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
                        "remember the model",
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
            suggestions = plan.get("memory_suggestions")
            self.assertEqual(len(suggestions), 5)
            notes = plan["notes"]
            self.assertIn("Ignored 1 invalid memory_suggestion(s).", notes)
            self.assertIn("Truncated memory_suggestions to 5 item(s).", notes)

    def test_ask_does_not_write_memory_items(self) -> None:
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
            StateStore(db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                initial_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
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
                        "remember the model",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
                    )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                final_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            self.assertEqual(initial_count, final_count)

    def test_ask_apply_memory_suggestions_writes_items_and_links_audit(self) -> None:
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
            policy_path = self._write_policy(
                tmpdir,
                {
                    "allowed_tools": ["memory.put"],
                    "memory": {"allow": {"memory.put": ["global"]}},
                },
            )
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
                        "remember the model",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=True,
                        explain=False,
                        apply_memory_suggestions=True,
                        policy_path=policy_path,
                    )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
                event_row = connection.execute(
                    "SELECT related_ask_event_id, result_meta_json FROM memory_events"
                ).fetchone()
            self.assertEqual(item_count, 1)
            self.assertIsNotNone(event_row)
            related_ask_event_id, result_meta_json = event_row
            self.assertIsNotNone(related_ask_event_id)
            result_meta = json.loads(result_meta_json)
            self.assertEqual(result_meta["policy_action"], "memory.put")
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            self.assertEqual(event.id, related_ask_event_id)
            payload = event.json_payload
            assert payload is not None
            self.assertTrue(payload["apply_memory_suggestions_requested"])
            self.assertEqual(payload["apply_memory_suggestions_result"]["applied"], 1)
            self.assertEqual(
                payload["apply_memory_suggestions_applied"],
                [{"namespace": "global", "key": "default_model"}],
            )

    def test_ask_apply_memory_suggestions_requires_confirmation_interactive(self) -> None:
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
                    with mock.patch("builtins.input", return_value="y"), mock.patch(
                        "sys.stdin.isatty",
                        return_value=True,
                    ), mock.patch("sys.stdout.isatty", return_value=True):
                        cli_main.run_ask(
                            db_path,
                            "remember preference",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                            yes=False,
                            explain=False,
                            apply_memory_suggestions=True,
                            policy_path=policy_path,
                        )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            self.assertEqual(item_count, 1)

    def test_ask_apply_memory_suggestions_non_interactive_fails_closed(self) -> None:
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
                    with self.assertRaises(SystemExit):
                        cli_main.run_ask(
                            db_path,
                            "remember preference",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                            yes=False,
                            explain=False,
                            apply_memory_suggestions=True,
                            non_interactive=True,
                            policy_path=policy_path,
                        )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            self.assertEqual(item_count, 0)

    def test_ask_apply_memory_suggestions_denied_for_retired_namespace(self) -> None:
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
                        "remember preference",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=True,
                        explain=False,
                        apply_memory_suggestions=True,
                        policy_path=policy_path,
                    )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
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
                    with self.assertRaises(ValueError) as exc:
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
            self.assertIn("LLM response was not valid JSON", str(exc.exception))
            self.assertIn("Model violated JSON-only contract", str(exc.exception))

            state_store = StateStore(db_path)
            events = state_store.list_events()
            self.assertTrue(events)
            self.assertEqual(events[0].event_type, EVENT_TYPE_LLM_PLAN)
    
    def test_ask_best_effort_json_extraction_succeeds(self) -> None:
        response = (
            "Here is the plan:\n"
            "```json\n"
            "{\"intent\":\"queue\",\"assumptions\":[],\"actions\":["
            "{\"type\":\"enqueue\",\"command\":\"echo: hello\","
            "\"timeout_seconds\":30,\"retries\":0,\"why\":\"test\",\"risk\":\"low\"}"
            "],\"notes\":[]}\n"
            "```\n"
            "Thanks."
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
                        "enqueue hello",
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
            self.assertEqual(plan["intent"], "queue")
            self.assertEqual(plan["actions"][0]["command"], "echo: hello")

    def test_ask_best_effort_json_extraction_fails_on_comments(self) -> None:
        response = (
            "{"
            "\"intent\": \"bad\","
            "\"assumptions\": [],"
            "\"actions\": [],"
            "\"notes\": []"
            "// trailing comment"
            "}"
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
                    with self.assertRaises(ValueError) as exc:
                        cli_main.run_ask(
                            db_path,
                            "bad plan",
                            model=None,
                            host=None,
                            timeout_s=None,
                            enqueue=False,
                            dry_run=True,
                            max_actions=10,
                            yes=False,
                            explain=False,
                        )
            self.assertIn("LLM response was not valid JSON", str(exc.exception))
            self.assertIn("Model violated JSON-only contract", str(exc.exception))

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

    def test_ask_env_defaults_used_for_llm_timeout(self) -> None:
        response = json.dumps(
            {"intent": "ping", "assumptions": [], "actions": [], "notes": []}
        )
        env = {
            "GISMO_LLM_TIMEOUT_S": "33",
            "GISMO_OLLAMA_TIMEOUT_S": "",
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
                    self.assertEqual(kwargs["timeout_s"], 33)

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
                    parser = cli_main.build_parser()
                    args = parser.parse_args(
                        ["ask", "--db", db_path, "ping", "--dry-run", "--timeout-s", "2"]
                    )
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        args.handler(args)
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

    def test_normalize_plan_flags_too_many_actions(self) -> None:
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
        plan = {
            "intent": "enqueue notes",
            "assumptions": [],
            "actions": actions,
            "notes": [],
        }
        normalized = cli_main._normalize_llm_plan(plan, max_actions=20)
        self.assertTrue(
            any("Too many actions (13)" in note for note in normalized["notes"])
        )

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

    def test_ask_without_memory_has_no_audit_metadata(self) -> None:
        response = json.dumps({"intent": "noop", "assumptions": [], "actions": [], "notes": []})
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
                        "noop",
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
            self.assertNotIn("memory_injection_enabled", payload)
            self.assertNotIn("memory_injected_count", payload)

    def test_ask_memory_injection_filters_and_orders(self) -> None:
        response = json.dumps({"intent": "noop", "assumptions": [], "actions": [], "notes": []})
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            memory_put_item(
                db_path,
                namespace="global",
                key="alpha",
                kind="fact",
                value={"a": 1},
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_put_item(
                db_path,
                namespace="project:alpha",
                key="beta",
                kind="preference",
                value={"b": 2},
                tags=None,
                confidence="medium",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_put_item(
                db_path,
                namespace="global",
                key="gamma",
                kind="note",
                value={"c": 3},
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_put_item(
                db_path,
                namespace="private",
                key="delta",
                kind="fact",
                value={"d": 4},
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_put_item(
                db_path,
                namespace="global",
                key="epsilon",
                kind="constraint",
                value={"e": 5},
                tags=None,
                confidence="low",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_put_item(
                db_path,
                namespace="project:alpha",
                key="zeta",
                kind="procedure",
                value={"z": 6},
                tags=None,
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            memory_tombstone_item(
                db_path,
                "project:alpha",
                "zeta",
                actor="test",
                policy_hash="test",
            )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE memory_items SET updated_at = ? WHERE namespace = ? AND key = ?",
                    ("2024-01-02T00:00:00+00:00", "global", "alpha"),
                )
                connection.execute(
                    "UPDATE memory_items SET updated_at = ? WHERE namespace = ? AND key = ?",
                    ("2024-01-03T00:00:00+00:00", "project:alpha", "beta"),
                )
                connection.commit()
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
                with mock.patch.object(cli_main, "ollama_chat", return_value=response) as mocked:
                    cli_main.run_ask(
                        db_path,
                        "noop",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
                        use_memory=True,
                    )
            user_prompt = mocked.call_args[0][0]
            match = re.search(
                r"<<<< MEMORY READ ONLY >>>>\n(.*)\n<<<< END MEMORY >>>>",
                user_prompt,
                re.DOTALL,
            )
            assert match is not None
            entries = json.loads(match.group(1))
            self.assertEqual([entry["key"] for entry in entries], ["beta", "alpha"])
            self.assertEqual(entries[0]["namespace"], "project:alpha")
            self.assertEqual(entries[1]["namespace"], "global")
            state_store = StateStore(db_path)
            event = state_store.list_events()[0]
            payload = event.json_payload
            assert payload is not None
            self.assertTrue(payload["memory_injection_enabled"])
            self.assertEqual(payload["memory_injected_count"], 2)
            self.assertEqual(
                payload["memory_injected_keys"],
                [{"namespace": "project:alpha", "key": "beta"}, {"namespace": "global", "key": "alpha"}],
            )
            self.assertLessEqual(payload["memory_injected_bytes"], 8192)
            os.remove(db_path)

    def test_ask_memory_enforces_item_and_byte_caps(self) -> None:
        response = json.dumps({"intent": "noop", "assumptions": [], "actions": [], "notes": []})
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            for index in range(25):
                memory_put_item(
                    db_path,
                    namespace="global",
                    key=f"k{index:02d}",
                    kind="fact",
                    value={"blob": "x" * 1000},
                    tags=None,
                    confidence="high",
                    source="operator",
                    ttl_seconds=None,
                    actor="test",
                    policy_hash="test",
                )
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE memory_items SET updated_at = ? WHERE namespace = ?",
                    ("2024-01-04T00:00:00+00:00", "global"),
                )
                connection.commit()
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
                with mock.patch.object(cli_main, "ollama_chat", return_value=response) as mocked:
                    cli_main.run_ask(
                        db_path,
                        "noop",
                        model=None,
                        host=None,
                        timeout_s=None,
                        enqueue=False,
                        dry_run=True,
                        max_actions=10,
                        yes=False,
                        explain=False,
                        use_memory=True,
                    )
            user_prompt = mocked.call_args[0][0]
            match = re.search(
                r"<<<< MEMORY READ ONLY >>>>\n(.*)\n<<<< END MEMORY >>>>",
                user_prompt,
                re.DOTALL,
            )
            assert match is not None
            entries = json.loads(match.group(1))
            keys = [entry["key"] for entry in entries]
            self.assertLessEqual(len(entries), 20)
            self.assertEqual(keys, sorted(keys))
            state_store = StateStore(db_path)
            payload = state_store.list_events()[0].json_payload
            assert payload is not None
            self.assertLessEqual(payload["memory_injected_bytes"], 8192)
            self.assertEqual(payload["memory_injected_count"], len(entries))


if __name__ == "__main__":
    unittest.main()
