"""Tests for gismo.web.api — pure data layer."""
from __future__ import annotations

import ipaddress
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from gismo.core.models import TaskStatus, QueueStatus
from gismo.core.state import StateStore
from gismo.llm.ollama import OllamaError
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


def _write_devices_config(root: Path, devices: list[dict]) -> None:
    config_dir = root / ".gismo"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "devices.json").write_text(json.dumps({"devices": devices}), encoding="utf-8")


class TestGetStatus(unittest.TestCase):
    def test_no_daemon(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            data = web_api.get_status(db)
            self.assertIn("daemon", data)
            self.assertIn("queue", data)
            self.assertFalse(data["daemon"]["running"])
            self.assertEqual(data["daemon"]["state"], "offline")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_queue_stats_included(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            data = web_api.get_status(db)
            self.assertIn("total", data["queue"])
            self.assertGreater(data["queue"]["total"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSetDaemonPaused(unittest.TestCase):
    def test_pause_and_resume(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            result = web_api.set_daemon_paused(db, True)
            self.assertTrue(result["paused"])
            result = web_api.set_daemon_paused(db, False)
            self.assertFalse(result["paused"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBriefingLanguage(unittest.TestCase):
    def test_briefing_avoids_daemon_terms(self) -> None:
        with mock.patch("gismo.onboarding.get_operator_name", return_value="Mike"), mock.patch.object(
            web_api,
            "get_status",
            return_value={
                "daemon": {"running": False, "paused": False},
                "queue": {"by_status": {"QUEUED": 0, "IN_PROGRESS": 0, "FAILED": 0, "SUCCEEDED": 2}},
            },
        ):
            data = web_api.get_briefing("tmp/state.db")

        briefing = data["briefing"].lower()
        self.assertNotIn("daemon", briefing)
        self.assertNotIn("heartbeat", briefing)
        self.assertIn("gismo is ready", briefing)


class TestGetQueueStats(unittest.TestCase):
    def test_returns_store_queue_stats(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            data = web_api.get_queue_stats(db)
            self.assertIn("total", data)
            self.assertIn("by_status", data)
            self.assertEqual(data["by_status"]["QUEUED"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGetQueue(unittest.TestCase):
    def test_returns_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            items = web_api.get_queue(db)
            self.assertIsInstance(items, list)
            self.assertGreater(len(items), 0)

    def test_item_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            items = web_api.get_queue(db)
            item = items[0]
            for field in ("id", "status", "command_text", "attempt_count", "created_at"):
                self.assertIn(field, item)

    def test_command_text_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            items = web_api.get_queue(db)
            cmds = [i["command_text"] for i in items]
            self.assertIn("echo world", cmds)


class TestCancelQueueItem(unittest.TestCase):
    def test_cancel_queued_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            items = web_api.get_queue(db)
            queued = [i for i in items if i["status"] == "QUEUED"]
            self.assertTrue(queued, "Expected at least one QUEUED item")
            result = web_api.cancel_queue_item(db, queued[0]["id"])
            self.assertEqual(result["status"], "CANCELLED")

    def test_cancel_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with self.assertRaises(ValueError):
                web_api.cancel_queue_item(db, "nonexistent-id")


class TestPurgeFailed(unittest.TestCase):
    def test_no_failed_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            result = web_api.purge_failed(db)
            self.assertIn("deleted", result)
            self.assertEqual(result["deleted"], 0)

    def test_purges_failed_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            # Enqueue and mark as failed via direct DB manipulation
            with StateStore(db) as store:
                item = store.enqueue_command("echo fail-me")
                store.mark_queue_item_failed(item.id, "forced failure", retryable=False)
            result = web_api.purge_failed(db)
            self.assertGreater(result["deleted"], 0)


class TestGetRuns(unittest.TestCase):
    def test_returns_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            runs = web_api.get_runs(db)
            self.assertIsInstance(runs, list)
            self.assertGreater(len(runs), 0)

    def test_run_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            runs = web_api.get_runs(db)
            r = runs[0]
            for field in ("id", "label", "status", "created_at", "task_total", "task_succeeded", "task_failed"):
                self.assertIn(field, r)

    def test_run_status_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            runs = web_api.get_runs(db)
            # The run we created has one SUCCEEDED task
            self.assertEqual(runs[0]["status"], "succeeded")


class TestGetRunDetail(unittest.TestCase):
    def test_returns_run_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            runs = web_api.get_runs(db)
            detail = web_api.get_run_detail(db, runs[0]["id"])
            self.assertIn("tasks", detail)
            self.assertIn("tool_calls", detail)
            self.assertGreater(len(detail["tasks"]), 0)

    def test_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with self.assertRaises(ValueError):
                web_api.get_run_detail(db, "nonexistent-run-id")


class TestGetMemory(unittest.TestCase):
    def test_empty_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            data = web_api.get_memory(db)
            self.assertIn("namespaces", data)
            self.assertIn("items", data)

    def test_with_memory_item(self) -> None:
        from gismo.memory.store import put_item
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            put_item(
                db,
                namespace="global",
                key="test-key",
                kind="note",
                value="hello",
                tags=[],
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="operator",
                policy_hash="test",
            )
            data = web_api.get_memory(db)
            ns_names = [ns["namespace"] for ns in data["namespaces"]]
            self.assertIn("global", ns_names)
            self.assertIn("global", data["items"])
            keys = [i["key"] for i in data["items"]["global"]]
            self.assertIn("test-key", keys)


class TestOnboardingAndHealth(unittest.TestCase):
    def test_onboarding_status_shape(self) -> None:
        from gismo.onboarding import set_operator_name

        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            data = web_api.get_onboarding_status(db)
            self.assertIn("needs_onboarding", data)
            self.assertIn("operator_name", data)
            self.assertIsInstance(data["needs_onboarding"], bool)

            set_operator_name(db, "Mike")
            updated = web_api.get_onboarding_status(db)
            self.assertFalse(updated["needs_onboarding"])
            self.assertEqual(updated["operator_name"], "Mike")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_system_health_shape(self) -> None:
        fake_psutil = SimpleNamespace(
            cpu_percent=lambda: 12.5,
            virtual_memory=lambda: SimpleNamespace(percent=61.0),
        )
        fake_socket = mock.MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        fake_socket.__exit__.return_value = False
        with mock.patch.dict("sys.modules", {"psutil": fake_psutil}), mock.patch(
            "gismo.web.api.socket.create_connection",
            return_value=fake_socket,
        ):
            data = web_api.get_system_health()

        self.assertEqual(data["cpu_percent"], 12.5)
        self.assertEqual(data["virtual_memory"], 61.0)
        self.assertIn("lan_connected", data)
        self.assertIn("lan_type", data)
        self.assertIn("lan_name", data)
        self.assertIn("lan_signal_percent", data)
        self.assertTrue(data["internet_connected"])
        self.assertIn("internet_latency_ms", data)


class TestChatMessage(unittest.TestCase):
    def test_deterministic_calendar_today_bypasses_llm(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            today = datetime.now().date().isoformat()
            web_api.create_calendar_event(
                db,
                {
                    "title": "Check in",
                    "start_at": today + "T09:00:00",
                    "end_at": today + "T10:00:00",
                },
            )
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ), mock.patch.object(
                web_api,
                "_calendar_now_local",
                return_value=datetime(2026, 3, 18, 12, 0, tzinfo=web_api._local_tz()),
            ):
                data = web_api.chat_message(db, "what do I have today", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "reply")
        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Check in", data["reply"])
        chat_mock.assert_not_called()

    def test_deterministic_calendar_month_bypasses_llm(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            web_api.create_calendar_event(
                db,
                {
                    "title": "Dinner",
                    "start_at": "2026-03-20T18:00:00",
                    "end_at": "2026-03-20T19:00:00",
                },
            )
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ), mock.patch.object(
                web_api,
                "_calendar_now_local",
                return_value=datetime(2026, 3, 18, 12, 0, tzinfo=web_api._local_tz()),
            ):
                data = web_api.chat_message(db, "what do I have in March", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Dinner", data["reply"])
        chat_mock.assert_not_called()

    def test_deterministic_upcoming_bypasses_llm(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
            web_api.create_calendar_event(
                db,
                {
                    "title": "Dentist",
                    "start_at": tomorrow + "T11:00:00",
                    "end_at": tomorrow + "T12:00:00",
                },
            )
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "what's coming up", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Dentist", data["reply"])
        chat_mock.assert_not_called()

    def test_deterministic_device_query_bypasses_llm(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            web_api.add_device(db, "192.168.1.9", "Kitchen Lamp", "light", "Tuya")
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "what devices are connected", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Kitchen Lamp", data["reply"])
        chat_mock.assert_not_called()

    def test_deterministic_device_query_includes_saved_controls(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            _write_devices_config(
                tmp,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-LOCAL-KEY",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ), mock.patch.object(
                web_api,
                "execute_device_runtime_action",
                return_value={
                    "devices": [{"id": "dads-room-light", "status": "online"}],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ):
                data = web_api.chat_message(db, "what devices are connected", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Saved controls", data["reply"])
        self.assertIn("Dad's Room Light", data["reply"])
        chat_mock.assert_not_called()

    def test_deterministic_model_query_bypasses_llm(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "get_settings", return_value={"voice": "af_sky", "model_policy": {"primary_assistant_model": "gismo:latest"}}), mock.patch(
                "gismo.llm.ollama.ollama_freeform_chat"
            ) as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "what model are you using", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("gismo:latest", data["reply"])
        chat_mock.assert_not_called()

    def test_light_power_request_routes_through_device_enqueue(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "turn Dad's light on", [])
            with StateStore(db) as store:
                commands = [item.command_text for item in store.list_queue_items(limit=20)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertEqual(data["classification"], "operational_request")
        self.assertIn("device: turn Dad's light on", commands)
        planner_mock.assert_not_called()

    def test_light_combined_white_request_routes_through_device_enqueue(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "turn Dad's light on cool white", [])
            with StateStore(db) as store:
                commands = [item.command_text for item in store.list_queue_items(limit=20)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertIn("device: turn Dad's light on cool white", commands)
        planner_mock.assert_not_called()

    def test_light_brightness_request_routes_through_device_enqueue(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "dim Dad's light to 20 percent", [])
            with StateStore(db) as store:
                commands = [item.command_text for item in store.list_queue_items(limit=20)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertIn("device: dim Dad's light to 20 percent", commands)
        planner_mock.assert_not_called()

    def test_light_color_request_routes_through_device_enqueue(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "make Dad's light blue", [])
            with StateStore(db) as store:
                commands = [item.command_text for item in store.list_queue_items(limit=20)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertIn("device: make Dad's light blue", commands)
        planner_mock.assert_not_called()

    def test_light_color_and_brightness_request_routes_through_device_enqueue(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "set Dad's light to blue at 50 percent", [])
            with StateStore(db) as store:
                commands = [item.command_text for item in store.list_queue_items(limit=20)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertIn("device: set Dad's light to blue at 50 percent", commands)
        planner_mock.assert_not_called()

    def test_ambiguous_light_request_asks_for_clarification_without_planner(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None), mock.patch.object(
                web_api,
                "_request_chat_plan",
            ) as planner_mock:
                data = web_api.chat_message(db, "make Dad's light nice", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "clarify")
        self.assertEqual(data["classification"], "ambiguous_request")
        self.assertIn("could not understand", data["reply"].lower())
        planner_mock.assert_not_called()

    def test_conversational_request_replies_directly(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            route = SimpleNamespace(
                degraded=False,
                candidate_models=["gismo:latest"],
                capability=SimpleNamespace(history_messages=8, assistant_timeout_s=90),
                policy=SimpleNamespace(allow_identity_fallback=False),
            )
            with mock.patch.object(web_api, "resolve_model_route", return_value=route), mock.patch(
                "gismo.llm.ollama.ollama_freeform_chat", return_value="Hello there"
            ), mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "who are you", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(data["mode"], "reply")
        self.assertEqual(data["classification"], "conversational_request")
        self.assertEqual(data["reply"], "Hello there")

    def test_conversational_request_uses_explicit_identity_fallback(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            route = SimpleNamespace(
                degraded=False,
                candidate_models=["gismo:latest", "tinyllama"],
                capability=SimpleNamespace(history_messages=8, assistant_timeout_s=90),
                policy=SimpleNamespace(allow_identity_fallback=True),
            )
            with mock.patch.object(web_api, "resolve_model_route", return_value=route), mock.patch(
                "gismo.llm.ollama.ollama_freeform_chat",
                side_effect=[OllamaError("model blew up"), "Fallback answer"],
            ) as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "who are you", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["reply"], "Fallback answer")
        self.assertEqual(chat_mock.call_count, 2)
        self.assertEqual(chat_mock.call_args_list[0].kwargs["model"], "gismo:latest")
        self.assertEqual(chat_mock.call_args_list[1].kwargs["model"], "tinyllama")

    def test_conversational_request_returns_degraded_message_when_fallback_is_disabled(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            route = SimpleNamespace(
                degraded=False,
                candidate_models=["gismo:latest", "tinyllama"],
                capability=SimpleNamespace(history_messages=8, assistant_timeout_s=90),
                policy=SimpleNamespace(allow_identity_fallback=False),
            )
            with mock.patch.object(web_api, "resolve_model_route", return_value=route), mock.patch(
                "gismo.llm.ollama.ollama_freeform_chat",
                side_effect=[OllamaError("out of memory"), OllamaError("stack trace raw detail")],
            ), mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "tell me a joke", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["classification"], "conversational_request")
        self.assertEqual(data["reply"], web_api._CHAT_MODEL_ERROR_REPLY)
        self.assertNotIn("stack trace", data["reply"].lower())

    def test_conversational_request_skips_llm_when_no_installed_model_is_available(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            route = SimpleNamespace(
                degraded=True,
                candidate_models=[],
                capability=SimpleNamespace(history_messages=4, assistant_timeout_s=45),
                policy=SimpleNamespace(allow_identity_fallback=False),
            )
            with mock.patch.object(web_api, "resolve_model_route", return_value=route), mock.patch(
                "gismo.llm.ollama.ollama_freeform_chat"
            ) as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "who are you", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["reply"], web_api._CHAT_MODEL_ERROR_REPLY)
        chat_mock.assert_not_called()

    def test_ambiguous_request_asks_for_clarification(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch("gismo.llm.ollama.ollama_freeform_chat") as chat_mock, mock.patch.object(
                web_api,
                "_append_chat_record",
                return_value=None,
            ):
                data = web_api.chat_message(db, "can you help me with something", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(data["mode"], "clarify")
        self.assertEqual(data["classification"], "ambiguous_request")
        chat_mock.assert_not_called()

    def test_operational_request_creates_pending_plan(self) -> None:
        plan = {
            "intent": "operate",
            "actions": [
                {
                    "type": "enqueue",
                    "command": "echo: scanning devices",
                    "timeout_seconds": 30,
                    "retries": 0,
                    "why": "scan your network for devices",
                    "risk": "medium",
                }
            ],
            "notes": [],
        }
        risk = {"risk_level": "MEDIUM", "risk_flags": ["network_access"], "rationale": ["needs review"]}
        explain = {"summary": "intent=operate actions=1"}

        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_build_device_enqueue_plan", return_value=None), \
                 mock.patch.object(web_api, "_build_calendar_enqueue_plan", return_value=None), \
                 mock.patch.object(web_api, "_request_chat_plan", return_value=(plan, risk, explain)), \
                 mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "scan for devices", [])
            with StateStore(db) as store:
                pending = store.list_pending_plans()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "plan")
        self.assertEqual(data["classification"], "operational_request")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].user_text, "scan for devices")
        self.assertEqual(data["plan_steps"], ["Scan your network for devices"])

    def test_operational_request_returns_friendly_message_when_planner_fails(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_build_device_enqueue_plan", return_value=None), \
                 mock.patch.object(web_api, "_build_calendar_enqueue_plan", return_value=None), \
                 mock.patch.object(web_api, "_request_chat_plan", side_effect=RuntimeError("ollama exploded")), \
                 mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "scan for devices", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["reply"], web_api._CHAT_PLAN_ERROR_REPLY)
        self.assertNotIn("ollama", data["reply"].lower())
        self.assertEqual(data["mode"], "reply")

    def test_request_chat_plan_includes_saved_device_context(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        captured: dict[str, object] = {}

        def _fake_request(
            db_path: str,
            user_text: str,
            **kwargs: object,
        ) -> tuple[dict, object, object, object, object, object]:
            captured["db_path"] = db_path
            captured["user_text"] = user_text
            fake_risk = SimpleNamespace(risk_level="LOW", risk_flags=[], rationale=[])
            fake_explain = SimpleNamespace(to_dict=lambda: {"summary": "ok"})
            return {"intent": "operate", "actions": [], "notes": []}, fake_risk, fake_explain, None, None, {}

        try:
            db = _make_db(str(tmp))
            web_api.add_device(db, "192.168.1.25", "Front Door", "camera", "Tapo", open_ports=[554])
            route = SimpleNamespace(
                degraded=False,
                candidate_models=["gismo:latest"],
                capability=SimpleNamespace(planner_timeout_s=75),
            )
            with mock.patch.object(web_api, "resolve_model_route", return_value=route), mock.patch(
                "gismo.cli.main._request_llm_plan", side_effect=_fake_request
            ):
                web_api._request_chat_plan(db, "check the cameras")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        planner_text = str(captured["user_text"])
        self.assertIn("Saved devices:", planner_text)
        self.assertIn("Front Door", planner_text)
        self.assertIn("device: check cameras", planner_text)


class TestDevicesAndSettings(unittest.TestCase):
    def test_scan_devices_uses_isolated_runtime_results(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch(
                "gismo.web.api.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {"ip": "192.168.1.2", "hostname": "desk-lamp", "device_type": "light"},
                    ],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                data = web_api.scan_devices(db, timeout_seconds=0.05)
            runtime.assert_called_once()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["ip"], "192.168.1.2")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_device_roundtrip_and_stream_fallback(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
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
                added = web_api.add_device(
                    db,
                    "192.168.1.25",
                    "Front Door",
                    "camera",
                    "Tapo",
                    rtsp_url="rtsp://192.168.1.25:554/stream1",
                    open_ports=[554],
                )
                listed = web_api.list_devices(db)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["ip"], "192.168.1.25")
            self.assertIn("check", listed[0]["actions"])
            self.assertIn("view", listed[0]["actions"])
            self.assertFalse(listed[0]["needs_setup"])

            with mock.patch("gismo.web.api.shutil.which", return_value=None):
                payload = web_api.get_device_stream_payload(db, added["id"])
            self.assertEqual(payload["kind"], "snapshot")
            self.assertIn("content_type", payload)
            self.assertIn("body", payload)

            removed = web_api.remove_device(db, added["id"])
            self.assertTrue(removed["ok"])
            self.assertEqual(web_api.list_devices(db), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_settings_roundtrip(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            fake_models = {
                "installed_models": ["gismo:latest", "tinyllama"],
                "loaded_models": ["gismo:latest"],
                "policy": {
                    "primary_assistant_model": "gismo:latest",
                    "planner_model": "gismo:latest",
                    "helper_model": "",
                    "allow_identity_fallback": False,
                    "performance_mode": "auto",
                },
                "assistant_route": {},
                "planner_route": {},
                "degraded_mode": {"active": False, "reason": None},
                "issues": [],
                "runtime_failures": {},
                "ollama_available": True,
            }
            with mock.patch("gismo.llm.model_policy.discover_models", return_value={"installed_models": ["gismo:latest", "tinyllama"], "loaded_models": ["gismo:latest"], "ollama_available": True}), mock.patch.object(
                web_api,
                "get_model_policy_health",
                return_value=fake_models,
            ):
                current = web_api.get_settings(db)
                self.assertIn("voices", current)
                self.assertIn("model", current)
                self.assertIn("models", current)
                self.assertIn("model_policy", current)
                voice_id = current["voices"][0]["id"]

                updated = web_api.save_settings(
                    db,
                    operator_name="Mike",
                    voice_id=voice_id,
                    primary_assistant_model="gismo:latest",
                    planner_model="tinyllama",
                    helper_model="tinyllama",
                    allow_identity_fallback=True,
                    performance_mode="balanced",
                )
            self.assertEqual(updated["operator_name"], "Mike")
            self.assertEqual(updated["voice"], voice_id)
            self.assertEqual(updated["model"], "gismo:latest")
            self.assertEqual(updated["model_policy"]["planner_model"], "tinyllama")
            self.assertEqual(updated["model_policy"]["helper_model"], "tinyllama")
            self.assertTrue(updated["model_policy"]["allow_identity_fallback"])
            self.assertEqual(updated["model_policy"]["performance_mode"], "balanced")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_light_device_flags_setup_need(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch(
                "gismo.web.api.execute_device_runtime_action",
                side_effect=[
                    {
                        "devices": [{"id": "device-1", "status": "offline"}],
                        "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                    },
                    {
                        "devices": [{"id": "device-1", "status": "offline"}],
                        "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                    },
                ],
            ):
                web_api.add_device(
                    db,
                    "192.168.1.40",
                    "Kitchen Lamp",
                    "light",
                    "FEIT",
                    open_ports=[6668],
                )
                listed = web_api.list_devices(db)
            self.assertEqual(len(listed), 1)
            self.assertIn("turn_on", listed[0]["actions"])
            self.assertTrue(listed[0]["needs_setup"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_saved_actuators_list_exposes_sanitized_configured_controls(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            _write_devices_config(
                tmp,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-LOCAL-KEY",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            with StateStore(db) as store:
                store.record_event(
                    actor="worker",
                    event_type="device_light_command",
                    message="I set Dad's Room Light to cool white.",
                    json_payload={
                        "target": "dads-room-light",
                        "command": "set_color_temp",
                        "params": {"preset": "cool_white"},
                        "changed": 1,
                        "failed": 0,
                        "confirmed": 1,
                        "verified_states": {
                            "dads-room-light": {"color_temp_preset": "cool_white"},
                        },
                    },
                )
            with mock.patch(
                "gismo.web.api.execute_device_runtime_action",
                return_value={
                    "devices": [{"id": "dads-room-light", "status": "online"}],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ):
                listed = web_api.list_saved_actuators(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(listed), 1)
        item = listed[0]
        self.assertEqual(item["device_ref"], "dads-room-light")
        self.assertEqual(item["name"], "Dad's Room Light")
        self.assertEqual(item["platform"], "tuya")
        self.assertEqual(item["status"], "online")
        self.assertEqual(item["reachability"], "reachable")
        self.assertIn("brightness", item["capabilities"])
        self.assertNotIn("local_key", item)
        self.assertEqual(item["last_result"]["summary"], "I set Dad's Room Light to cool white.")
        self.assertEqual(item["current_state"]["white_mode"], "cool_white")
        self.assertIn("cool white", item["current_state"]["summary"].lower())

    def test_dashboard_control_enqueues_saved_actuator_command_without_connected_device(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            _write_devices_config(
                tmp,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-LOCAL-KEY",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            data = web_api.control_actuator(
                db,
                {"device_ref": "dads-room-light", "action": "turn_off", "params": {}},
            )
            with StateStore(db) as store:
                items = [
                    item
                    for item in store.list_queue_items(limit=20)
                    if item.metadata_json.get("device_ref") == "dads-room-light"
                ]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertEqual(data["device_ref"], "dads-room-light")
        self.assertTrue(data["structured"])
        self.assertTrue(data["command_text"].startswith("control: "))
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].command_text.startswith("control: "))
        self.assertEqual(items[0].metadata_json["device_ref"], "dads-room-light")
        self.assertEqual(
            items[0].metadata_json["operator_plan"]["steps"][0]["input_json"]["target"],
            "dads-room-light",
        )
        self.assertEqual(
            items[0].metadata_json["structured_command"]["actions"][0]["action"],
            "turn_off",
        )

    def test_saved_actuators_list_reports_pending_structured_command(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            _write_devices_config(
                tmp,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-LOCAL-KEY",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            with StateStore(db) as store:
                store.enqueue_command(
                    "lights: Dad's Room Light / turn_off",
                    metadata={
                        "device_ref": "dads-room-light",
                        "structured_command": {
                            "device_ref": "dads-room-light",
                            "label": "lights: Dad's Room Light / turn_off",
                            "actions": [{"action": "turn_off", "target": "dads-room-light", "params": {}}],
                        },
                    },
                )
            with mock.patch(
                "gismo.web.api.execute_device_runtime_action",
                return_value={
                    "devices": [{"id": "dads-room-light", "status": "online"}],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ):
                listed = web_api.list_saved_actuators(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(listed[0]["current_command"]["state"], "pending")
        self.assertIn("turn_off", listed[0]["current_command"]["summary"])

    def test_chat_light_request_uses_saved_light_ref_in_structured_metadata(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            _write_devices_config(
                tmp,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "alias": "Dad's light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-LOCAL-KEY",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            with mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "turn Dad's light off", [])
            with StateStore(db) as store:
                items = [
                    item
                    for item in store.list_queue_items(limit=20)
                    if item.metadata_json.get("device_ref") == "dads-room-light"
                ]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "execution")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].metadata_json["device_ref"], "dads-room-light")
        self.assertEqual(
            items[0].metadata_json["operator_plan"]["steps"][0]["input_json"]["target"],
            "dads-room-light",
        )
        self.assertEqual(items[0].command_text, "device: turn Dad's light off")

    def test_activity_feed_includes_device_events(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with StateStore(db) as store:
                store.record_event(
                    actor="worker",
                    event_type="device_check",
                    message="I checked Front Door. Offline: Front Door.",
                    json_payload={"target": "front door"},
                )
            feed = web_api.get_activity_feed(db)
            self.assertTrue(any(item["type"] == "device" for item in feed))
            self.assertTrue(any("Front Door" in item["label"] for item in feed))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCalendar(unittest.TestCase):
    def test_calendar_crud_roundtrip(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            created = web_api.create_calendar_event(
                db,
                {
                    "title": "Dinner",
                    "description": "With family",
                    "event_type": "personal",
                    "status": "scheduled",
                    "start_at": "2026-03-20T18:00:00",
                    "end_at": "2026-03-20T19:30:00",
                    "all_day": False,
                    "source": "local",
                    "requires_ack": True,
                    "metadata_json": {"room": "kitchen"},
                },
            )
            listed = web_api.list_calendar_events(db, day="2026-03-20")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["title"], "Dinner")

            updated = web_api.update_calendar_event(
                db,
                created["id"],
                {"title": "Family dinner", "status": "done"},
            )
            self.assertEqual(updated["title"], "Family dinner")
            self.assertEqual(updated["status"], "done")

            removed = web_api.delete_calendar_event(db, created["id"])
            self.assertTrue(removed["ok"])
            self.assertEqual(web_api.list_calendar_events(db, day="2026-03-20"), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_chat_can_add_calendar_event(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "remind me tomorrow at 3", [])
            with StateStore(db) as store:
                pending = store.list_pending_plans()
            events = web_api.list_calendar_events(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "plan")
        self.assertEqual(data["classification"], "operational_request")
        self.assertEqual(len(pending), 1)
        self.assertEqual(events, [])

    def test_chat_can_plan_calendar_delete_range(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            with mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "delete calendar events from March 27 through April 1", [])
            with StateStore(db) as store:
                pending = store.list_pending_plans()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "plan")
        self.assertEqual(data["classification"], "operational_request")
        self.assertEqual(len(pending), 1)
        self.assertIn("calendar: delete_range", pending[0].plan_json["actions"][0]["command"])

    def test_chat_can_read_calendar_today(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            today = datetime.now().date().isoformat()
            web_api.create_calendar_event(
                db,
                {
                    "title": "Check in",
                    "start_at": today + "T09:00:00",
                    "end_at": today + "T10:00:00",
                },
            )
            with mock.patch.object(web_api, "_append_chat_record", return_value=None):
                data = web_api.chat_message(db, "what is on my calendar today", [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(data["mode"], "reply")
        self.assertEqual(data["classification"], "deterministic_query")
        self.assertIn("Check in", data["reply"])


class TestSavedActuatorIdentityAndCapabilities(unittest.TestCase):
    def _workspace(self) -> tuple[Path, str]:
        tmp = Path("tmp") / f"web-api-identity-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        return tmp, _make_db(str(tmp))

    def test_exact_device_ref_survives_stale_ip(self) -> None:
        tmp, db = self._workspace()
        try:
            _write_devices_config(tmp, [{
                "gismo_device_id": "desk-light",
                "name": "Desk Light",
                "alias": "office lamp",
                "device_id": "vendor-123",
                "ip": "192.168.1.250",
                "platform": "tuya",
                "device_type": "light",
            }])
            result = web_api.control_actuator(
                db,
                {"device_ref": "desk-light", "action": "turn_off", "params": {}},
            )
            self.assertEqual(result["device_ref"], "desk-light")
            self.assertNotIn("192.168.1.250", result["command_text"])
            self.assertNotIn("vendor-123", result["command_text"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exact_unique_alias_resolves_to_canonical_ref(self) -> None:
        tmp, db = self._workspace()
        try:
            _write_devices_config(tmp, [{
                "gismo_device_id": "desk-light",
                "name": "Desk Light",
                "alias": "office lamp",
                "platform": "tuya",
                "device_type": "light",
            }])
            result = web_api.control_actuator(
                db,
                {"device_ref": "office lamp", "action": "turn_on", "params": {}},
            )
            self.assertEqual(result["device_ref"], "desk-light")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ambiguous_alias_requires_explicit_device_ref(self) -> None:
        tmp, db = self._workspace()
        try:
            _write_devices_config(tmp, [
                {"gismo_device_id": "desk-left", "name": "Left", "alias": "desk lamp", "device_type": "light"},
                {"gismo_device_id": "desk-right", "name": "Right", "alias": "desk lamp", "device_type": "light"},
            ])
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                web_api.control_actuator(
                    db,
                    {"device_ref": "desk lamp", "action": "turn_on", "params": {}},
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_similarly_named_and_nonexistent_targets_do_not_fuzzy_match(self) -> None:
        tmp, db = self._workspace()
        try:
            _write_devices_config(tmp, [
                {"gismo_device_id": "bed-left", "name": "Bed Light Left", "device_type": "light"},
                {"gismo_device_id": "bed-right", "name": "Bed Light Right", "device_type": "light"},
            ])
            for target in ("bed light", "garage light"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "not found"):
                    web_api.control_actuator(
                        db,
                        {"device_ref": target, "action": "turn_on", "params": {}},
                    )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_capabilities_are_explicit_and_unsupported_action_is_rejected(self) -> None:
        tmp, db = self._workspace()
        try:
            _write_devices_config(tmp, [
                {
                    "gismo_device_id": "coffee-plug",
                    "name": "Coffee Plug",
                    "device_type": "switch",
                    "actions": ["turn_on", "turn_off"],
                },
                {
                    "gismo_device_id": "status-sensor",
                    "name": "Status Sensor",
                    "device_type": "sensor",
                    "actions": [],
                },
            ])
            with mock.patch.object(web_api, "_probe_saved_actuator_statuses", return_value=(False, {})):
                listed = {item["device_ref"]: item for item in web_api.list_saved_actuators(db)}
            self.assertEqual(listed["coffee-plug"]["kind"], "switch")
            self.assertEqual(listed["coffee-plug"]["actions"], ["turn_on", "turn_off"])
            self.assertNotIn("brightness", listed["coffee-plug"]["capabilities"])
            self.assertEqual(listed["status-sensor"]["actions"], [])
            self.assertEqual(listed["status-sensor"]["current_state"]["power"], "unknown")
            with self.assertRaisesRegex(ValueError, "Unsupported actuator action"):
                web_api.control_actuator(
                    db,
                    {"device_ref": "coffee-plug", "action": "set_brightness", "params": {"brightness": 50}},
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
