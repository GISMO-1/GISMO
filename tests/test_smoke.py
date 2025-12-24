import tempfile
import unittest
from pathlib import Path

from gismo.core.agent import SimpleAgent
from gismo.core.models import FailureType, ToolCallStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, Tool, ToolRegistry, WriteNoteTool


class FlakyTool(Tool):
    def __init__(self) -> None:
        super().__init__(name="flaky", description="Fails once then succeeds")
        self.calls = 0

    def run(self, tool_input: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("flaky failure")
        return {"ok": True}


class CountingTool(Tool):
    def __init__(self) -> None:
        super().__init__(name="counting", description="Counts invocations")
        self.calls = 0

    def run(self, tool_input: dict) -> dict:
        self.calls += 1
        return {"count": self.calls}


class SmokeTest(unittest.TestCase):
    def test_demo_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(EchoTool())
            registry.register(WriteNoteTool(state_store))
            policy = PermissionPolicy(allowed_tools={"echo"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="smoke", metadata={})

            echo_task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo payload",
                input_json={"tool": "echo", "payload": {"message": "ping"}},
            )
            orchestrator.run_tool(run.id, echo_task, "echo", {"message": "ping"})

            note_task = state_store.create_task(
                run_id=run.id,
                title="Write note",
                description="Write a note",
                input_json={"tool": "write_note", "payload": {"note": "hello"}},
            )
            orchestrator.run_tool(run.id, note_task, "write_note", {"note": "hello"})
            policy.allow("write_note")
            orchestrator.run_tool(run.id, note_task, "write_note", {"note": "hello"})

            tasks = list(state_store.list_tasks(run.id))
            self.assertEqual(len(tasks), 2)
            task_statuses = {task.title: task.status for task in tasks}
            self.assertEqual(task_statuses["Echo"].value, "SUCCEEDED")
            self.assertEqual(task_statuses["Write note"].value, "SUCCEEDED")

            tool_calls = list(state_store.list_tool_calls(run.id))
            self.assertEqual(len(tool_calls), 3)
            statuses = [call.status.value for call in tool_calls]
            self.assertIn("FAILED", statuses)
            self.assertIn("SUCCEEDED", statuses)

    def test_permission_denied_failure_type_no_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(EchoTool())
            policy = PermissionPolicy(allowed_tools=set())
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="permission", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Denied",
                description="Permission denied",
                input_json={"tool": "echo", "payload": {"message": "nope"}},
            )

            result = orchestrator.run_tool(
                run.id,
                task,
                "echo",
                {"message": "nope"},
                max_attempts=3,
            )

            self.assertEqual(result.status.value, "FAILED")
            self.assertEqual(result.failure_type, FailureType.PERMISSION_DENIED)
            tool_calls = list(state_store.list_tool_calls_for_task(task.id))
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].attempt_number, 1)
            self.assertEqual(tool_calls[0].failure_type, FailureType.PERMISSION_DENIED)

    def test_retry_flaky_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            flaky = FlakyTool()
            registry.register(flaky)
            policy = PermissionPolicy(allowed_tools={"flaky"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="retry", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Flaky",
                description="Retry tool",
                input_json={"tool": "flaky", "payload": {}},
            )

            result = orchestrator.run_tool(
                run.id,
                task,
                "flaky",
                {},
                max_attempts=2,
                backoff_base_seconds=0.0,
            )

            self.assertEqual(result.status.value, "SUCCEEDED")
            tool_calls = list(state_store.list_tool_calls_for_task(task.id))
            self.assertEqual(len(tool_calls), 2)
            self.assertEqual([call.attempt_number for call in tool_calls], [1, 2])
            self.assertEqual(tool_calls[0].status, ToolCallStatus.FAILED)
            self.assertEqual(tool_calls[0].failure_type, FailureType.TOOL_ERROR)
            self.assertEqual(tool_calls[1].status, ToolCallStatus.SUCCEEDED)

    def test_idempotency_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            counting = CountingTool()
            registry.register(counting)
            policy = PermissionPolicy(allowed_tools={"counting"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="idempotency", metadata={})
            first_task = state_store.create_task(
                run_id=run.id,
                title="First",
                description="First run",
                input_json={"tool": "counting", "payload": {"value": 1}},
                idempotency_key="abc123",
            )
            orchestrator.run_tool(run.id, first_task, "counting", {"value": 1})

            second_task = state_store.create_task(
                run_id=run.id,
                title="Second",
                description="Second run",
                input_json={"tool": "counting", "payload": {"value": 1}},
                idempotency_key="abc123",
            )
            result = orchestrator.run_tool(run.id, second_task, "counting", {"value": 1})

            self.assertEqual(counting.calls, 1)
            self.assertEqual(result.status.value, "SUCCEEDED")
            tool_calls = list(state_store.list_tool_calls_for_task(second_task.id))
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].status, ToolCallStatus.SKIPPED)
            self.assertEqual(tool_calls[0].failure_type, FailureType.NONE)


if __name__ == "__main__":
    unittest.main()
