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
            return_value={"cpu_percent": 7.0, "virtual_memory": 55.0},
        ):
            data = self._request_json("/api/health")
        self.assertEqual(data, {"cpu_percent": 7.0, "virtual_memory": 55.0})

    def test_status_endpoint_shape(self) -> None:
        data = self._request_json("/api/status")
        self.assertIn("daemon", data)
        self.assertIn("queue", data)

    def test_onboarding_endpoint_shape(self) -> None:
        data = self._request_json("/api/onboarding")
        self.assertIn("needs_onboarding", data)
        self.assertIn("operator_name", data)

    def test_chat_endpoint_returns_reply(self) -> None:
        with mock.patch.object(web_api, "chat_message", return_value={"reply": "hello"}) as chat_mock:
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
        self.assertEqual(data, {"reply": "hello"})


if __name__ == "__main__":
    unittest.main()
