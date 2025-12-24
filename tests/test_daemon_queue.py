import tempfile
import unittest
from pathlib import Path

from gismo.core import daemon as daemon_module
from gismo.core.models import QueueStatus
from gismo.core.state import StateStore
from gismo.core.tools import Tool, ToolRegistry


class FlakyEchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(name="echo", description="Fails once then succeeds")
        self.calls = 0

    def run(self, tool_input: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("flaky echo")
        return {"echo": tool_input}


class DaemonQueueTest(unittest.TestCase):
    def _policy_path(self) -> str:
        repo_root = Path(__file__).resolve().parents[1]
        return str(repo_root / "policy" / "readonly.json")

    def test_enqueue_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            first = state_store.enqueue_command("echo: first")
            second = state_store.enqueue_command("echo: second")

            claimed = state_store.claim_next_queue_item()
            assert claimed is not None
            self.assertEqual(claimed.id, first.id)
            self.assertEqual(claimed.status, QueueStatus.IN_PROGRESS)

            claimed_second = state_store.claim_next_queue_item()
            assert claimed_second is not None
            self.assertEqual(claimed_second.id, second.id)
            self.assertEqual(claimed_second.status, QueueStatus.IN_PROGRESS)
            self.assertIsNone(state_store.claim_next_queue_item())

    def test_daemon_once_executes_one_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            item = state_store.enqueue_command("echo: hi")

            daemon_module.run_daemon_loop(
                state_store,
                policy_path=self._policy_path(),
                sleep_seconds=0.0,
                once=True,
            )

            updated = state_store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.SUCCEEDED)

            run = state_store.get_latest_run()
            assert run is not None
            self.assertEqual(run.metadata_json["queue_item_id"], item.id)
            tasks = list(state_store.list_tasks(run.id))
            tool_calls = list(state_store.list_tool_calls(run.id))
            self.assertGreaterEqual(len(tasks), 1)
            self.assertGreaterEqual(len(tool_calls), 1)

    def test_retry_behavior(self) -> None:
        flaky_tool = FlakyEchoTool()

        def _build_flaky_registry(state_store: StateStore, policy) -> ToolRegistry:
            registry = ToolRegistry()
            registry.register(flaky_tool)
            return registry

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            item = state_store.enqueue_command("echo: retry", max_attempts=3)

            original_factory = daemon_module.build_registry
            try:
                daemon_module.build_registry = _build_flaky_registry
                daemon_module.run_daemon_loop(
                    state_store,
                    policy_path=self._policy_path(),
                    sleep_seconds=0.0,
                    once=True,
                )
            finally:
                daemon_module.build_registry = original_factory

            updated = state_store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.SUCCEEDED)
            self.assertEqual(updated.attempt_count, 1)

    def test_non_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            item = state_store.enqueue_command("note: forbidden")

            daemon_module.run_daemon_loop(
                state_store,
                policy_path=self._policy_path(),
                sleep_seconds=0.0,
                once=True,
            )

            updated = state_store.get_queue_item(item.id)
            assert updated is not None
            self.assertEqual(updated.status, QueueStatus.FAILED)
            self.assertEqual(updated.attempt_count, 0)


if __name__ == "__main__":
    unittest.main()
