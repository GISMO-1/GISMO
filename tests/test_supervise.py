import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import supervise as supervise_cli


class FakeProcess:
    def __init__(self, pid: int, poll_result: int | None = 0) -> None:
        self.pid = pid
        self._poll_result = poll_result
        self.stdout = None

    def poll(self) -> int | None:
        return self._poll_result


class FakeProcessOps:
    def __init__(
        self,
        spawn_processes: list[FakeProcess] | None = None,
        running: set[int] | None = None,
    ) -> None:
        self._running = running or set()
        self._spawn_processes = list(spawn_processes or [])
        self.spawn_calls: list[list[str]] = []
        self.terminated: list[int] = []
        self.killed: list[int] = []

    def spawn(self, argv: list[str], env: dict[str, str]) -> FakeProcess:
        self.spawn_calls.append(argv)
        if not self._spawn_processes:
            raise AssertionError("unexpected spawn call")
        return self._spawn_processes.pop(0)

    def is_running(self, pid: int) -> bool:
        return pid in self._running

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)


class SupervisePidFileTest(unittest.TestCase):
    def test_save_and_load_supervisor_record(self) -> None:
        record = supervise_cli.SupervisorRecord(
            ipc_pid=1001,
            daemon_pid=1002,
            ipc_started=True,
            ipc_reused=False,
            daemon_started=False,
            db_path=".gismo/state.db",
            started_at="2024-01-01T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "supervise.json"
            supervise_cli.save_supervisor_record(path, record)
            loaded = supervise_cli.load_supervisor_record(path)
            self.assertEqual(record, loaded)

    def test_load_missing_supervisor_record(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "missing.json"
            loaded = supervise_cli.load_supervisor_record(path)
            self.assertIsNone(loaded)


class SuperviseStatusTest(unittest.TestCase):
    def test_summarize_supervisor_status(self) -> None:
        record = supervise_cli.SupervisorRecord(
            ipc_pid=2001,
            daemon_pid=2002,
            ipc_started=True,
            ipc_reused=False,
            daemon_started=True,
            db_path="state.db",
            started_at="2024-01-02T00:00:00Z",
        )
        process_ops = FakeProcessOps(running={2002})
        status = supervise_cli.summarize_supervisor_status(record, process_ops)
        self.assertFalse(status.ipc_running)
        self.assertTrue(status.daemon_running)


class SuperviseUpTest(unittest.TestCase):
    def test_ipc_already_running_skips_ipc_spawn(self) -> None:
        process_ops = FakeProcessOps(spawn_processes=[FakeProcess(3001)])
        ping_response = {
            "ok": True,
            "request_id": "ping-1",
            "data": {"status": "ok"},
            "error": None,
        }
        with tempfile.TemporaryDirectory() as tempdir:
            pid_path = Path(tempdir) / "supervise.json"
            with mock.patch(
                "gismo.cli.supervise.ipc_cli.ipc_request",
                return_value=ping_response,
            ):
                with mock.patch(
                    "gismo.cli.supervise.save_supervisor_record"
                ) as save_mock:
                    supervise_cli.run_supervise_up(
                        "state.db",
                        "token",
                        pid_path=pid_path,
                        process_ops=process_ops,
                    )
        self.assertEqual(len(process_ops.spawn_calls), 1)
        self.assertIn("daemon", process_ops.spawn_calls[0])
        saved_record = save_mock.call_args.args[1]
        self.assertFalse(saved_record.ipc_started)
        self.assertTrue(saved_record.ipc_reused)
        self.assertTrue(saved_record.daemon_started)

    def test_ipc_unauthorized_fails_cleanly(self) -> None:
        process_ops = FakeProcessOps()
        unauthorized_response = {
            "ok": False,
            "request_id": "ping-2",
            "data": None,
            "error": "unauthorized",
        }
        with tempfile.TemporaryDirectory() as tempdir:
            pid_path = Path(tempdir) / "supervise.json"
            with mock.patch(
                "gismo.cli.supervise.ipc_cli.ipc_request",
                return_value=unauthorized_response,
            ):
                with self.assertRaises(SystemExit) as context:
                    supervise_cli.run_supervise_up(
                        "state.db",
                        "bad-token",
                        pid_path=pid_path,
                        process_ops=process_ops,
                    )
        self.assertNotEqual(context.exception.code, 0)
        self.assertEqual(process_ops.spawn_calls, [])

    def test_pid_record_captures_started_children(self) -> None:
        process_ops = FakeProcessOps(spawn_processes=[FakeProcess(4001), FakeProcess(4002)])
        with tempfile.TemporaryDirectory() as tempdir:
            pid_path = Path(tempdir) / "supervise.json"
            with mock.patch(
                "gismo.cli.supervise.ipc_cli.ipc_request",
                side_effect=supervise_cli.ipc_cli.IPCConnectionError("down"),
            ):
                with mock.patch(
                    "gismo.cli.supervise.save_supervisor_record"
                ) as save_mock:
                    supervise_cli.run_supervise_up(
                        "state.db",
                        "token",
                        pid_path=pid_path,
                        process_ops=process_ops,
                    )
        saved_record = save_mock.call_args.args[1]
        self.assertTrue(saved_record.ipc_started)
        self.assertFalse(saved_record.ipc_reused)
        self.assertTrue(saved_record.daemon_started)


class SuperviseStatusOutputTest(unittest.TestCase):
    def test_status_reports_running_when_ipc_reachable(self) -> None:
        record = supervise_cli.SupervisorRecord(
            ipc_pid=0,
            daemon_pid=0,
            ipc_started=False,
            ipc_reused=True,
            daemon_started=True,
            db_path="state.db",
            started_at="2024-01-03T00:00:00Z",
        )
        ping_response = {
            "ok": True,
            "request_id": "ping-1",
            "data": {"status": "ok"},
            "error": None,
        }
        daemon_response = {
            "ok": True,
            "request_id": "daemon-1",
            "data": {"paused": False},
            "error": None,
        }
        with tempfile.TemporaryDirectory() as tempdir:
            pid_path = Path(tempdir) / "supervise.json"
            supervise_cli.save_supervisor_record(pid_path, record)
            with mock.patch(
                "gismo.cli.supervise.ipc_cli.ipc_request",
                side_effect=[ping_response, daemon_response],
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    supervise_cli.run_supervise_status(
                        "token",
                        db_path="state.db",
                        pid_path=pid_path,
                        process_ops=FakeProcessOps(),
                    )
        output = buffer.getvalue()
        self.assertIn("ipc_status: running (observed: ping)", output)
        self.assertIn("daemon_status: running", output)

    def test_status_reports_paused_from_ipc(self) -> None:
        record = supervise_cli.SupervisorRecord(
            ipc_pid=1234,
            daemon_pid=5678,
            ipc_started=True,
            ipc_reused=False,
            daemon_started=True,
            db_path="state.db",
            started_at="2024-01-04T00:00:00Z",
        )
        ping_response = {
            "ok": True,
            "request_id": "ping-2",
            "data": {"status": "ok"},
            "error": None,
        }
        daemon_response = {
            "ok": True,
            "request_id": "daemon-2",
            "data": {"paused": True},
            "error": None,
        }
        with tempfile.TemporaryDirectory() as tempdir:
            pid_path = Path(tempdir) / "supervise.json"
            supervise_cli.save_supervisor_record(pid_path, record)
            with mock.patch(
                "gismo.cli.supervise.ipc_cli.ipc_request",
                side_effect=[ping_response, daemon_response],
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    supervise_cli.run_supervise_status(
                        "token",
                        db_path="state.db",
                        pid_path=pid_path,
                        process_ops=FakeProcessOps(),
                    )
        output = buffer.getvalue()
        self.assertIn("daemon_status: paused", output)


if __name__ == "__main__":
    unittest.main()
