from __future__ import annotations

import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.models import QueueStatus, TaskStatus
from gismo.core.state import StateStore
from gismo.web import api as web_api


def _make_db(tmp: str) -> str:
    db_path = str(Path(tmp) / "state.db")
    with StateStore(db_path) as store:
        run = store.create_run(label="test-run")
        task = store.create_task(
            run.id,
            title="Echo hello",
            description="desc",
            input_json={"tool": "echo", "payload": {"message": "hello"}},
        )
        task.status = TaskStatus.SUCCEEDED
        store.update_task(task)
        store.enqueue_command("echo world")
    return db_path


class TestPhase6StatusAndChat(unittest.TestCase):
    def _tmpdir(self, prefix: str) -> Path:
        path = Path("tmp") / f"{prefix}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_status_surfaces_offline_when_worker_missing(self) -> None:
        tmp = self._tmpdir("phase6-status")
        try:
            db = _make_db(str(tmp))
            with mock.patch("gismo.core.readiness.get_model_health", return_value={
                "issues": [],
                "degraded_mode": {"active": False, "reason": None},
                "ollama_available": True,
            }):
                data = web_api.get_status(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["daemon"]["state"], "offline")
        self.assertEqual(data["gismo"]["state"], "offline")
        self.assertIn("summary", data["gismo"])

    def test_status_surfaces_approval_needed(self) -> None:
        tmp = self._tmpdir("phase6-approval")
        try:
            db = _make_db(str(tmp))
            with StateStore(db) as store:
                store.set_daemon_heartbeat(
                    pid=1234,
                    started_at=datetime.now(timezone.utc),
                    last_seen=datetime.now(timezone.utc),
                    version="test",
                )
                store.create_pending_plan(
                    intent="operate",
                    plan_json={"intent": "operate", "actions": []},
                    risk_level="HIGH",
                    risk_json={"risk_level": "HIGH", "risk_flags": ["writes"], "rationale": ["test"]},
                    explain_json={"summary": "test"},
                    user_text="turn on the kitchen lights",
                    actor="web-chat",
                )
            with mock.patch("gismo.core.readiness.get_model_health", return_value={
                "issues": [],
                "degraded_mode": {"active": False, "reason": None},
                "ollama_available": True,
            }), mock.patch("gismo.core.readiness.get_operator_name", return_value="Mike"):
                data = web_api.get_status(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["gismo"]["state"], "approval_needed")
        self.assertEqual(data["approvals"]["pending_count"], 1)

    def test_low_risk_operational_chat_uses_execution_mode(self) -> None:
        plan = {
            "intent": "device_check",
            "actions": [
                {
                    "type": "enqueue",
                    "command": "device: check cameras",
                    "timeout_seconds": 30,
                    "retries": 0,
                    "why": "check cameras",
                    "risk": "low",
                }
            ],
            "notes": [],
        }
        risk = {"risk_level": "LOW", "risk_flags": [], "rationale": ["safe"]}
        explain = {"summary": "device_check"}
        tmp = self._tmpdir("phase6-chat-exec")
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_request_chat_plan", return_value=(plan, risk, explain)), mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "check the cameras", [])
            with StateStore(db) as store:
                pending = store.list_pending_plans()
                queue_items = store.list_queue_items(limit=10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertEqual(data["reply"], "Working on that now.")
        self.assertEqual(pending, [])
        self.assertEqual(len(queue_items), 2)
        self.assertEqual(queue_items[0].status, QueueStatus.QUEUED)

    def test_high_risk_operational_chat_stays_pending(self) -> None:
        plan = {
            "intent": "device_power",
            "actions": [
                {
                    "type": "enqueue",
                    "command": "device: turn on kitchen lights",
                    "timeout_seconds": 30,
                    "retries": 0,
                    "why": "turn on kitchen lights",
                    "risk": "high",
                }
            ],
            "notes": [],
        }
        risk = {"risk_level": "HIGH", "risk_flags": ["writes"], "rationale": ["approval needed"]}
        explain = {"summary": "device_power"}
        tmp = self._tmpdir("phase6-chat-plan")
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_request_chat_plan", return_value=(plan, risk, explain)), mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "turn on the kitchen lights", [])
            with StateStore(db) as store:
                pending = store.list_pending_plans()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "plan")
        self.assertEqual(len(pending), 1)
        self.assertEqual(data["risk_level"], "HIGH")

    def test_execution_status_reports_completion_without_overclaiming(self) -> None:
        tmp = self._tmpdir("phase6-execution-complete")
        try:
            db = _make_db(str(tmp))
            with StateStore(db) as store:
                run = store.create_run(label="execution", metadata={"queue_item_id": "queue-1"})
                task = store.create_task(
                    run.id,
                    title="Check cameras",
                    description="desc",
                    input_json={"tool": "echo", "payload": {"message": "done"}},
                )
                task.mark_succeeded({"summary": "I checked 1 camera. Online: Front Door."})
                store.update_task(task)
                item = store.enqueue_command("device: check cameras", run_id=run.id)
                store.mark_queue_item_succeeded(item.id)
            status = web_api.get_execution_status(db, queue_item_ids=[item.id])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["final"])
        self.assertIn("I ran that successfully.", status["message"])

    def test_execution_status_reports_policy_block(self) -> None:
        tmp = self._tmpdir("phase6-execution-blocked")
        try:
            db = _make_db(str(tmp))
            with StateStore(db) as store:
                run = store.create_run(label="execution", metadata={"queue_item_id": "queue-1"})
                task = store.create_task(
                    run.id,
                    title="Blocked task",
                    description="desc",
                    input_json={"tool": "device_control", "payload": {"action": "scan"}},
                )
                task.mark_failed("blocked by policy")
                store.update_task(task)
                item = store.enqueue_command("device: scan", run_id=run.id)
                store.mark_queue_item_failed(item.id, "blocked by policy", retryable=False)
            status = web_api.get_execution_status(db, queue_item_ids=[item.id])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(status["state"], "blocked")
        self.assertTrue(status["final"])
        self.assertEqual(status["message"], "That was blocked by policy.")


class TestPhase6OnboardingAndDevices(unittest.TestCase):
    def _tmpdir(self, prefix: str) -> Path:
        path = Path("tmp") / f"{prefix}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_onboarding_status_includes_performance_defaults(self) -> None:
        tmp = self._tmpdir("phase6-onboarding")
        try:
            db = _make_db(str(tmp))
            data = web_api.get_onboarding_status(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertIn("voices", data)
        self.assertIn("performance_mode", data)
        self.assertIn("performance_modes", data)

    def test_complete_onboarding_persists_performance_mode(self) -> None:
        tmp = self._tmpdir("phase6-onboarding-complete")
        try:
            db = _make_db(str(tmp))
            with mock.patch("gismo.llm.model_policy.discover_models", return_value={
                "installed_models": [],
                "loaded_models": [],
                "ollama_available": False,
            }):
                result = web_api.complete_onboarding(
                    db,
                    "Mike",
                    "af_sky",
                    performance_mode="prefer_responsiveness",
                )
            onboarding = web_api.get_onboarding_status(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["performance_mode"], "prefer_responsiveness")
        self.assertEqual(onboarding["operator_name"], "Mike")

    def test_device_capabilities_are_surfaced(self) -> None:
        tmp = self._tmpdir("phase6-device-caps")
        try:
            db = _make_db(str(tmp))
            with mock.patch(
                "gismo.web.api.execute_device_runtime_action",
                side_effect=[
                    {
                        "devices": [{"id": "device-1", "status": "online"}],
                        "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                    },
                    {
                        "devices": [{"id": "device-1", "status": "online"}],
                        "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                    },
                ],
            ):
                web_api.add_device(
                    db,
                    "192.168.1.20",
                    "Front Door",
                    "camera",
                    "Tapo",
                    rtsp_url="rtsp://192.168.1.20:554/stream1",
                    snapshot_url="http://192.168.1.20/snapshot.jpg",
                    open_ports=[554],
                )
                devices = web_api.list_devices(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(devices[0]["capabilities"], ["status", "stream", "snapshot"])
