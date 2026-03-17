from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from gismo.core import background_worker
from gismo.desktop import app as desktop_app
from gismo.web import server as web_server


class TestBackgroundWorker(unittest.TestCase):
    def test_ensure_background_worker_skips_when_healthy(self) -> None:
        with mock.patch.object(background_worker, "_worker_is_healthy", return_value=True), mock.patch(
            "gismo.core.background_worker.subprocess.Popen"
        ) as popen_mock:
            started = background_worker.ensure_background_worker("tmp/state.db")

        self.assertFalse(started)
        popen_mock.assert_not_called()

    def test_ensure_background_worker_spawns_when_missing(self) -> None:
        with mock.patch.object(background_worker, "_worker_is_healthy", return_value=False), mock.patch(
            "gismo.core.background_worker.subprocess.Popen"
        ) as popen_mock:
            started = background_worker.ensure_background_worker("tmp/state.db")

        self.assertTrue(started)
        argv = popen_mock.call_args.args[0]
        self.assertEqual(argv[1:4], ["-m", "gismo.cli.main", "daemon"])

    def test_worker_is_healthy_rejects_stale_dead_pid(self) -> None:
        heartbeat = SimpleNamespace(
            pid=4321,
            last_seen=datetime.now(timezone.utc) - timedelta(seconds=90),
        )
        store = mock.MagicMock()
        store.get_daemon_heartbeat.return_value = heartbeat
        state_store = mock.MagicMock()
        state_store.return_value.__enter__.return_value = store
        state_store.return_value.__exit__.return_value = False
        with mock.patch.object(background_worker, "StateStore", state_store), mock.patch.object(
            background_worker,
            "_pid_is_running",
            return_value=False,
        ):
            healthy = background_worker._worker_is_healthy("tmp/state.db")

        self.assertFalse(healthy)


class TestLaunchHooks(unittest.TestCase):
    def test_web_server_run_ensures_background_worker(self) -> None:
        fake_server = mock.MagicMock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt()
        with mock.patch.object(web_server, "ensure_background_worker") as ensure_mock, mock.patch.object(
            web_server,
            "HTTPServer",
            return_value=fake_server,
        ):
            web_server.run("tmp/state.db", open_browser=False)

        ensure_mock.assert_called_once_with("tmp/state.db")
        fake_server.server_close.assert_called_once()

    def test_desktop_launch_ensures_background_worker(self) -> None:
        fake_server = mock.MagicMock()
        fake_window = object()
        fake_webview = SimpleNamespace(
            create_window=mock.Mock(return_value=fake_window),
            start=mock.Mock(),
        )
        with mock.patch.object(desktop_app, "ensure_background_worker") as ensure_mock, mock.patch.object(
            desktop_app,
            "_find_free_port",
            return_value=7812,
        ), mock.patch.object(
            desktop_app,
            "_start_server",
            return_value=fake_server,
        ), mock.patch.dict(
            "sys.modules",
            {"webview": fake_webview},
        ):
            desktop_app.launch("tmp/state.db")

        ensure_mock.assert_called_once_with("tmp/state.db")
        fake_webview.start.assert_called_once()
        fake_server.shutdown.assert_called_once()
        fake_server.server_close.assert_called_once()
