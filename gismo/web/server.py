"""GISMO local web server — stdlib only, zero extra dependencies."""
from __future__ import annotations

import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from gismo.web import api as web_api
from gismo.web.templates import HTML

_ITEM_ID_RE = re.compile(r"^/api/queue/([^/]+)/cancel$")
_RUN_ID_RE = re.compile(r"^/api/runs/([^/?]+)$")


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, msg: str, status: int) -> None:
    _json_response(handler, {"error": msg}, status)


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
                elif path == "/api/queue/purge-failed":
                    _json_response(self, web_api.purge_failed(db_path))
                elif path == "/api/daemon/pause":
                    _json_response(self, web_api.set_daemon_paused(db_path, True))
                elif path == "/api/daemon/resume":
                    _json_response(self, web_api.set_daemon_paused(db_path, False))
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
