from __future__ import annotations

import http.cookiejar
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.state import StateStore
from gismo.memory.store import put_item
from gismo.web import control_security
from gismo.web import api as web_api
from gismo.web.server import _make_handler


def _make_db(tmp: Path) -> str:
    db_path = str(tmp / "state.db")
    with StateStore(db_path):
        pass
    return db_path


class WebControlSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("tmp") / f"web-control-{uuid4().hex}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.db_path = _make_db(self.tmp)
        self.server = None
        self.thread = None
        self.base_url = ""
        self._start_server()

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_server(self, *, bind_host: str = "127.0.0.1") -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(self.db_path, bind_host=bind_host))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def _build_opener(self) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        return opener, jar

    def _cookie_value(self, jar: http.cookiejar.CookieJar, name: str) -> str:
        return next(cookie.value for cookie in jar if cookie.name == name)

    def _cookie_control_headers(self, jar: http.cookiejar.CookieJar) -> dict[str, str]:
        return {
            "Origin": self.base_url,
            "X-GISMO-CSRF-Token": self._cookie_value(jar, "gismo_control_csrf"),
        }

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        client = opener or urllib.request.build_opener()
        with client.open(request, timeout=5) as response:
            return response.status, dict(response.headers.items()), response.read()

    def _security_events(self, event_type: str) -> list:
        with StateStore(self.db_path) as store:
            return store.list_security_events(limit=20, event_type=event_type)

    def test_read_endpoint_remains_open_without_auth(self) -> None:
        status, _, body = self._request("/api/actuators/list")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), [])

    def test_remote_clients_are_not_bootstrapped_with_control_cookies(self) -> None:
        context = control_security.build_context(self.db_path, bind_host="0.0.0.0")
        self.assertEqual(
            control_security.response_headers(context, client_ip="192.168.1.44"),
            [],
        )
        local_headers = control_security.response_headers(context, client_ip="127.0.0.1")
        self.assertTrue(any(name == "Set-Cookie" for name, _ in local_headers))

    def test_loopback_control_rejects_missing_token(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertEqual(payload["error"], "Actuator control is not authorized.")
        events = self._security_events("web_control_unauthorized")
        self.assertEqual(events[0].payload["reason"], "invalid_token")

    def test_loopback_control_rejects_wrong_token_without_exposing_secrets(self) -> None:
        context = control_security.build_context(self.db_path)
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="127.0.0.1",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": "wrong"},
            body_bytes=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestRejection)
        assert isinstance(result, control_security.ControlRequestRejection)
        self.assertEqual(result.status, 401)
        self.assertNotIn(context.token, result.message)
        self.assertNotIn(context.csrf_token, result.message)

    def test_loopback_control_accepts_bootstrapped_cookie_with_csrf_proof(self) -> None:
        opener, jar = self._build_opener()
        self._request("/", opener=opener)
        self.assertTrue(any(cookie.name == "gismo_control_token" for cookie in jar))
        token_path = control_security.control_token_path(self.db_path)
        self.assertTrue(token_path.exists())
        self.assertIn(".gismo", str(token_path))
        with mock.patch.object(
            web_api,
            "control_actuator",
            return_value={"ok": True, "mode": "execution"},
        ) as control_mock:
            status, _, body = self._request(
                "/api/actuators/control",
                method="POST",
                payload={"device_ref": "desk-lamp", "action": "turn_on", "params": {}},
                headers=self._cookie_control_headers(jar),
                opener=opener,
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["mode"], "execution")
        control_mock.assert_called_once()
        events = self._security_events("web_control_request_accepted")
        self.assertEqual(events[0].payload["result"], "accepted")
        self.assertNotIn(control_security.build_context(self.db_path).token, json.dumps(events[0].payload))

    def test_cookie_auth_rejects_cross_origin_request(self) -> None:
        opener, jar = self._build_opener()
        self._request("/", opener=opener)
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "turn_off", "params": {}}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
                "X-GISMO-CSRF-Token": self._cookie_value(jar, "gismo_control_csrf"),
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertIn("same-origin", payload["error"].lower())
        events = self._security_events("web_control_invalid_csrf")
        self.assertEqual(events[0].payload["reason"], "origin_mismatch")

    def test_cookie_auth_rejects_missing_csrf_proof(self) -> None:
        opener, _ = self._build_opener()
        self._request("/", opener=opener)
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base_url},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertIn("csrf", payload["error"].lower())

    def test_cookie_auth_rejects_wrong_csrf_proof(self) -> None:
        opener, _ = self._build_opener()
        self._request("/", opener=opener)
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-GISMO-CSRF-Token": "wrong",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_control_rejects_non_json_content_type(self) -> None:
        opener, _ = self._build_opener()
        self._request("/", opener=opener)
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=b'{"device_ref":"desk-lamp"}',
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 415)

    def test_control_rejects_malformed_json(self) -> None:
        context = control_security.build_context(self.db_path)
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="127.0.0.1",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=b'{"device_ref":',
        )
        self.assertIsInstance(result, control_security.ControlRequestRejection)
        assert isinstance(result, control_security.ControlRequestRejection)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.reason, "invalid_json")

    def test_authenticated_request_rejects_unsupported_action(self) -> None:
        context = control_security.build_context(self.db_path)
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "self_destruct", "params": {}}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-GISMO-Control-Token": context.token,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertEqual(payload["error"], "Unsupported actuator action.")
        self.assertNotIn(context.token, json.dumps(payload))

    def test_control_rejects_large_body(self) -> None:
        opener, _ = self._build_opener()
        self._request("/", opener=opener)
        large_params = {"payload": "x" * 5000}
        request = urllib.request.Request(
            f"{self.base_url}/api/actuators/control",
            data=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": large_params}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 413)

    def test_control_rejects_excessive_params(self) -> None:
        context = control_security.build_context(self.db_path)
        body = {
            "device_ref": "desk-lamp",
            "action": "turn_on",
            "params": {f"k{i}": i for i in range(20)},
        }
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="127.0.0.1",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=json.dumps(body).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestRejection)
        assert isinstance(result, control_security.ControlRequestRejection)
        self.assertEqual(result.status, 400)
        self.assertIn("too many", result.message)

    def test_remote_client_is_rejected_without_secure_mode(self) -> None:
        context = control_security.build_context(self.db_path, bind_host="0.0.0.0")
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="192.168.1.44",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestRejection)
        assert isinstance(result, control_security.ControlRequestRejection)
        self.assertEqual(result.status, 403)
        self.assertEqual(result.event_type, "web_control_rejected_exposure")

    def test_non_loopback_binding_rejects_loopback_client_without_secure_mode(self) -> None:
        context = control_security.build_context(self.db_path, bind_host="0.0.0.0")
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="127.0.0.1",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestRejection)
        assert isinstance(result, control_security.ControlRequestRejection)
        self.assertEqual(result.event_type, "web_control_rejected_exposure")

    def test_batched_commands_are_bounded_and_accepted(self) -> None:
        context = control_security.build_context(self.db_path)
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="127.0.0.1",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=json.dumps({
                "device_ref": "desk-lamp",
                "commands": [
                    {"action": "turn_on", "params": {}},
                    {"action": "set_brightness", "params": {"brightness": 50}},
                ],
            }).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestApproval)

    def test_remote_secure_mode_accepts_header_token(self) -> None:
        put_item(
            self.db_path,
            namespace="gismo:settings",
            key="web.remote_control_enabled",
            kind="preference",
            value=True,
            tags=["web", "security"],
            confidence="high",
            source="operator",
            ttl_seconds=None,
            actor="test",
            policy_hash="test",
        )
        context = control_security.build_context(self.db_path, bind_host="0.0.0.0")
        result = control_security.authorize_control_request(
            context=context,
            method="POST",
            path="/api/actuators/control",
            client_ip="192.168.1.44",
            headers={"Content-Type": "application/json", "X-GISMO-Control-Token": context.token},
            body_bytes=json.dumps({"device_ref": "desk-lamp", "action": "turn_on", "params": {}}).encode("utf-8"),
        )
        self.assertIsInstance(result, control_security.ControlRequestApproval)


if __name__ == "__main__":
    unittest.main()
