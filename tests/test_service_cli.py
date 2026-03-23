"""Tests for gismo service install/uninstall/status CLI."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import service_cli
from gismo.core.security_events import list_security_events
from gismo.core.state import StateStore


def _make_db(tmp: str) -> str:
    db_path = os.path.join(tmp, "state.db")
    with StateStore(db_path) as store:
        pass
    return db_path


def _write_policy(tmp: str, allowed_tools: list[str]) -> str:
    policy = {
        "allowed_tools": allowed_tools,
        "fs": {"base_dir": "."},
    }
    path = os.path.join(tmp, "policy.json")
    Path(path).write_text(json.dumps(policy), encoding="utf-8")
    return path


class ServiceStatusTest(unittest.TestCase):
    def test_service_status_when_no_task_exists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with mock.patch("gismo.cli.service_cli.subprocess.run", return_value=fake_result):
                with mock.patch("builtins.print") as mock_print:
                    service_cli.run_service_status(db_path)
            output = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", output)


class ServiceInstallTest(unittest.TestCase):
    @mock.patch("gismo.cli.service_cli.os.name", "nt")
    def test_service_install_creates_task(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["service.install"])
            query_not_found = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with mock.patch("gismo.cli.service_cli.subprocess.run", return_value=query_not_found):
                with mock.patch("gismo.cli.service_cli.install_windows_task") as mock_install:
                    with mock.patch("gismo.cli.service_cli._ensure_windows"):
                        service_cli.run_service_install(db_path, policy_path=policy_path)
            mock_install.assert_called_once()
            config = mock_install.call_args[0][0]
            self.assertEqual(config.name, "GISMO-Daemon")
            self.assertEqual(config.db_path, db_path)
            events = list_security_events(db_path=db_path, event_type="service_install")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].resource, "GISMO-Daemon")
            self.assertIn("db_path", events[0].payload)

    @mock.patch("gismo.cli.service_cli.os.name", "nt")
    def test_service_install_denied_when_task_already_exists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["service.install"])
            query_found = subprocess.CompletedProcess(args=[], returncode=0, stdout="TaskName: GISMO-Daemon", stderr="")
            with mock.patch("gismo.cli.service_cli.subprocess.run", return_value=query_found):
                with mock.patch("gismo.cli.service_cli._ensure_windows"):
                    with mock.patch("builtins.print") as mock_print:
                        service_cli.run_service_install(db_path, policy_path=policy_path)
            output = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("already exists", output)
            events = list_security_events(db_path=db_path, event_type="service_install")
            self.assertEqual(len(events), 0)

    def test_service_install_requires_policy_permission(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["echo"])
            with mock.patch("gismo.cli.service_cli._ensure_windows"):
                with self.assertRaises(PermissionError):
                    service_cli.run_service_install(db_path, policy_path=policy_path)


class ServiceUninstallTest(unittest.TestCase):
    @mock.patch("gismo.cli.service_cli.os.name", "nt")
    def test_service_uninstall_removes_task(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["service.uninstall"])

            def fake_run(args, **kwargs):
                if "/Query" in args:
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="TaskName: GISMO-Daemon", stderr="")
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

            with mock.patch("gismo.cli.service_cli.subprocess.run", side_effect=fake_run):
                with mock.patch("gismo.cli.service_cli._ensure_windows"):
                    service_cli.run_service_uninstall(db_path, policy_path=policy_path)
            events = list_security_events(db_path=db_path, event_type="service_uninstall")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].resource, "GISMO-Daemon")

    @mock.patch("gismo.cli.service_cli.os.name", "nt")
    def test_service_uninstall_when_not_installed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["service.uninstall"])
            query_not_found = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with mock.patch("gismo.cli.service_cli.subprocess.run", return_value=query_not_found):
                with mock.patch("gismo.cli.service_cli._ensure_windows"):
                    with mock.patch("builtins.print") as mock_print:
                        service_cli.run_service_uninstall(db_path, policy_path=policy_path)
            output = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", output)


class ServiceAuditEventTest(unittest.TestCase):
    @mock.patch("gismo.cli.service_cli.os.name", "nt")
    def test_service_emits_audit_event_on_install(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = _make_db(tmp)
            policy_path = _write_policy(tmp, ["service.install"])
            query_not_found = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with mock.patch("gismo.cli.service_cli.subprocess.run", return_value=query_not_found):
                with mock.patch("gismo.cli.service_cli.install_windows_task"):
                    with mock.patch("gismo.cli.service_cli._ensure_windows"):
                        service_cli.run_service_install(db_path, policy_path=policy_path)
            events = list_security_events(db_path=db_path, event_type="service_install")
            self.assertEqual(len(events), 1)
            payload = events[0].payload
            self.assertEqual(payload["task_name"], "GISMO-Daemon")
            self.assertEqual(payload["db_path"], db_path)
            self.assertIn("schtasks_args", payload)
            self.assertIn("command", payload)


if __name__ == "__main__":
    unittest.main()
