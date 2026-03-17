"""GISMO desktop launcher — native window via pywebview, no browser tab."""
from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

from gismo.core.background_worker import ensure_background_worker

# ── Splash screen HTML ──────────────────────────────────────────────────────

_SPLASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: #0d1117;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace;
    user-select: none;
  }

  .logo-wrap { position: relative; margin-bottom: 28px; }

  .glow {
    position: absolute;
    inset: -28px;
    background: radial-gradient(circle, rgba(88,166,255,0.18) 0%, transparent 68%);
    animation: pulse 2.4s ease-in-out infinite;
    pointer-events: none;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.35; transform: scale(0.92); }
    50%       { opacity: 1.0;  transform: scale(1.08); }
  }

  svg.hex { position: relative; z-index: 1; }

  .wordmark { font-size: 26px; letter-spacing: 12px; color: #58a6ff; margin-bottom: 6px; }
  .tagline  { font-size: 10px; letter-spacing: 2.5px; color: #4a5568; margin-bottom: 36px; text-transform: uppercase; }
  .status   { font-size: 11px; letter-spacing: 1.5px; color: #8b949e; }
</style>
</head>
<body>
  <div class="logo-wrap">
    <div class="glow"></div>
    <svg class="hex" width="170" height="170" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="hexFill" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%"   stop-color="#1a2a42"/>
          <stop offset="100%" stop-color="#0d1117"/>
        </linearGradient>
      </defs>
      <!-- Outer hex border -->
      <polygon
        points="50,3.5 93.3,27 93.3,73 50,96.5 6.7,73 6.7,27"
        fill="url(#hexFill)"
        stroke="#58a6ff"
        stroke-width="1.5"
      />
      <!-- Inner divider ring -->
      <polygon
        points="50,14 83,32 83,68 50,86 17,68 17,32"
        fill="none"
        stroke="#30363d"
        stroke-width="0.6"
        opacity="0.7"
      />
      <!-- G · I · S · M · O -->
      <text x="50" y="45"
        text-anchor="middle"
        font-family="Consolas, 'Courier New', monospace"
        font-size="10.5"
        font-weight="bold"
        letter-spacing="2"
        fill="#58a6ff">G·I·S·M·O</text>
      <!-- Divider -->
      <line x1="22" y1="51" x2="78" y2="51" stroke="#30363d" stroke-width="0.5"/>
      <!-- Subtitle -->
      <text x="50" y="63"
        text-anchor="middle"
        font-family="Consolas, 'Courier New', monospace"
        font-size="6"
        letter-spacing="0.8"
        fill="#8b949e">LOCAL · AI</text>
    </svg>
  </div>

  <div class="wordmark">GISMO</div>
  <div class="tagline">General Intelligent System for Multiflow Operations</div>
  <div class="status" id="status">Initializing<span id="dots"></span></div>

  <script>
    let n = 0;
    setInterval(() => {
      n = (n + 1) % 4;
      document.getElementById('dots').textContent = '.'.repeat(n);
    }, 380);
  </script>
</body>
</html>"""


# ── Server helpers ──────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Bind to port 0, let the OS pick, return the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(db_path: str, port: int) -> HTTPServer:
    """Create and start the GISMO HTTP server in a background daemon thread."""
    from gismo.web.server import _make_handler

    handler_cls = _make_handler(db_path)
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Startup function (runs in webview's worker thread) ─────────────────────


def _startup(window, server_url: str) -> None:
    """Called by webview in a daemon thread after the GUI loop starts."""
    time.sleep(0.9)  # let the splash breathe
    window.load_url(server_url)


# ── Main entry point ────────────────────────────────────────────────────────


def launch(db_path: str) -> None:
    """Open the GISMO desktop window. Blocks until the window is closed."""
    try:
        import webview
    except ImportError:
        print(
            "pywebview is required for the desktop app.\n"
            "Install it with:  pip install pywebview",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure db directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    ensure_background_worker(db_path)

    port = _find_free_port()
    server = _start_server(db_path, port)
    server_url = f"http://127.0.0.1:{port}/"

    window = webview.create_window(
        title="GISMO",
        html=_SPLASH_HTML,
        width=1440,
        height=900,
        min_size=(1024, 680),
        background_color="#0d1117",
        text_select=True,
    )

    try:
        webview.start(_startup, args=(window, server_url))
    finally:
        # webview.start() blocks until all windows are closed
        server.shutdown()
        server.server_close()
