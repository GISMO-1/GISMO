import re
import tempfile
import unittest
from unittest import mock

from gismo.cli import ipc as ipc_cli
from gismo.core.state import StateStore


class IpcHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tempdir.name}/state.db"
        self.state_store = StateStore(self.db_path)
        self.token = "secret-token"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_missing_token_unauthorized(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {"action": "queue_stats", "args": {}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "unauthorized")
        self.assertTrue(response["request_id"])

    def test_wrong_token_unauthorized(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {"action": "queue_stats", "token": "bad", "args": {}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "unauthorized")

    def test_enqueue_validates_operator_command(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {
                "action": "enqueue",
                "token": self.token,
                "args": {"command": "invalid: command"},
            },
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertFalse(response["ok"])
        self.assertIn("Unsupported command", response["error"])

    def test_enqueue_routes_to_state_store(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {
                "action": "enqueue",
                "token": self.token,
                "args": {"command": "echo: hello"},
            },
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertTrue(response["ok"])
        data = response["data"]
        assert data is not None
        item = self.state_store.get_queue_item(data["queue_item_id"])
        self.assertIsNotNone(item)

    def test_queue_stats_response_shape(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {"action": "queue_stats", "token": self.token, "args": {}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertTrue(response["ok"])
        data = response["data"]
        assert data is not None
        self.assertIn("total", data)
        self.assertIn("by_status", data)
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)
        self.assertIn("attempts", data)
        self.assertIn("db_path", data)

    def test_run_show_response_shape(self) -> None:
        run = self.state_store.create_run(label="ipc-test")
        response = ipc_cli.handle_ipc_request(
            {"action": "run_show", "token": self.token, "args": {"run_id": run.id}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertTrue(response["ok"])
        data = response["data"]
        assert data is not None
        self.assertEqual(data["run"]["id"], run.id)
        self.assertIn("tasks", data)
        self.assertIn("tool_calls", data)

    def test_ipc_request_wraps_connection_error(self) -> None:
        with mock.patch.object(ipc_cli, "_connect", side_effect=FileNotFoundError()):
            with self.assertRaises(ipc_cli.IPCConnectionError):
                ipc_cli.ipc_request("queue_stats", {}, self.token)

    def test_new_actions_require_token(self) -> None:
        actions = [
            "ping",
            "daemon_status",
            "daemon_pause",
            "daemon_resume",
            "queue_purge_failed",
            "queue_requeue_stale",
        ]
        for action in actions:
            with self.subTest(action=action):
                response = ipc_cli.handle_ipc_request(
                    {"action": action, "args": {}},
                    expected_token=self.token,
                    state_store=self.state_store,
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "unauthorized")

    def test_queue_purge_failed_deletes_only_failed(self) -> None:
        failed_item = self.state_store.enqueue_command("echo: fail")
        self.state_store.mark_queue_item_failed(failed_item.id, "boom", retryable=False)
        queued_item = self.state_store.enqueue_command("echo: queued")
        succeeded_item = self.state_store.enqueue_command("echo: ok")
        self.state_store.mark_queue_item_succeeded(succeeded_item.id)

        response = ipc_cli.handle_ipc_request(
            {"action": "queue_purge_failed", "token": self.token, "args": {}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertTrue(response["ok"])
        data = response["data"]
        assert data is not None
        self.assertEqual(data["deleted"], 1)
        self.assertIsNone(self.state_store.get_queue_item(failed_item.id))
        self.assertIsNotNone(self.state_store.get_queue_item(queued_item.id))
        self.assertIsNotNone(self.state_store.get_queue_item(succeeded_item.id))

    def test_ping_response_shape(self) -> None:
        response = ipc_cli.handle_ipc_request(
            {"action": "ping", "token": self.token, "args": {}},
            expected_token=self.token,
            state_store=self.state_store,
        )
        self.assertTrue(response["ok"])
        data = response["data"]
        assert data is not None
        self.assertEqual(data["status"], "ok")


class IpcEndpointTest(unittest.TestCase):
    def test_windows_ipc_endpoint_is_stable_and_sanitized(self) -> None:
        db_path = r"C:\Users\gismo\state.db"
        endpoint_one = ipc_cli.ipc_endpoint(db_path, os_name="nt")
        endpoint_two = ipc_cli.ipc_endpoint(db_path, os_name="nt")
        self.assertEqual(endpoint_one.address, endpoint_two.address)
        self.assertEqual(endpoint_one.family, "AF_PIPE")
        self.assertRegex(
            endpoint_one.address,
            re.compile(r"^\\\\\.\\pipe\\gismo-[0-9a-f]{12}$"),
        )


class IpcServeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tempdir.name}/state.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_serve_ipc_stops_on_keyboard_interrupt(self) -> None:
        listener = self._run_serve_with_listener([KeyboardInterrupt()])
        self.assertTrue(listener.closed)

    def test_serve_ipc_stops_on_assertion_error_when_allowed(self) -> None:
        listener = self._run_serve_with_listener(
            [AssertionError("boom")],
            allow_accept_error=True,
        )
        self.assertTrue(listener.closed)

    def test_serve_ipc_stops_on_oserror_when_allowed(self) -> None:
        listener = self._run_serve_with_listener(
            [OSError("operation aborted")],
            allow_accept_error=True,
        )
        self.assertTrue(listener.closed)

    def _run_serve_with_listener(
        self,
        exceptions: list[BaseException],
        *,
        allow_accept_error: bool = False,
    ) -> "FakeListener":
        listener = FakeListener(exceptions)
        endpoint = ipc_cli.IPCEndpoint("ignored", "AF_PIPE")
        with mock.patch.object(ipc_cli, "Listener", return_value=listener):
            with mock.patch.object(ipc_cli, "ipc_endpoint", return_value=endpoint):
                if allow_accept_error:
                    with mock.patch.object(
                        ipc_cli,
                        "_should_exit_on_accept_error",
                        return_value=True,
                    ):
                        ipc_cli.serve_ipc(self.db_path, "token")
                else:
                    ipc_cli.serve_ipc(self.db_path, "token")
        return listener


class FakeListener:
    def __init__(self, exceptions: list[BaseException]):
        self.exceptions = list(exceptions)
        self.closed = False

    def accept(self):
        exc = self.exceptions.pop(0)
        raise exc

    def close(self) -> None:
        self.closed = True


class IpcAcceptErrorTest(unittest.TestCase):
    def test_should_exit_on_accept_error_windows_assertion(self) -> None:
        self.assertTrue(
            ipc_cli._should_exit_on_accept_error(AssertionError("boom"), "nt")
        )
        self.assertFalse(
            ipc_cli._should_exit_on_accept_error(AssertionError("boom"), "posix")
        )

    def test_should_exit_on_accept_error_windows_oserror(self) -> None:
        oserr = OSError("operation aborted")
        oserr.winerror = 995
        self.assertTrue(ipc_cli._should_exit_on_accept_error(oserr, "nt"))
        self.assertFalse(ipc_cli._should_exit_on_accept_error(oserr, "posix"))


if __name__ == "__main__":
    unittest.main()
