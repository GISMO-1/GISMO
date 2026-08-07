import json
import logging
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from gismo.core import background_worker
from gismo.core import device_runtime
from gismo.core.models import ConnectedDevice
from gismo.core.paths import normalize_database_path
from gismo.core.state import StateStore
from gismo.web import api as web_api
from gismo.web.server import _make_handler
from gismo.web.templates import HTML


class _RunningServer:
    def __init__(self, db_path: str) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(db_path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
    ) -> tuple[int, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        try:
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            response.close()


class TestWebStateIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("tmp") / f"web-state-isolation-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _db(self, name: str) -> str:
        path = str(self.root / name)
        with StateStore(path):
            pass
        return path

    def _identity(self, db_path: str) -> dict:
        with StateStore(db_path) as store:
            return store.get_runtime_identity()

    def test_blank_database_has_zero_conversations_and_blocks_legacy_fallback(self) -> None:
        db_path = self._db("blank.db")
        legacy_path = self.root / "legacy-chat.jsonl"
        legacy_path.write_text('{"user":"LEGACY-SENTINEL"}\n', encoding="utf-8")

        with mock.patch.object(web_api, "_LEGACY_CHAT_HISTORY_FILE", legacy_path):
            history = web_api.get_chat_history(db_path)
            activity = web_api.get_activity_feed(db_path)

        self.assertEqual(history["messages"], [])
        self.assertFalse(any(item.get("type") == "chat" for item in activity))
        with StateStore(db_path) as store:
            self.assertEqual(store.list_chat_exchanges(), [])
            events = store.list_events_by_type("database_scope_fallback_blocked")
        self.assertEqual(len(events), 1)
        audit_json = json.dumps(events[0].json_payload, sort_keys=True)
        self.assertNotIn("LEGACY-SENTINEL", audit_json)
        self.assertIn("fallback_fingerprint", events[0].json_payload)

    def test_two_databases_and_two_servers_in_one_process_are_isolated(self) -> None:
        db_a = self._db("a.db")
        db_b = self._db("b.db")
        sentinel_a = "ISO-A-2a31"
        sentinel_b = "ISO-B-94f0"
        with StateStore(db_a) as store:
            store.append_chat_exchange(sentinel_a, "reply-a")
        with StateStore(db_b) as store:
            store.append_chat_exchange(sentinel_b, "reply-b")

        server_a = _RunningServer(db_a)
        server_b = _RunningServer(db_b)
        try:
            status_a, history_a = server_a.request("/api/chat/history")
            status_b, history_b = server_b.request("/api/chat/history")
        finally:
            server_a.close()
            server_b.close()

        self.assertEqual((status_a, status_b), (200, 200))
        payload_a = json.dumps(history_a)
        payload_b = json.dumps(history_b)
        self.assertIn(sentinel_a, payload_a)
        self.assertNotIn(sentinel_b, payload_a)
        self.assertIn(sentinel_b, payload_b)
        self.assertNotIn(sentinel_a, payload_b)
        self.assertNotEqual(history_a["instance_id"], history_b["instance_id"])

    def test_restart_keeps_its_database_and_does_not_import_another(self) -> None:
        db_a = self._db("restart-a.db")
        db_b = self._db("restart-b.db")
        with StateStore(db_a) as store:
            store.append_chat_exchange("RESTART-A", "reply-a")
        with StateStore(db_b) as store:
            store.append_chat_exchange("RESTART-B", "reply-b")

        first = _RunningServer(db_a)
        try:
            _, before = first.request("/api/chat/history")
        finally:
            first.close()
        restarted = _RunningServer(db_a)
        try:
            _, after = restarted.request("/api/chat/history")
        finally:
            restarted.close()

        self.assertEqual(before["instance_id"], after["instance_id"])
        after_json = json.dumps(after)
        self.assertIn("RESTART-A", after_json)
        self.assertNotIn("RESTART-B", after_json)

    def test_background_worker_preserves_normalized_database_path(self) -> None:
        db_path = str(self.root / "worker.db")
        stopped = background_worker.BackgroundWorkerStatus(
            running=False,
            stale=False,
            paused=False,
            pid=None,
            started_at=None,
            last_seen=None,
            age_seconds=None,
        )
        fake_process = SimpleNamespace(pid=1234)
        with mock.patch.object(background_worker, "_worker_is_healthy", return_value=False), mock.patch.object(
            background_worker, "get_background_worker_status", return_value=stopped
        ), mock.patch.object(
            background_worker.subprocess, "Popen", return_value=fake_process
        ) as popen_mock, mock.patch.object(
            background_worker, "_record_autostart_event"
        ):
            background_worker.ensure_background_worker_status(db_path, source="isolation-test")

        argv = popen_mock.call_args.args[0]
        self.assertEqual(argv[argv.index("--db") + 1], normalize_database_path(db_path))

    def test_device_worker_uses_only_its_explicit_config_path(self) -> None:
        config_path = self.root / "scoped-devices.json"
        config_path.write_text(
            json.dumps(
                {
                    "devices": [
                        {
                            "gismo_device_id": "scoped-light",
                            "device_id": "artificial-device-id",
                            "local_key": "artificial-local-key",
                            "ip": "192.0.2.44",
                            "platform": "tuya",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        device = ConnectedDevice(
            id="scoped-light",
            ip="192.0.2.44",
            hostname="Scoped Light",
            device_type="light",
            brand="FEIT",
        )

        with mock.patch.dict(
            "os.environ",
            {"GISMO_DEVICES_CONFIG": str(config_path.resolve())},
            clear=False,
        ):
            resolved = device_runtime._resolve_configured_device(device)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["gismo_device_id"], "scoped-light")

    def test_missing_server_database_scope_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit database path"):
            _make_handler(None)  # type: ignore[arg-type]

    def test_client_history_is_not_authoritative(self) -> None:
        db_path = self._db("server-authoritative.db")
        with StateStore(db_path) as store:
            store.append_chat_exchange("SERVER-SENTINEL", "server reply")
        route = SimpleNamespace(
            degraded=False,
            capability=SimpleNamespace(history_messages=6),
        )
        with mock.patch.object(
            web_api,
            "classify_chat_request",
            return_value={"kind": "conversational_request", "confidence": 1.0, "reason": "test"},
        ), mock.patch.object(
            web_api, "_build_device_enqueue_plan", return_value=None
        ), mock.patch.object(
            web_api, "_build_calendar_enqueue_plan", return_value=None
        ), mock.patch.object(
            web_api, "resolve_model_route", return_value=route
        ), mock.patch.object(
            web_api, "_run_freeform_chat_with_fallback", return_value="new reply"
        ) as chat_mock, mock.patch.object(
            web_api, "_append_chat_record"
        ):
            web_api.chat_message(
                db_path,
                "new question",
                [{"role": "user", "content": "CLIENT-CACHE-SENTINEL"}],
            )

        sent_messages = chat_mock.call_args.kwargs["messages"]
        serialized = json.dumps(sent_messages)
        self.assertIn("SERVER-SENTINEL", serialized)
        self.assertNotIn("CLIENT-CACHE-SENTINEL", serialized)

    def test_stale_instance_is_rejected_without_echoing_private_input(self) -> None:
        db_a = self._db("instance-a.db")
        db_b = self._db("instance-b.db")
        identity_b = self._identity(db_b)
        sentinel = "PRIVATE-CHAT-7d92"
        server = _RunningServer(db_a)
        try:
            status, body = server.request(
                "/api/chat",
                method="POST",
                payload={
                    "message": sentinel,
                    "history": [],
                    "instance_id": identity_b["instance_id"],
                    "schema_version": identity_b["schema_version"],
                },
            )
        finally:
            server.close()

        self.assertEqual(status, 409)
        self.assertNotIn(sentinel, json.dumps(body))

    def test_chat_errors_and_logs_do_not_echo_content_or_tokens(self) -> None:
        db_path = self._db("safe-errors.db")
        identity = self._identity(db_path)
        private_sentinel = "PRIVATE-CONTENT-8081"
        token_sentinel = "CONTROL-TOKEN-3934"
        server = _RunningServer(db_path)
        try:
            with mock.patch.object(
                web_api,
                "chat_message",
                side_effect=RuntimeError(f"{private_sentinel} {token_sentinel}"),
            ), self.assertLogs("gismo.web.server", level=logging.ERROR) as captured:
                status, body = server.request(
                    "/api/chat",
                    method="POST",
                    payload={
                        "message": private_sentinel,
                        "history": [],
                        **identity,
                    },
                )
        finally:
            server.close()

        combined = json.dumps(body) + "\n" + "\n".join(captured.output)
        self.assertEqual(status, 503)
        self.assertNotIn(private_sentinel, combined)
        self.assertNotIn(token_sentinel, combined)

    def test_frontend_has_no_persistent_chat_store_and_resets_on_instance_change(self) -> None:
        self.assertNotIn("localStorage", HTML)
        self.assertNotIn("sessionStorage", HTML)
        self.assertNotIn("indexedDB", HTML)
        self.assertIn("loadServerChatHistory", HTML)
        self.assertIn("activeInstanceId !== data.instance_id", HTML)
        self.assertIn("resetInstanceScopedUi()", HTML)
        self.assertIn("instance_id: activeInstanceId", HTML)


if __name__ == "__main__":
    unittest.main()
