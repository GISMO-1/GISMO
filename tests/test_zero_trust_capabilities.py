import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core import daemon as daemon_module
from gismo.core.agent import SimpleAgent
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry


class ZeroTrustCapabilityTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_receipt_includes_capability_summary(self) -> None:
        tmpdir = self._tmpdir("capability-receipt")
        try:
            state_store = StateStore(str(tmpdir / "state.db"))
            registry = ToolRegistry()
            registry.register(EchoTool())
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(allowed_tools={"echo"}),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="capability", metadata={"source": "test"})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )

            result = orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})
            receipt = list(state_store.list_tool_receipts(run.id))[0]
            events = state_store.list_security_events(limit=10)

            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertIsNotNone(receipt.capability_id)
            self.assertIsNotNone(receipt.capability_summary)
            self.assertTrue(receipt.capability_summary["valid"])
            self.assertEqual(receipt.capability_summary["subject"], "test")
            self.assertIn("capability_issued", [event.event_type for event in events])
            self.assertIn("capability_verified", [event.event_type for event in events])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tampered_capability_is_rejected(self) -> None:
        tmpdir = self._tmpdir("capability-tamper")
        try:
            state_store = StateStore(str(tmpdir / "state.db"))
            registry = ToolRegistry()
            registry.register(EchoTool())
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(allowed_tools={"echo"}),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="capability", metadata={"source": "test"})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )
            assert task.capability_token is not None
            task.capability_token = task.capability_token[:-2] + "aa"
            state_store.update_task(task)

            result = orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})
            receipt = list(state_store.list_tool_receipts(run.id))[0]
            events = state_store.list_security_events(limit=10, event_type="capability_rejected")

            self.assertEqual(result.status.value, "FAILED")
            self.assertEqual(result.failure_type.value, "PERMISSION_DENIED")
            self.assertFalse(receipt.capability_summary["valid"])
            self.assertEqual(len(events), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_queue_approval_id_flows_into_capability(self) -> None:
        tmpdir = self._tmpdir("capability-approval")
        try:
            state_store = StateStore(str(tmpdir / "state.db"))
            state_store.enqueue_command("echo: hello", metadata={"approval_id": "plan-123"})
            item = state_store.claim_next_queue_item()
            assert item is not None

            daemon_module._run_queue_item_plan(
                state_store,
                item,
                policy_path=None,
                repo_root=Path(__file__).resolve().parents[1],
                registry_factory=None,
            )

            run = list(state_store.list_runs(limit=5))[0]
            receipt = list(state_store.list_tool_receipts(run.id))[0]
            self.assertEqual(run.metadata_json["approval_id"], "plan-123")
            self.assertEqual(receipt.capability_summary["approval_id"], "plan-123")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
