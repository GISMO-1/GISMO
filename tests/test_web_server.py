from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.models import TaskStatus
from gismo.core.state import StateStore
from gismo.web import api as web_api
from gismo.web.server import _make_handler
from http.server import HTTPServer


def _make_db(tmp: str) -> str:
    db_path = str(Path(tmp) / "state.db")
    with StateStore(db_path) as store:
        run = store.create_run(label="web-test")
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


class TestWebServerEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("tmp") / f"web-server-{uuid4().hex}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.db = _make_db(str(self.tmp))
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(self.db))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request_json(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_queue_stats_endpoint(self) -> None:
        data = self._request_json("/api/queue/stats")
        self.assertIn("total", data)
        self.assertIn("by_status", data)
        self.assertEqual(data["by_status"]["QUEUED"], 1)

    def test_health_endpoint(self) -> None:
        with mock.patch.object(
            web_api,
            "get_system_health",
            return_value={
                "cpu_percent": 7.0,
                "virtual_memory": 55.0,
                "internet_connected": True,
                "internet_latency_ms": 24,
            },
        ):
            data = self._request_json("/api/health")
        self.assertEqual(data["cpu_percent"], 7.0)
        self.assertEqual(data["virtual_memory"], 55.0)
        self.assertTrue(data["internet_connected"])
        self.assertEqual(data["internet_latency_ms"], 24)

    def test_status_endpoint_shape(self) -> None:
        data = self._request_json("/api/status")
        self.assertIn("daemon", data)
        self.assertIn("queue", data)

    def test_onboarding_endpoint_shape(self) -> None:
        data = self._request_json("/api/onboarding")
        self.assertIn("needs_onboarding", data)
        self.assertIn("operator_name", data)

    def test_settings_endpoint_shape(self) -> None:
        data = self._request_json("/api/settings")
        self.assertIn("operator_name", data)
        self.assertIn("voice", data)
        self.assertIn("voices", data)

    def test_devices_add_list_remove(self) -> None:
        added = self._request_json(
            "/api/devices/add",
            method="POST",
            payload={
                "ip": "192.168.1.55",
                "hostname": "Desk Lamp",
                "device_type": "light",
                "brand": "Tuya",
                "open_ports": [6668],
            },
        )
        listed = self._request_json("/api/devices/list")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], added["id"])

        removed = self._request_json(
            "/api/devices/remove",
            method="POST",
            payload={"id": added["id"]},
        )
        self.assertTrue(removed["ok"])

    def test_devices_scan_endpoint(self) -> None:
        with mock.patch.object(
            web_api,
            "scan_devices",
            return_value=[{"ip": "192.168.1.20", "hostname": "front-door", "device_type": "camera", "brand": "Tapo"}],
        ):
            data = self._request_json("/api/devices/scan")
        self.assertEqual(data[0]["brand"], "Tapo")

    def test_calendar_crud_endpoints(self) -> None:
        created = self._request_json(
            "/api/calendar",
            method="POST",
            payload={
                "title": "Dinner",
                "start_at": "2026-03-20T18:00:00",
                "end_at": "2026-03-20T19:00:00",
            },
        )
        listed = self._request_json("/api/calendar?day=2026-03-20")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], created["id"])

        updated = self._request_json(
            f"/api/calendar/{created['id']}",
            method="PATCH",
            payload={"title": "Late Dinner"},
        )
        self.assertEqual(updated["title"], "Late Dinner")

        removed = self._request_json(
            f"/api/calendar/{created['id']}",
            method="DELETE",
        )
        self.assertTrue(removed["ok"])

    def test_chat_endpoint_returns_reply(self) -> None:
        with mock.patch.object(
            web_api,
            "chat_message",
            return_value={"reply": "hello", "mode": "reply", "classification": "informational"},
        ) as chat_mock:
            data = self._request_json(
                "/api/chat",
                method="POST",
                payload={"message": "hi", "history": [{"role": "user", "content": "earlier"}]},
            )
        chat_mock.assert_called_once_with(
            self.db,
            "hi",
            [{"role": "user", "content": "earlier"}],
        )
        self.assertEqual(data["reply"], "hello")

    def test_tts_preview_endpoint(self) -> None:
        with mock.patch.object(web_api, "tts_preview", return_value=b"wav") as preview_mock:
            request = urllib.request.Request(
                f"{self.base_url}/api/tts/preview",
                data=json.dumps({"voice": "af_bella"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type")
        preview_mock.assert_called_once_with(self.db, "af_bella")
        self.assertEqual(body, b"wav")
        self.assertEqual(content_type, "audio/wav")


if __name__ == "__main__":
    unittest.main()
