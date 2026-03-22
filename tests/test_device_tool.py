import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4
import shutil

from gismo.core.agent import SimpleAgent
from gismo.core.models import ConnectedDevice, TaskStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.toolpacks.device_tool import DeviceControlTool
from gismo.core.tools import ToolRegistry
from gismo.core.trust_zones import EXECUTION_MODE_SANDBOXED


class DeviceToolTest(unittest.TestCase):
    def _build_orchestrator(self, db_path: str) -> tuple[StateStore, Orchestrator]:
        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(DeviceControlTool(state_store))
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=PermissionPolicy(allowed_tools={"device_control"}),
            agent=SimpleAgent(registry=registry),
        )
        return state_store, orchestrator

    def test_check_cameras_succeeds(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            now = datetime.now(timezone.utc)
            state_store.upsert_device(
                ConnectedDevice(
                    ip="192.168.1.25",
                    hostname="Front Door",
                    device_type="camera",
                    brand="Tapo",
                    metadata_json={"label": "Front Door", "open_ports": [554]},
                    created_at=now,
                )
            )
            run = state_store.create_run(label="device-check", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Check cameras",
                description="Check saved cameras",
                input_json={"tool": "device_control", "payload": {"action": "check", "target": "cameras"}},
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {
                            "id": "camera-1",
                            "ip": "192.168.1.25",
                            "name": "Front Door",
                            "device_type": "camera",
                            "brand": "Tapo",
                            "status": "online",
                            "actions": ["check", "view"],
                        }
                    ],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                result = orchestrator.run_tool(run.id, task, "device_control", {"action": "check", "target": "cameras"})
            events = state_store.list_events(limit=10)
            receipts = list(state_store.list_tool_receipts(run.id))

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("I checked 1 camera", result.output_json.get("summary", ""))
            self.assertEqual(result.output_json["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            runtime.assert_called_once()
            self.assertEqual(receipts[0].policy_snapshot["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertEqual(receipts[0].policy_snapshot["execution"]["zone"], "device_adapter")
            self.assertTrue(any(event.event_type == "device_check" for event in events))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_turn_on_light_without_local_details_returns_setup_message(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            now = datetime.now(timezone.utc)
            state_store.upsert_device(
                ConnectedDevice(
                    ip="192.168.1.40",
                    hostname="Kitchen Lamp",
                    device_type="light",
                    brand="FEIT",
                    metadata_json={"label": "Kitchen Lamp", "open_ports": [6668]},
                    created_at=now,
                )
            )
            run = state_store.create_run(label="device-power", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Turn on lamp",
                description="Turn on light",
                input_json={"tool": "device_control", "payload": {"action": "turn_on", "target": "kitchen lamp"}},
            )
            result = orchestrator.run_tool(
                run.id,
                task,
                "device_control",
                {"action": "turn_on", "target": "kitchen lamp"},
            )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("needs a little more setup", result.output_json.get("summary", ""))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_scan_network_uses_existing_scan_logic(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="device-scan", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Scan network",
                description="Find devices",
                input_json={"tool": "device_control", "payload": {"action": "scan", "target": "network"}},
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {"ip": "192.168.1.9", "hostname": "desk-lamp", "device_type": "light"},
                    ],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {"action": "scan", "target": "network"},
                )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("I found 1 device", result.output_json.get("summary", ""))
            self.assertEqual(result.output_json["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            runtime.assert_called_once()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
