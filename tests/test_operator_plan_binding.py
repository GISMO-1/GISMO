from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gismo.core import daemon as daemon_module
from gismo.core.models import QueueStatus
from gismo.core.operator_plan import (
    OperatorPlanValidationError,
    build_operator_plan_binding,
    verify_operator_plan_binding,
)
from gismo.core.plan_store import enqueue_plan_actions
from gismo.core.state import StateStore


def _echo_plan(message: str = "hi") -> dict[str, object]:
    return {
        "mode": "single",
        "steps": [
            {
                "tool_name": "echo",
                "input_json": {"message": message},
                "title": f"Echo: {message}",
            }
        ],
    }


class OperatorPlanBindingTest(unittest.TestCase):
    def test_unknown_tool_and_action_are_rejected(self) -> None:
        unknown_tool = _echo_plan()
        unknown_tool["steps"][0]["tool_name"] = "unreviewed_tool"  # type: ignore[index]
        with self.assertRaisesRegex(OperatorPlanValidationError, "tool is not allowed"):
            build_operator_plan_binding(visible_command="echo: hi", operator_plan=unknown_tool)

        unknown_action = {
            "mode": "single",
            "steps": [{
                "tool_name": "device_control",
                "input_json": {"request": "do it", "action": "flash", "target": "lamp-1"},
                "title": "Device action",
            }],
        }
        with self.assertRaisesRegex(OperatorPlanValidationError, "action is not allowed"):
            build_operator_plan_binding(visible_command="devices: do it", operator_plan=unknown_action)

    def test_mapping_key_order_is_stable_but_graph_step_order_is_bound(self) -> None:
        plan = {
            "mode": "graph",
            "steps": [
                {"tool_name": "echo", "input_json": {"message": "first"}, "title": "First"},
                {"tool_name": "echo", "input_json": {"message": "second"}, "title": "Second"},
            ],
        }
        canonical, binding = build_operator_plan_binding(visible_command="echo graph", operator_plan=plan)
        reordered_keys = {
            "steps": [
                {"title": "First", "input_json": {"message": "first"}, "tool_name": "echo"},
                {"input_json": {"message": "second"}, "tool_name": "echo", "title": "Second"},
            ],
            "mode": "graph",
        }
        verified, _ = verify_operator_plan_binding(
            visible_command="echo graph",
            operator_plan=reordered_keys,
            binding=binding,
        )
        self.assertEqual(verified, canonical)

        reordered_steps = {"mode": "graph", "steps": list(reversed(plan["steps"]))}
        with self.assertRaisesRegex(OperatorPlanValidationError, "plan digest mismatch"):
            verify_operator_plan_binding(
                visible_command="echo graph",
                operator_plan=reordered_steps,
                binding=binding,
            )

    def test_enqueue_stores_canonical_binding_and_daemon_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            ids, skipped = enqueue_plan_actions(
                store,
                {
                    "actions": [
                        {
                            "type": "enqueue",
                            "command": "echo: hi",
                            "timeout_seconds": 30,
                            "retries": 0,
                            "metadata": {
                                "operator_plan": _echo_plan("hi"),
                                "normalized_command": "echo: hi",
                            },
                        }
                    ]
                },
                approval_id="approval-1",
            )

            self.assertEqual(skipped, [])
            self.assertEqual(len(ids), 1)
            item = store.get_queue_item(ids[0])
            assert item is not None
            metadata = item.metadata_json
            self.assertEqual(metadata["normalized_command"], "echo: hi")
            self.assertIn("operator_plan_binding", metadata)
            self.assertEqual(metadata["operator_plan_binding"]["visible_command"], "echo: hi")

            daemon_module.run_daemon_loop(
                store,
                policy_path=None,
                sleep_seconds=0.0,
                once=True,
            )

            updated = store.get_queue_item(ids[0])
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.SUCCEEDED)

    def test_enqueue_rejects_malformed_operator_plan_and_records_security_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            ids, skipped = enqueue_plan_actions(
                store,
                {
                    "actions": [
                        {
                            "type": "enqueue",
                            "command": "echo: hi",
                            "metadata": {
                                "operator_plan": {
                                    "mode": "single",
                                    "steps": [
                                        {
                                            "tool_name": "echo",
                                            "input_json": {"message": "hi"},
                                            "title": "Echo: hi",
                                            "allowed_tools": ["run_shell"],
                                        }
                                    ],
                                }
                            },
                        }
                    ]
                },
            )

            self.assertEqual(ids, [])
            self.assertEqual(len(skipped), 1)
            self.assertIn("unexpected fields", skipped[0])
            events = store.list_security_events(limit=10, event_type="operator_plan_binding_failed")
            self.assertEqual(len(events), 1)
            self.assertIn("unexpected fields", events[0].payload["reason"])

    def test_enqueue_rejects_caller_supplied_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            ids, skipped = enqueue_plan_actions(
                store,
                {
                    "actions": [
                        {
                            "type": "enqueue",
                            "command": "echo: hi",
                            "metadata": {
                                "operator_plan": _echo_plan("hi"),
                                "operator_plan_binding": {"version": 1},
                            },
                        }
                    ]
                },
            )

            self.assertEqual(ids, [])
            self.assertEqual(len(skipped), 1)
            self.assertIn("generated internally", skipped[0])

    def test_daemon_rejects_missing_binding_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            item = store.enqueue_command(
                "echo: hi",
                metadata={
                    "operator_plan": _echo_plan("hi"),
                    "normalized_command": "echo: hi",
                },
            )

            daemon_module.run_daemon_loop(
                store,
                policy_path=None,
                sleep_seconds=0.0,
                once=True,
            )

            updated = store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.FAILED)
            self.assertIn("Rejected operator plan binding", updated.last_error or "")
            events = store.list_security_events(limit=10, event_type="operator_plan_binding_rejected")
            self.assertEqual(len(events), 1)
            self.assertIn("operator_plan_binding must be an object", events[0].payload["reason"])

    def test_daemon_rejects_changed_command_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            canonical_plan, binding = build_operator_plan_binding(
                visible_command="echo: hi",
                operator_plan=_echo_plan("hi"),
            )
            item = store.enqueue_command(
                "echo: changed",
                metadata={
                    "operator_plan": canonical_plan,
                    "operator_plan_binding": binding,
                    "normalized_command": "echo: hi",
                },
            )

            daemon_module.run_daemon_loop(
                store,
                policy_path=None,
                sleep_seconds=0.0,
                once=True,
            )

            updated = store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.FAILED)
            self.assertIn("visible command mismatch", updated.last_error or "")

    def test_daemon_rejects_changed_plan_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            _, binding = build_operator_plan_binding(
                visible_command="echo: hi",
                operator_plan=_echo_plan("hi"),
            )
            item = store.enqueue_command(
                "echo: hi",
                metadata={
                    "operator_plan": _echo_plan("changed"),
                    "operator_plan_binding": binding,
                    "normalized_command": "echo: hi",
                },
            )

            daemon_module.run_daemon_loop(
                store,
                policy_path=None,
                sleep_seconds=0.0,
                once=True,
            )

            updated = store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.FAILED)
            self.assertIn("plan digest mismatch", updated.last_error or "")


if __name__ == "__main__":
    unittest.main()
