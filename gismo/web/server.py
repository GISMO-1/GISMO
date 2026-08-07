"""GISMO local web server — stdlib only, zero extra dependencies."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from gismo.core.background_worker import ensure_background_worker_status
from gismo.core.paths import normalize_database_path
from gismo.core.state import StateStore
from gismo.web import api as web_api
from gismo.web import control_security
from gismo.web.templates import HTML

LOGGER = logging.getLogger(__name__)

_ITEM_ID_RE = re.compile(r"^/api/queue/([^/]+)/cancel$")
_DEVICE_STREAM_RE = re.compile(r"^/api/devices/([^/]+)/stream$")
_RUN_ID_RE = re.compile(r"^/api/runs/([^/?]+)$")
_PLAN_ID_RE = re.compile(r"^/api/plans/([^/]+)$")
_PLAN_ACTION_RE = re.compile(r"^/api/plans/([^/]+)/(approve|reject)$")
_CAL_EVENT_RE = re.compile(r"^/api/calendar/([^/]+)$")
_QUARANTINE_ID_RE = re.compile(r"^/api/quarantine/([^/]+)$")
_QUARANTINE_ACTION_RE = re.compile(r"^/api/quarantine/([^/]+)/(promote|reject)$")
_SECURITY_EXECUTION_ID_RE = re.compile(r"^/api/security/execution/([^/]+)$")


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, default=str).encode()
    handler.send_response(status)
    _send_extra_headers(handler)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, msg: str, status: int) -> None:
    _json_response(handler, {"error": msg}, status)


def _bytes_response(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    *,
    content_type: str,
    status: int = 200,
) -> None:
    handler.send_response(status)
    _send_extra_headers(handler)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_request_body(handler: BaseHTTPRequestHandler, *, max_bytes: int | None = None) -> bytes:
    length = int(handler.headers.get("Content-Length", 0))
    if max_bytes is not None and length > max_bytes:
        return b"x" * (max_bytes + 1)
    raw = handler.rfile.read(length) if length else b"{}"
    return raw or b"{}"


def _read_json_body(handler: BaseHTTPRequestHandler) -> Any:
    return json.loads(_read_request_body(handler))


def _send_extra_headers(handler: BaseHTTPRequestHandler) -> None:
    extra_headers = getattr(handler, "_extra_response_headers", None)
    if not callable(extra_headers):
        return
    for name, value in extra_headers():
        handler.send_header(name, value)


def _stream_mjpeg(handler: BaseHTTPRequestHandler, ffmpeg_args: list[str]) -> None:
    process = subprocess.Popen(
        ffmpeg_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        handler.send_response(200)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("Connection", "close")
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.end_headers()

        if process.stdout is None:
            return

        buffer = b""
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    buffer = b""
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buffer = buffer[start:]
                    break
                frame = buffer[start:end + 2]
                buffer = buffer[end + 2:]
                part = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
                try:
                    handler.wfile.write(part)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _make_handler(db_path: str, *, bind_host: str = "127.0.0.1") -> type[BaseHTTPRequestHandler]:
    db_path = normalize_database_path(db_path)
    with StateStore(db_path) as store:
        store.get_runtime_identity()
    control_context = control_security.build_context(db_path, bind_host=bind_host)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
            pass

        def _extra_response_headers(self) -> list[tuple[str, str]]:
            return control_security.response_headers(
                control_context,
                client_ip=self.client_address[0],
            )

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            try:
                if path == "/" or path == "/index.html":
                    body = HTML.encode()
                    self.send_response(200)
                    _send_extra_headers(self)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/status":
                    data = web_api.get_status(db_path)
                    data["db_path"] = db_path
                    _json_response(self, data)
                elif path == "/api/ready":
                    _json_response(self, web_api.get_readiness(db_path))
                elif path == "/api/queue":
                    _json_response(self, web_api.get_queue(db_path))
                elif path == "/api/queue/stats":
                    _json_response(self, web_api.get_queue_stats(db_path))
                elif path == "/api/runs":
                    _json_response(self, web_api.get_runs(db_path))
                elif m := _RUN_ID_RE.match(path):
                    run_id = m.group(1)
                    try:
                        _json_response(self, web_api.get_run_detail(db_path, run_id))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                elif path == "/api/memory":
                    _json_response(self, web_api.get_memory(db_path))
                elif path == "/api/tts/voices":
                    _json_response(self, web_api.get_voices(db_path))
                elif path == "/api/onboarding":
                    _json_response(self, web_api.get_onboarding_status(db_path))
                elif path == "/api/settings":
                    _json_response(self, web_api.get_settings(db_path))
                elif path == "/api/models/health":
                    _json_response(self, web_api.get_models_health(db_path))
                elif path == "/api/health":
                    _json_response(self, web_api.get_system_health(db_path))
                elif path == "/api/devices":
                    _json_response(self, web_api.list_devices(db_path))
                elif path == "/api/devices/list":
                    _json_response(self, web_api.list_devices(db_path))
                elif path == "/api/actuators":
                    _json_response(self, web_api.list_saved_actuators(db_path))
                elif path == "/api/actuators/list":
                    _json_response(self, web_api.list_saved_actuators(db_path))
                elif path == "/api/devices/scan":
                    _json_response(self, web_api.scan_devices(db_path))
                elif path == "/api/activity":
                    _json_response(self, web_api.get_activity_feed(db_path))
                elif path == "/api/chat/history":
                    _json_response(self, web_api.get_chat_history(db_path))
                elif path == "/api/execution/status":
                    _error(self, "Use POST for execution status.", 405)
                elif path == "/api/security/events":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    try:
                        _json_response(
                            self,
                            web_api.get_security_events(
                                db_path,
                                event_type=qs.get("type", [None])[0],
                                related_run_id=qs.get("run", [None])[0],
                                related_task_id=qs.get("task", [None])[0],
                                related_plan_id=qs.get("plan", [None])[0],
                                related_approval_id=qs.get("approval", [None])[0],
                                event_id=qs.get("event", [None])[0],
                                limit=int(qs.get("limit", ["50"])[0]),
                            ),
                        )
                    except ValueError as exc:
                        _error(self, str(exc), 404 if "not found" in str(exc).lower() else 400)
                elif path == "/api/security/verify":
                    _json_response(self, web_api.verify_security_event_chain(db_path))
                elif path == "/api/security/execution":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    _json_response(
                        self,
                        web_api.get_security_execution(
                            db_path,
                            mode=qs.get("mode", [None])[0],
                            zone=qs.get("zone", [None])[0],
                            component=qs.get("component", [None])[0],
                            related_run_id=qs.get("run", [None])[0],
                            related_task_id=qs.get("task", [None])[0],
                            related_plan_id=qs.get("plan", [None])[0],
                            recent=int(qs.get("recent", ["25"])[0]),
                        ),
                    )
                elif m := _SECURITY_EXECUTION_ID_RE.match(path):
                    try:
                        _json_response(self, web_api.get_security_execution_detail(db_path, m.group(1)))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                elif path == "/api/quarantine":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    _json_response(
                        self,
                        web_api.list_quarantine_entries(
                            db_path,
                            status=qs.get("status", [None])[0],
                            verification_status=qs.get("verification_status", [None])[0],
                            source_kind=qs.get("source_kind", [None])[0],
                            origin_type=qs.get("origin_type", [None])[0],
                            limit=int(qs.get("limit", ["50"])[0]),
                        ),
                    )
                elif m := _QUARANTINE_ID_RE.match(path):
                    try:
                        _json_response(self, web_api.get_quarantine_entry(db_path, m.group(1)))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                elif path == "/api/calendar":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    _json_response(
                        self,
                        web_api.list_calendar_events(
                            db_path,
                            start=qs.get("start", [None])[0],
                            end=qs.get("end", [None])[0],
                            day=qs.get("day", [None])[0],
                            limit=int(qs.get("limit", ["500"])[0]),
                        ),
                    )
                elif m := _CAL_EVENT_RE.match(path):
                    try:
                        _json_response(self, web_api.get_calendar_event(db_path, m.group(1)))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                elif path == "/api/briefing":
                    _json_response(self, web_api.get_briefing(db_path))
                elif m := _DEVICE_STREAM_RE.match(path):
                    payload = web_api.get_device_stream_payload(db_path, m.group(1))
                    if payload["kind"] == "mjpeg":
                        _stream_mjpeg(self, payload["ffmpeg_args"])
                    else:
                        _bytes_response(
                            self,
                            payload["body"],
                            content_type=payload["content_type"],
                        )
                elif path == "/api/plans":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    status_filter = qs.get("status", [None])[0]
                    _json_response(self, web_api.get_plans(db_path, status=status_filter))
                elif m := _PLAN_ID_RE.match(path):
                    plan_id = m.group(1)
                    try:
                        _json_response(self, web_api.get_plan_detail(db_path, plan_id))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                else:
                    _error(self, "Not found", 404)
            except Exception as exc:
                _error(self, str(exc), 500)

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            try:
                if m := _ITEM_ID_RE.match(path):
                    item_id = m.group(1)
                    try:
                        _json_response(self, web_api.cancel_queue_item(db_path, item_id))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                elif path == "/api/devices":
                    body = _read_json_body(self)
                    ip = (body.get("ip") or "").strip()
                    if not ip:
                        _error(self, "ip is required", 400)
                        return
                    _json_response(self, web_api.add_device(
                        db_path,
                        ip,
                        body.get("hostname"),
                        body.get("device_type", "smart device"),
                        body.get("brand", "Unknown"),
                        rtsp_url=body.get("rtsp_url"),
                        snapshot_url=body.get("snapshot_url"),
                        open_ports=body.get("open_ports") or [],
                    ))
                elif path == "/api/devices/add":
                    body = _read_json_body(self)
                    ip = (body.get("ip") or "").strip()
                    if not ip:
                        _error(self, "ip is required", 400)
                        return
                    _json_response(self, web_api.add_device(
                        db_path,
                        ip,
                        body.get("hostname"),
                        body.get("device_type", "smart device"),
                        body.get("brand", "Unknown"),
                        rtsp_url=body.get("rtsp_url"),
                        snapshot_url=body.get("snapshot_url"),
                        open_ports=body.get("open_ports") or [],
                    ))
                elif path == "/api/devices/remove":
                    body = _read_json_body(self)
                    device_id = (body.get("id") or "").strip()
                    if not device_id:
                        _error(self, "id is required", 400)
                        return
                    _json_response(self, web_api.remove_device(db_path, device_id))
                elif path == "/api/actuators/control":
                    request_bytes = _read_request_body(
                        self,
                        max_bytes=control_security.MAX_CONTROL_BODY_BYTES,
                    )
                    authorization = control_security.authorize_control_request(
                        context=control_context,
                        method="POST",
                        path=path,
                        client_ip=self.client_address[0],
                        headers=self.headers,
                        body_bytes=request_bytes,
                    )
                    if isinstance(authorization, control_security.ControlRequestRejection):
                        control_security.record_rejection(control_context, authorization)
                        _error(self, authorization.message, authorization.status)
                        return
                    control_security.record_accepted_request(control_context, authorization)
                    try:
                        _json_response(self, web_api.control_actuator(db_path, authorization.body))
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/queue/purge-failed":
                    _json_response(self, web_api.purge_failed(db_path))
                elif path == "/api/calendar":
                    body = _read_json_body(self)
                    try:
                        _json_response(self, web_api.create_calendar_event(db_path, body), 201)
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/daemon/pause":
                    _json_response(self, web_api.set_daemon_paused(db_path, True))
                elif path == "/api/daemon/resume":
                    _json_response(self, web_api.set_daemon_paused(db_path, False))
                elif path == "/api/settings":
                    body = _read_json_body(self)
                    try:
                        _json_response(
                            self,
                            web_api.save_settings(
                                db_path,
                                operator_name=body.get("operator_name"),
                                voice_id=body.get("voice_id") or body.get("voice"),
                                model_name=body.get("model_name") or body.get("model"),
                                primary_assistant_model=body.get("primary_assistant_model"),
                                planner_model=body.get("planner_model"),
                                helper_model=body.get("helper_model"),
                                allow_identity_fallback=body.get("allow_identity_fallback"),
                                performance_mode=body.get("performance_mode"),
                            ),
                        )
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/tts/voices/set":
                    body = _read_json_body(self)
                    voice_id = body.get("voice", "")
                    try:
                        _json_response(self, web_api.set_voice_preference(db_path, voice_id))
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/tts/speak":
                    body = _read_json_body(self)
                    text = body.get("text", "")
                    voice_id = body.get("voice") or None
                    if not text:
                        _error(self, "text is required", 400)
                        return
                    wav_bytes = web_api.tts_synthesize(db_path, text, voice_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                elif path == "/api/tts/preview":
                    body = _read_json_body(self)
                    voice_id = body.get("voice") or body.get("voice_id") or None
                    wav_bytes = web_api.tts_preview(db_path, voice_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                elif m := _PLAN_ACTION_RE.match(path):
                    plan_id, action = m.group(1), m.group(2)
                    body = _read_json_body(self)
                    try:
                        if action == "approve":
                            _json_response(self, web_api.approve_plan(db_path, plan_id))
                        else:
                            _json_response(self, web_api.reject_plan(db_path, plan_id, body.get("reason")))
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/onboarding/complete":
                    body = _read_json_body(self)
                    name = (body.get("name") or "").strip()
                    voice_id = (body.get("voice_id") or "").strip()
                    if not name or not voice_id:
                        _error(self, "name and voice_id are required", 400)
                        return
                    try:
                        _json_response(
                            self,
                            web_api.complete_onboarding(
                                db_path,
                                name,
                                voice_id,
                                performance_mode=body.get("performance_mode"),
                            ),
                        )
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/execution/status":
                    body = _read_json_body(self)
                    try:
                        _json_response(
                            self,
                            web_api.get_execution_status(
                                db_path,
                                queue_item_ids=body.get("queue_item_ids") or [],
                                run_id=body.get("run_id"),
                            ),
                        )
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/chat":
                    body = _read_json_body(self)
                    message = (body.get("message") or "").strip()
                    history = body.get("history") or []
                    if not message:
                        _error(self, "message is required", 400)
                        return
                    with StateStore(db_path) as store:
                        runtime_identity = store.get_runtime_identity()
                    if (
                        body.get("instance_id") != runtime_identity["instance_id"]
                        or body.get("schema_version") != runtime_identity["schema_version"]
                    ):
                        _error(self, "This page belongs to a different GISMO instance. Refresh and try again.", 409)
                        return
                    try:
                        _json_response(self, web_api.chat_message(db_path, message, history))
                    except RuntimeError as exc:
                        LOGGER.error("chat_endpoint_failed error_type=%s", type(exc).__name__)
                        _error(self, "GISMO could not answer that right now. Please try again in a moment.", 503)
                elif m := _QUARANTINE_ACTION_RE.match(path):
                    record_id, action = m.group(1), m.group(2)
                    body = _read_json_body(self)
                    try:
                        if action == "promote":
                            _json_response(self, web_api.promote_quarantine_entry(db_path, record_id, body))
                        else:
                            _json_response(self, web_api.reject_quarantine_entry(db_path, record_id, body))
                    except ValueError as exc:
                        status = 404 if "not found" in str(exc).lower() else 400
                        _error(self, str(exc), status)
                else:
                    _error(self, "Not found", 404)
            except Exception as exc:
                LOGGER.error("web_post_failed error_type=%s", type(exc).__name__)
                _error(self, "Request failed.", 500)

        def do_PATCH(self) -> None:
            path = self.path.split("?")[0]
            try:
                if m := _CAL_EVENT_RE.match(path):
                    body = _read_json_body(self)
                    try:
                        _json_response(self, web_api.update_calendar_event(db_path, m.group(1), body))
                    except ValueError as exc:
                        _error(self, str(exc), 400 if "not found" not in str(exc).lower() else 404)
                elif m := _PLAN_ID_RE.match(path):
                    plan_id = m.group(1)
                    body = _read_json_body(self)
                    try:
                        result = web_api.patch_plan(
                            db_path, plan_id,
                            action_index=body.get("action_index"),
                            new_command=body.get("command"),
                            remove_action=bool(body.get("remove_action", False)),
                        )
                        _json_response(self, result)
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                else:
                    _error(self, "Not found", 404)
            except Exception as exc:
                _error(self, str(exc), 500)

        def do_DELETE(self) -> None:
            path = self.path.split("?")[0]
            try:
                if m := _CAL_EVENT_RE.match(path):
                    try:
                        _json_response(self, web_api.delete_calendar_event(db_path, m.group(1)))
                    except ValueError as exc:
                        _error(self, str(exc), 404)
                else:
                    _error(self, "Not found", 404)
            except Exception as exc:
                _error(self, str(exc), 500)

    return _Handler


def run(db_path: str, host: str = "127.0.0.1", port: int = 7800, open_browser: bool = True) -> None:
    """Start the local web server and optionally open the browser."""
    db_path = normalize_database_path(db_path)
    ensure_background_worker_status(db_path, source="web_server")
    handler_cls = _make_handler(db_path, bind_host=host)
    server = HTTPServer((host, port), handler_cls)
    url = f"http://{host}:{port}/"
    print(f"GISMO web dashboard: {url}")
    print(f"DB: {db_path}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
