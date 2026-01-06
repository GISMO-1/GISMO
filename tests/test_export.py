import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.agent import SimpleAgent
from gismo.core.export import export_latest_run_jsonl, export_run_jsonl
from gismo.core.models import EVENT_TYPE_LLM_PLAN, ToolCall, ToolCallStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry
from gismo.memory.store import record_event as memory_record_event


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

    def test_export_includes_agent_role_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            run = state_store.create_run(
                label="role-export",
                metadata={
                    "agent_role": {
                        "role_id": "role-123",
                        "role_name": "planner",
                        "memory_profile_id": "profile-123",
                    }
                },
            )
            output_path = export_run_jsonl(state_store, run.id, base_dir=Path(tmpdir))
            records = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            run_record = records[0]
            metadata = run_record["metadata"].get("agent_role", {})
            self.assertEqual(metadata.get("role_id"), "role-123")

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

    def test_export_includes_memory_provenance_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            state_store = StateStore(db_path)
            plan_event_id = str(uuid4())
            run = state_store.create_run(
                label="memory-export",
                metadata={"plan_event_id": plan_event_id},
            )
            payload = {
                "plan": {
                    "memory_suggestions": [
                        {
                            "namespace": "global",
                            "key": "default_model",
                            "kind": "preference",
                            "confidence": "high",
                            "source": "llm",
                        }
                    ]
                },
                "memory_injection_enabled": True,
                "memory_injected_count": 1,
                "memory_injected_keys": [{"namespace": "global", "key": "default_model"}],
                "memory_injected_bytes": 128,
                "memory_injected_cap_items": 20,
                "memory_injected_cap_bytes": 8192,
                "apply_memory_suggestions_requested": False,
                "apply_memory_suggestions_result": {"applied": 0, "skipped": 0, "denied": 0},
                "apply_memory_suggestions_applied": [],
                "apply_memory_policy_path": None,
                "apply_memory_yes": False,
                "apply_memory_non_interactive": True,
                "apply_memory_decision_path": "non-interactive",
            }
            state_store.record_event(
                actor="agent",
                event_type=EVENT_TYPE_LLM_PLAN,
                message="LLM plan generated.",
                json_payload=payload,
                event_id=plan_event_id,
            )
            memory_record_event(
                db_path,
                operation="put",
                actor="agent",
                policy_hash="test-hash",
                request={
                    "namespace": "global",
                    "key": "default_model",
                    "kind": "preference",
                    "value_json": "\"phi3:mini\"",
                    "tags_json": None,
                    "confidence": "high",
                    "source": "llm",
                    "ttl_seconds": None,
                },
                result_meta={
                    "policy_decision": "denied",
                    "policy_reason": "confirmation_required",
                    "confirmation": {"required": True, "provided": False, "mode": None},
                },
                related_ask_event_id=plan_event_id,
            )
            output_path = export_run_jsonl(
                state_store,
                run.id,
                base_dir=Path(tmpdir),
            )
            records = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            run_record = next(record for record in records if record["record_type"] == "run")
            self.assertIn("memory_provenance", run_record)
            event_record = next(record for record in records if record["record_type"] == "event")
            self.assertIn("memory_provenance", event_record)
            memory_event = next(
                record for record in records if record["record_type"] == "memory_event"
            )
            self.assertEqual(memory_event["originating_event_id"], plan_event_id)
            self.assertEqual(memory_event["policy_decision"], "denied")


if __name__ == "__main__":
    unittest.main()
