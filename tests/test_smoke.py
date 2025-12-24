import tempfile
import unittest
from pathlib import Path

from gismo.core.agent import SimpleAgent
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool


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


if __name__ == "__main__":
    unittest.main()
