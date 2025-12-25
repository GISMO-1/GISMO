import tempfile
import unittest
from pathlib import Path

from gismo.cli import supervise as supervise_cli


class FakeProcessOps:
    def __init__(self, running: set[int]) -> None:
        self._running = running

    def spawn(self, argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("spawn should not be called in tests")

    def is_running(self, pid: int) -> bool:
        return pid in self._running

    def terminate(self, pid: int) -> None:
        raise AssertionError("terminate should not be called in tests")

    def kill(self, pid: int) -> None:
        raise AssertionError("kill should not be called in tests")


class SupervisePidFileTest(unittest.TestCase):
    def test_save_and_load_supervisor_record(self) -> None:
        record = supervise_cli.SupervisorRecord(
            ipc_pid=1001,
            daemon_pid=1002,
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
            db_path="state.db",
            started_at="2024-01-02T00:00:00Z",
        )
        process_ops = FakeProcessOps(running={2002})
        status = supervise_cli.summarize_supervisor_status(record, process_ops)
        self.assertFalse(status.ipc_running)
        self.assertTrue(status.daemon_running)


if __name__ == "__main__":
    unittest.main()
