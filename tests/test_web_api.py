"""Tests for gismo.web.api — pure data layer."""
from __future__ import annotations

import ipaddress
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from gismo.core.models import TaskStatus, QueueStatus
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
            input_json={"cmd": "echo hello"},
        )
        task.status = TaskStatus.SUCCEEDED
        store.update_task(task)
        store.enqueue_command("echo world")
    return db_path


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
            self.assertEqual(data["daemon"]["state"], "ready")
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
        self.assertTrue(data["internet_connected"])
        self.assertIn("internet_latency_ms", data)


class TestDevicesAndSettings(unittest.TestCase):
    def test_scan_devices_times_out_and_returns_partial_results(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
            started = time.monotonic()
            with mock.patch("gismo.web.api._list_device_models", return_value=[]), mock.patch(
                "gismo.web.api._local_networks",
                return_value=[ipaddress.ip_network("192.168.1.0/30")],
            ), mock.patch(
                "gismo.web.api._local_ipv4_addresses",
                return_value=["192.168.1.1"],
            ), mock.patch(
                "gismo.web.api._read_arp_table",
                return_value=["192.168.1.2"],
            ), mock.patch(
                "gismo.web.api._safe_hostname",
                return_value="desk-lamp",
            ), mock.patch(
                "gismo.web.api._ping_host",
                side_effect=lambda *_args, **_kwargs: (time.sleep(0.2), True)[1],
            ):
                data = web_api.scan_devices(db, timeout_seconds=0.05)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["ip"], "192.168.1.2")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_device_roundtrip_and_stream_fallback(self) -> None:
        tmp = Path("tmp") / f"web-api-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(str(tmp))
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
            current = web_api.get_settings(db)
            self.assertIn("voices", current)
            voice_id = current["voices"][0]["id"]

            updated = web_api.save_settings(
                db,
                operator_name="Mike",
                voice_id=voice_id,
            )
            self.assertEqual(updated["operator_name"], "Mike")
            self.assertEqual(updated["voice"], voice_id)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
