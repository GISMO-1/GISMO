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


if __name__ == "__main__":
    unittest.main()
