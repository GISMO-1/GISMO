from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from uuid import uuid4

from gismo.core.state import StateStore
from gismo.web.server import _make_handler


class SecurityWebServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("tmp") / f"security-web-{uuid4().hex}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.db_path = str(self.tmp / "state.db")
        with StateStore(self.db_path) as store:
            self.event = store.record_security_event(
                event_type="outbound_denied",
                actor="operator",
                action="connect",
                resource="network:public",
                payload={"destination": "8.8.8.8"},
                related_run_id="run-1",
            )
            self.promote_record = store.create_quarantine_entry(
                source_kind="web_import",
                source_ref="https://example.test/one",
                origin_type="external",
                content={"value": "one"},
                content_sha256="hash-1",
                actor="test",
                trust_labels=["external"],
                verification_status="unverified",
            )
            self.reject_record = store.create_quarantine_entry(
                source_kind="web_import",
                source_ref="https://example.test/two",
                origin_type="external",
                content={"value": "two"},
                content_sha256="hash-2",
                actor="test",
                trust_labels=["external"],
                verification_status="unverified",
            )
            self.execution_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            store.record_security_event(
                event_type="execution_mode_selected",
                actor="worker",
                action="device_status",
                resource="tool:device_control",
                payload={
                    "execution_id": self.execution_id,
                    "component": "device_control",
                    "zone": "device_adapter",
                    "mode": "sandboxed",
                    "action": "device_status",
                    "resource": "tool:device_control",
                },
                related_run_id="run-exec",
            )
            store.record_security_event(
                event_type="isolated_execution_finished",
                actor="worker",
                action="device_status",
                resource="tool:device_control",
                payload={
                    "execution_id": self.execution_id,
                    "component": "device_control",
                    "zone": "device_adapter",
                    "mode": "sandboxed",
                    "action": "device_status",
                    "resource": "tool:device_control",
                    "result": "succeeded",
                },
                related_run_id="run-exec",
            )
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(self.db_path))
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

    def test_security_event_endpoints(self) -> None:
        events = self._request_json("/api/security/events?type=outbound_denied&run=run-1&event=" + self.event.id)
        self.assertEqual(len(events["events"]), 1)
        self.assertEqual(events["selected_event"]["id"], self.event.id)

        verify = self._request_json("/api/security/verify")
        self.assertTrue(verify["valid"])
        self.assertGreaterEqual(verify["checked"], 1)

    def test_security_execution_endpoints(self) -> None:
        executions = self._request_json("/api/security/execution?mode=sandboxed&zone=device_adapter&run=run-exec")
        self.assertEqual(len(executions["executions"]), 1)
        self.assertEqual(executions["executions"][0]["execution_id"], self.execution_id)

        detail = self._request_json(f"/api/security/execution/{self.execution_id[:12]}")
        self.assertEqual(detail["execution"]["execution_id"], self.execution_id)
        self.assertEqual(len(detail["events"]), 2)

    def test_quarantine_endpoints(self) -> None:
        listed = self._request_json("/api/quarantine")
        ids = {item["id"] for item in listed}
        self.assertIn(self.promote_record.id, ids)
        self.assertIn(self.reject_record.id, ids)

        detail = self._request_json(f"/api/quarantine/{self.promote_record.id}")
        self.assertEqual(detail["id"], self.promote_record.id)
        self.assertTrue(detail["content_present"])

    def test_quarantine_promote_and_reject_endpoints(self) -> None:
        promoted = self._request_json(
            f"/api/quarantine/{self.promote_record.id}/promote",
            method="POST",
            payload={
                "labels": ["external", "verified", "trusted"],
                "reason": "Reviewed",
                "namespace": "global",
                "key": "reviewed-one",
            },
        )
        self.assertEqual(promoted["quarantine"]["status"], "promoted")
        self.assertEqual(promoted["memory_item"]["key"], "reviewed-one")

        rejected = self._request_json(
            f"/api/quarantine/{self.reject_record.id}/reject",
            method="POST",
            payload={"reason": "Rejected"},
        )
        self.assertEqual(rejected["status"], "rejected")

    def test_quarantine_promote_endpoint_returns_clear_errors(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/quarantine/{self.promote_record.id}/promote",
            data=json.dumps({"reason": "Missing labels"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertIn("trust labels", payload["error"].lower())
