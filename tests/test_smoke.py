import tempfile
import unittest
from pathlib import Path

from gismo.cli.operator import make_idempotency_key, normalize_command, parse_command, required_tools
from gismo.core.agent import SimpleAgent
from gismo.core.models import FailureType, TaskStatus, ToolCallStatus
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


class AlwaysFailTool(Tool):
    def __init__(self) -> None:
        super().__init__(name="always_fail", description="Always fails")

    def run(self, tool_input: dict) -> dict:
        raise ValueError("planned failure")


def run_operator_plan(
    state_store: StateStore,
    orchestrator: Orchestrator,
    plan: dict,
    normalized_command: str,
) -> tuple[str, list[str]]:
    run = state_store.create_run(label="operator-test", metadata={"command": normalized_command})
    created_task_ids = []
    previous_task_id = None
    for index, step in enumerate(plan["steps"]):
        tool_name = step["tool_name"]
        tool_input = step["input_json"]
        idempotency_key = make_idempotency_key(step, normalized_command, index)
        depends_on = [previous_task_id] if plan["mode"] == "graph" and previous_task_id else None
        task = state_store.create_task(
            run_id=run.id,
            title=step["title"],
            description="Operator test step",
            input_json={"tool": tool_name, "payload": tool_input},
            depends_on=depends_on,
            idempotency_key=idempotency_key,
        )
        created_task_ids.append(task.id)
        previous_task_id = task.id

    if plan["mode"] == "single":
        task = state_store.get_task(created_task_ids[0])
        assert task is not None
        orchestrator.run_tool(run.id, task, task.input_json["tool"], task.input_json["payload"])
    else:
        orchestrator.run_task_graph(run.id)

    return run.id, created_task_ids


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

    def test_task_graph_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(EchoTool())
            registry.register(WriteNoteTool(state_store))
            policy = PermissionPolicy(allowed_tools={"echo", "write_note"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="graph", metadata={})
            task_a = state_store.create_task(
                run_id=run.id,
                title="A",
                description="Echo A",
                input_json={"tool": "echo", "payload": {"message": "A"}},
            )
            task_b = state_store.create_task(
                run_id=run.id,
                title="B",
                description="Note B",
                input_json={"tool": "write_note", "payload": {"note": "B"}},
                depends_on=[task_a.id],
            )
            task_c = state_store.create_task(
                run_id=run.id,
                title="C",
                description="Echo C",
                input_json={"tool": "echo", "payload": {"message": "C"}},
                depends_on=[task_b.id],
            )

            orchestrator.run_task_graph(run.id)

            tasks = {task.id: task for task in state_store.list_tasks(run.id)}
            self.assertEqual(tasks[task_a.id].status, TaskStatus.SUCCEEDED)
            self.assertEqual(tasks[task_b.id].status, TaskStatus.SUCCEEDED)
            self.assertEqual(tasks[task_c.id].status, TaskStatus.SUCCEEDED)

            tool_calls = list(state_store.list_tool_calls(run.id))
            self.assertEqual([call.task_id for call in tool_calls], [task_a.id, task_b.id, task_c.id])

    def test_task_graph_dependency_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(AlwaysFailTool())
            policy = PermissionPolicy(allowed_tools={"always_fail"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="dep-fail", metadata={})
            task_a = state_store.create_task(
                run_id=run.id,
                title="A",
                description="Failing A",
                input_json={"tool": "always_fail", "payload": {}},
            )
            task_b = state_store.create_task(
                run_id=run.id,
                title="B",
                description="Blocked B",
                input_json={"tool": "always_fail", "payload": {}},
                depends_on=[task_a.id],
            )

            orchestrator.run_task_graph(run.id)

            updated_a = state_store.get_task(task_a.id)
            updated_b = state_store.get_task(task_b.id)
            assert updated_a is not None
            assert updated_b is not None
            self.assertEqual(updated_a.status, TaskStatus.FAILED)
            self.assertEqual(updated_a.failure_type, FailureType.INVALID_INPUT)
            self.assertEqual(updated_b.status, TaskStatus.FAILED)
            self.assertEqual(updated_b.failure_type, FailureType.SYSTEM_ERROR)
            self.assertIn(task_a.id, updated_b.error or "")
            tool_calls_b = list(state_store.list_tool_calls_for_task(task_b.id))
            self.assertEqual(tool_calls_b, [])

    def test_task_graph_deadlock_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(EchoTool())
            policy = PermissionPolicy(allowed_tools={"echo"})
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            run = state_store.create_run(label="deadlock", metadata={})
            task_a = state_store.create_task(
                run_id=run.id,
                title="A",
                description="A",
                input_json={"tool": "echo", "payload": {"message": "A"}},
            )
            task_b = state_store.create_task(
                run_id=run.id,
                title="B",
                description="B",
                input_json={"tool": "echo", "payload": {"message": "B"}},
                depends_on=[task_a.id],
            )
            task_a.depends_on = [task_b.id]
            state_store.update_task(task_a)

            orchestrator.run_task_graph(run.id)

            updated_a = state_store.get_task(task_a.id)
            updated_b = state_store.get_task(task_b.id)
            assert updated_a is not None
            assert updated_b is not None
            self.assertEqual(updated_a.status, TaskStatus.FAILED)
            self.assertEqual(updated_b.status, TaskStatus.FAILED)
            self.assertIn("Deadlock/cycle detected", updated_a.error or "")
            self.assertIn("Deadlock/cycle detected", updated_b.error or "")

    def test_operator_run_echo(self) -> None:
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

            command = "echo: hello operator"
            plan = parse_command(command)
            normalized = normalize_command(command)
            policy.allowed_tools = required_tools(plan)
            run_id, task_ids = run_operator_plan(state_store, orchestrator, plan, normalized)

            tasks = {task.id: task for task in state_store.list_tasks(run_id)}
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[task_ids[0]].status, TaskStatus.SUCCEEDED)
            tool_calls = list(state_store.list_tool_calls(run_id))
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].status, ToolCallStatus.SUCCEEDED)

    def test_operator_run_note_permissions(self) -> None:
        plan = parse_command("note: keep this")
        tools = required_tools(plan)
        self.assertEqual(tools, {"write_note"})
        policy = PermissionPolicy(allowed_tools=tools)
        policy.check_tool_allowed("write_note")
        with self.assertRaises(PermissionError):
            policy.check_tool_allowed("echo")

    def test_operator_run_graph_creates_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(EchoTool())
            registry.register(WriteNoteTool(state_store))
            policy = PermissionPolicy(allowed_tools=set())
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            command = "graph: echo A -> note B -> echo C"
            plan = parse_command(command)
            normalized = normalize_command(command)
            policy.allowed_tools = required_tools(plan)
            run_id, task_ids = run_operator_plan(state_store, orchestrator, plan, normalized)

            tasks = {task.id: task for task in state_store.list_tasks(run_id)}
            self.assertEqual(len(tasks), 3)
            self.assertEqual(tasks[task_ids[0]].depends_on, [])
            self.assertEqual(tasks[task_ids[1]].depends_on, [task_ids[0]])
            self.assertEqual(tasks[task_ids[2]].depends_on, [task_ids[1]])
            self.assertEqual(tasks[task_ids[0]].status, TaskStatus.SUCCEEDED)
            self.assertEqual(tasks[task_ids[1]].status, TaskStatus.SUCCEEDED)
            self.assertEqual(tasks[task_ids[2]].status, TaskStatus.SUCCEEDED)

    def test_operator_idempotency_repeat(self) -> None:
        class CountingEchoTool(Tool):
            def __init__(self) -> None:
                super().__init__(name="echo", description="Counts echo invocations")
                self.calls = 0

            def run(self, tool_input: dict) -> dict:
                self.calls += 1
                return {"count": self.calls}

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            counting = CountingEchoTool()
            registry.register(counting)
            policy = PermissionPolicy(allowed_tools=set())
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )

            command = "echo: repeat"
            plan = parse_command(command)
            normalized = normalize_command(command)
            policy.allowed_tools = required_tools(plan)

            run = state_store.create_run(label="operator-idempotency", metadata={})
            step = plan["steps"][0]
            idempotency_key = make_idempotency_key(step, normalized, 0)
            first_task = state_store.create_task(
                run_id=run.id,
                title=step["title"],
                description="First operator run",
                input_json={"tool": "echo", "payload": step["input_json"]},
                idempotency_key=idempotency_key,
            )
            orchestrator.run_tool(run.id, first_task, "echo", step["input_json"])

            second_task = state_store.create_task(
                run_id=run.id,
                title=step["title"],
                description="Second operator run",
                input_json={"tool": "echo", "payload": step["input_json"]},
                idempotency_key=idempotency_key,
            )
            result = orchestrator.run_tool(run.id, second_task, "echo", step["input_json"])

            self.assertEqual(counting.calls, 1)
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            tool_calls = list(state_store.list_tool_calls_for_task(second_task.id))
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].status, ToolCallStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
