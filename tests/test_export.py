import json
import tempfile
import unittest
from pathlib import Path

from gismo.core.agent import SimpleAgent
from gismo.core.export import export_latest_run_jsonl, export_run_jsonl
from gismo.core.models import ToolCall, ToolCallStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry


class ExportTest(unittest.TestCase):
    def _build_orchestrator(
        self, db_path: str, policy: PermissionPolicy
    ) -> tuple[StateStore, Orchestrator]:
        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(EchoTool())
        agent = SimpleAgent(registry=registry)
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=policy,
            agent=agent,
        )
        return state_store, orchestrator

    def test_export_by_run_id_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy = PermissionPolicy(allowed_tools={"echo"})
            state_store, orchestrator = self._build_orchestrator(db_path, policy)
            run = state_store.create_run(label="export", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )
            orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})

            output_path = export_run_jsonl(
                state_store,
                run.id,
                base_dir=Path(tmpdir),
            )

            self.assertTrue(output_path.exists())
            lines = output_path.read_text(encoding="utf-8").strip().splitlines()
            records = [json.loads(line) for line in lines]
            self.assertEqual(records[0]["record_type"], "run")
            self.assertEqual(records[0]["id"], run.id)
            record_types = [record["record_type"] for record in records]
            self.assertIn("task", record_types)
            self.assertIn("tool_call", record_types)
            self.assertGreater(record_types.index("tool_call"), record_types.index("task"))

    def test_export_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            run_one = state_store.create_run(label="first", metadata={})
            run_two = state_store.create_run(label="second", metadata={})

            output_path = export_latest_run_jsonl(state_store, base_dir=Path(tmpdir))

            self.assertIn(run_two.id, output_path.name)
            self.assertNotIn(run_one.id, output_path.name)

    def test_export_redact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            run = state_store.create_run(label="redact", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Write",
                description="Write file",
                input_json={"tool": "write_file", "payload": {"content": "secret"}},
            )
            large_payload = "x" * 2048
            task.mark_succeeded({"data": large_payload})
            state_store.update_task(task)

            tool_call = ToolCall(
                run_id=run.id,
                task_id=task.id,
                tool_name="run_shell",
                input_json={"command": ["echo", "secret"]},
                status=ToolCallStatus.SUCCEEDED,
                output_json={"stdout": "secret", "stderr": "oops"},
            )
            tool_call.finished_at = tool_call.started_at
            state_store.record_tool_call(tool_call)

            tool_call_large = ToolCall(
                run_id=run.id,
                task_id=task.id,
                tool_name="echo",
                input_json={"message": "large"},
                status=ToolCallStatus.SUCCEEDED,
                output_json={"blob": large_payload},
            )
            tool_call_large.finished_at = tool_call_large.started_at
            state_store.record_tool_call(tool_call_large)

            output_path = export_run_jsonl(
                state_store,
                run.id,
                base_dir=Path(tmpdir),
                redact=True,
            )

            records = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            task_record = next(record for record in records if record["record_type"] == "task")
            self.assertEqual(
                task_record["inputs"]["payload"]["content"],
                "[REDACTED]",
            )
            self.assertEqual(task_record["outputs"], "[REDACTED]")
            tool_records = [record for record in records if record["record_type"] == "tool_call"]
            stdout_record = next(record for record in tool_records if record["tool_name"] == "run_shell")
            self.assertEqual(stdout_record["outputs"]["stdout"], "[REDACTED]")
            self.assertEqual(stdout_record["outputs"]["stderr"], "[REDACTED]")
            large_record = next(record for record in tool_records if record["tool_name"] == "echo")
            self.assertEqual(large_record["outputs"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
