"""GISMO local web server — stdlib only, zero extra dependencies."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from gismo.web import api as web_api
from gismo.web.templates import HTML

_ITEM_ID_RE = re.compile(r"^/api/queue/([^/]+)/cancel$")
_DEVICE_STREAM_RE = re.compile(r"^/api/devices/([^/]+)/stream$")
_RUN_ID_RE = re.compile(r"^/api/runs/([^/?]+)$")
_PLAN_ID_RE = re.compile(r"^/api/plans/([^/]+)$")
_PLAN_ACTION_RE = re.compile(r"^/api/plans/([^/]+)/(approve|reject)$")


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, default=str).encode()
    handler.send_response(status)
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
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw or b"{}")


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


def _make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
            pass

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            try:
                if path == "/" or path == "/index.html":
                    body = HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/status":
                    data = web_api.get_status(db_path)
                    data["db_path"] = db_path
                    _json_response(self, data)
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
                elif path == "/api/health":
                    _json_response(self, web_api.get_system_health())
                elif path == "/api/devices":
                    _json_response(self, web_api.list_devices(db_path))
                elif path == "/api/devices/list":
                    _json_response(self, web_api.list_devices(db_path))
                elif path == "/api/devices/scan":
                    _json_response(self, web_api.scan_devices(db_path))
                elif path == "/api/activity":
                    _json_response(self, web_api.get_activity_feed(db_path))
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
                elif path == "/api/queue/purge-failed":
                    _json_response(self, web_api.purge_failed(db_path))
                elif path == "/api/daemon/pause":
                    _json_response(self, web_api.set_daemon_paused(db_path, True))
                elif path == "/api/daemon/resume":
                    _json_response(self, web_api.set_daemon_paused(db_path, False))
                elif path == "/api/settings":
                    body = _read_json_body(self)
                    _json_response(
                        self,
                        web_api.save_settings(
                            db_path,
                            operator_name=body.get("operator_name"),
                            voice_id=body.get("voice_id"),
                        ),
                    )
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
                        _json_response(self, web_api.complete_onboarding(db_path, name, voice_id))
                    except ValueError as exc:
                        _error(self, str(exc), 400)
                elif path == "/api/chat":
                    body = _read_json_body(self)
                    message = (body.get("message") or "").strip()
                    history = body.get("history") or []
                    if not message:
                        _error(self, "message is required", 400)
                        return
                    try:
                        _json_response(self, web_api.chat_message(db_path, message, history))
                    except RuntimeError as exc:
                        _error(self, str(exc), 503)
                else:
                    _error(self, "Not found", 404)
            except Exception as exc:
                _error(self, str(exc), 500)

        def do_PATCH(self) -> None:
            path = self.path.split("?")[0]
            try:
                if m := _PLAN_ID_RE.match(path):
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

    return _Handler


def run(db_path: str, host: str = "127.0.0.1", port: int = 7800, open_browser: bool = True) -> None:
    """Start the local web server and optionally open the browser."""
    handler_cls = _make_handler(db_path)
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
