"""Tests for interactive plan approval — StateStore CRUD, CLI, and web API."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gismo.core.models import PlanStatus
from gismo.core.state import StateStore

# ── test fixtures ──────────────────────────────────────────────────────────

_PLAN_JSON = {
    "intent": "greet",
    "assumptions": [],
    "actions": [
        {"type": "enqueue", "command": "echo:hello", "timeout_seconds": 30, "retries": 0, "why": "say hi", "risk": "low"},
        {"type": "enqueue", "command": "note:noted", "timeout_seconds": 30, "retries": 0, "why": "record", "risk": "low"},
    ],
    "notes": [],
}
_RISK_JSON = {"risk_level": "LOW", "risk_flags": [], "rationale": ["Read-only."]}
_EXPLAIN_JSON = {"summary": "intent=greet actions=2", "risk_level": "LOW"}


def _make_db(tmp: str) -> str:
    db_path = str(Path(tmp) / "state.db")
    with StateStore(db_path):
        pass
    return db_path


def _create_plan(store: StateStore, **kwargs) -> "object":
    defaults = dict(
        intent="greet",
        plan_json=dict(_PLAN_JSON),
        risk_level="LOW",
        risk_json=dict(_RISK_JSON),
        explain_json=dict(_EXPLAIN_JSON),
        user_text="say hello",
    )
    defaults.update(kwargs)
    return store.create_pending_plan(**defaults)


# ── StateStore CRUD ────────────────────────────────────────────────────────

class TestPendingPlanStateStore(unittest.TestCase):
    def test_create_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                fetched = store.get_pending_plan(plan.id)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.status, PlanStatus.PENDING)
            self.assertEqual(fetched.intent, "greet")
            self.assertEqual(fetched.risk_level, "LOW")

    def test_list_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                _create_plan(store)
                _create_plan(store, intent="second")
                plans = store.list_pending_plans()
            self.assertEqual(len(plans), 2)

    def test_list_filter_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                p1 = _create_plan(store)
                _create_plan(store, intent="second")
                store.approve_pending_plan(p1.id)
                pending = store.list_pending_plans(status=PlanStatus.PENDING)
                approved = store.list_pending_plans(status=PlanStatus.APPROVED)
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(approved), 1)

    def test_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                result = store.approve_pending_plan(plan.id)
            self.assertEqual(result.status, PlanStatus.APPROVED)
            self.assertIsNotNone(result.approved_at)

    def test_reject_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                result = store.reject_pending_plan(plan.id, reason="too risky")
            self.assertEqual(result.status, PlanStatus.REJECTED)
            self.assertEqual(result.rejection_reason, "too risky")
            self.assertIsNotNone(result.rejected_at)

    def test_approve_non_pending_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                store.reject_pending_plan(plan.id)
                # approve on already-rejected plan should not change status
                result = store.approve_pending_plan(plan.id)
            self.assertEqual(result.status, PlanStatus.REJECTED)

    def test_update_plan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                new_plan = dict(_PLAN_JSON)
                new_plan["actions"] = [new_plan["actions"][0]]  # remove second action
                result = store.update_pending_plan_json(plan.id, new_plan)
            self.assertEqual(len(result.plan_json["actions"]), 1)

    def test_update_plan_json_non_pending_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                store.approve_pending_plan(plan.id)
                result = store.update_pending_plan_json(plan.id, {"actions": []})
            # Approved plan should not have been changed
            self.assertEqual(len(result.plan_json["actions"]), 2)

    def test_resolve_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                matches = store.resolve_pending_plan_id(plan.id[:8])
            self.assertIn(plan.id, matches)

    def test_resolve_prefix_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                matches = store.resolve_pending_plan_id("xxxxxxxx")
            self.assertEqual(matches, [])

    def test_get_not_found_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                result = store.get_pending_plan("nonexistent-id")
            self.assertIsNone(result)


# ── plan_store.enqueue_plan_actions ────────────────────────────────────────

class TestEnqueuePlanActions(unittest.TestCase):
    def test_enqueues_valid_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                from gismo.core.plan_store import enqueue_plan_actions
                ids, skipped = enqueue_plan_actions(store, _PLAN_JSON)
            self.assertEqual(len(ids), 2)
            self.assertEqual(skipped, [])

    def test_skips_empty_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            plan = {"actions": [{"type": "enqueue", "command": ""}]}
            with StateStore(db) as store:
                from gismo.core.plan_store import enqueue_plan_actions
                ids, skipped = enqueue_plan_actions(store, plan)
            self.assertEqual(ids, [])
            self.assertEqual(len(skipped), 1)

    def test_skips_non_enqueue_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            plan = {"actions": [{"type": "echo", "command": "echo:hi"}]}
            with StateStore(db) as store:
                from gismo.core.plan_store import enqueue_plan_actions
                ids, skipped = enqueue_plan_actions(store, plan)
            self.assertEqual(ids, [])
            self.assertEqual(skipped, [])


# ── Web API ────────────────────────────────────────────────────────────────

class TestWebApiPlans(unittest.TestCase):
    def test_get_plans_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_plans
            self.assertEqual(get_plans(db), [])

    def test_get_plans_with_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                _create_plan(store)
            from gismo.web.api import get_plans
            plans = get_plans(db)
            self.assertEqual(len(plans), 1)
            self.assertIn("id", plans[0])
            self.assertIn("risk_level", plans[0])
            self.assertIn("intent", plans[0])
            self.assertIn("action_count", plans[0])

    def test_get_plans_filter_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                p = _create_plan(store)
                store.approve_pending_plan(p.id)
                _create_plan(store, intent="pending one")
            from gismo.web.api import get_plans
            pending = get_plans(db, status="PENDING")
            approved = get_plans(db, status="APPROVED")
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(approved), 1)

    def test_get_plan_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import get_plan_detail
            detail = get_plan_detail(db, plan.id)
            self.assertEqual(detail["id"], plan.id)
            self.assertIn("plan", detail)
            self.assertIn("explain", detail)
            self.assertIn("risk", detail)

    def test_get_plan_detail_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_plan_detail
            with self.assertRaises(ValueError):
                get_plan_detail(db, "nonexistent")

    def test_approve_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import approve_plan
            result = approve_plan(db, plan.id)
            self.assertEqual(result["status"], "APPROVED")
            self.assertEqual(len(result["enqueued_ids"]), 2)

    def test_approve_plan_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import approve_plan
            with self.assertRaises(ValueError):
                approve_plan(db, "nonexistent")

    def test_approve_already_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                store.approve_pending_plan(plan.id)
            from gismo.web.api import approve_plan
            with self.assertRaises(ValueError):
                approve_plan(db, plan.id)

    def test_reject_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import reject_plan
            result = reject_plan(db, plan.id, reason="not needed")
            self.assertEqual(result["status"], "REJECTED")
            self.assertEqual(result["reason"], "not needed")

    def test_patch_plan_edit_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import patch_plan, get_plan_detail
            patch_plan(db, plan.id, action_index=0, new_command="echo:changed")
            detail = get_plan_detail(db, plan.id)
            self.assertEqual(detail["plan"]["actions"][0]["command"], "echo:changed")

    def test_patch_plan_remove_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import patch_plan, get_plan_detail
            patch_plan(db, plan.id, action_index=0, remove_action=True)
            detail = get_plan_detail(db, plan.id)
            self.assertEqual(len(detail["plan"]["actions"]), 1)

    def test_patch_plan_index_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
            from gismo.web.api import patch_plan
            with self.assertRaises(ValueError):
                patch_plan(db, plan.id, action_index=99, new_command="echo:x")

    def test_patch_approved_plan_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with StateStore(db) as store:
                plan = _create_plan(store)
                store.approve_pending_plan(plan.id)
            from gismo.web.api import patch_plan
            with self.assertRaises(ValueError):
                patch_plan(db, plan.id, action_index=0, new_command="echo:x")


if __name__ == "__main__":
    unittest.main()
