import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.core.agent import SimpleAgent
from gismo.core.export import export_run_jsonl
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tool_receipts import canonical_json, redact_payload, sha256_payload
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool


class ToolReceiptTest(unittest.TestCase):
    def _build_orchestrator(self, db_path: str, policy: PermissionPolicy) -> Orchestrator:
        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(WriteNoteTool(state_store))
        agent = SimpleAgent(registry=registry)
        return Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=policy,
            agent=agent,
        )

    def test_receipt_records_success_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy = PermissionPolicy(allowed_tools={"echo"})
            orchestrator = self._build_orchestrator(db_path, policy)
            state_store = orchestrator.state_store
            run = state_store.create_run(label="receipts", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )

            orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})
            receipts = list(state_store.list_tool_receipts(run.id))
            state_store.close()

            self.assertEqual(len(receipts), 1)
            receipt = receipts[0]
            expected_request = canonical_json(redact_payload({"message": "hi"}))
            expected_response = canonical_json(
                redact_payload({"echo": {"message": "hi"}})
            )
            self.assertEqual(receipt.request_payload_json, expected_request)
            self.assertEqual(receipt.request_sha256, sha256_payload(expected_request))
            self.assertEqual(receipt.response_payload_json, expected_response)
            self.assertEqual(receipt.response_sha256, sha256_payload(expected_response))

    def test_receipt_redaction_is_deterministic(self) -> None:
        payload = {
            "api_key": "secret",
            "nested": {"token": "hidden", "value": "ok"},
            "list": [{"authorization": "bearer"}],
        }
        first = redact_payload(payload)
        second = redact_payload(payload)
        self.assertEqual(first, second)
        expected = {
            "api_key": "[REDACTED]",
            "nested": {"token": "[REDACTED]", "value": "ok"},
            "list": [{"authorization": "[REDACTED]"}],
        }
        self.assertEqual(first, expected)
        first_json = canonical_json(first)
        second_json = canonical_json(second)
        self.assertEqual(first_json, second_json)
        self.assertEqual(sha256_payload(first_json), sha256_payload(second_json))

    def test_receipt_records_policy_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            policy = PermissionPolicy(allowed_tools=set())
            orchestrator = self._build_orchestrator(db_path, policy)
            state_store = orchestrator.state_store
            run = state_store.create_run(label="policy", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )

            orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})
            receipts = list(state_store.list_tool_receipts(run.id))
            state_store.close()

            self.assertEqual(len(receipts), 1)
            receipt = receipts[0]
            self.assertEqual(receipt.status.value, "error")
            self.assertIsNotNone(receipt.policy_snapshot)
            self.assertFalse(receipt.policy_snapshot["allowed"])


class ToolReceiptReplayCLITest(unittest.TestCase):
    def test_replay_detects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "state.db"
            policy = PermissionPolicy(allowed_tools={"echo"})
            state_store = StateStore(str(db_path))
            registry = ToolRegistry()
            registry.register(EchoTool())
            agent = SimpleAgent(registry=registry)
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=policy,
                agent=agent,
            )
            run = state_store.create_run(label="replay", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Echo",
                input_json={"tool": "echo", "payload": {"message": "hi"}},
            )
            orchestrator.run_tool(run.id, task, "echo", {"message": "hi"})
            export_path = export_run_jsonl(state_store, run.id, base_dir=tmp_path)
            state_store.close()

            cmd = [
                sys.executable,
                "-m",
                "gismo.cli.main",
                "tools",
                "replay",
                "--db",
                str(db_path),
                "--run",
                run.id,
                "--from-export",
                str(export_path),
            ]
            ok_proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)

            lines = export_path.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines if line.strip()]
            for record in records:
                if record.get("record_type") == "tool_receipt":
                    response_payload = json.loads(record["response_payload_json"])
                    response_payload["tampered"] = True
                    response_payload_json = canonical_json(response_payload)
                    record["response_payload_json"] = response_payload_json
                    record["response_sha256"] = sha256_payload(response_payload_json)
                    break
            export_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            mismatch_proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(mismatch_proc.returncode, 2, mismatch_proc.stderr)
