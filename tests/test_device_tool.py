import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4
import shutil

from gismo.core.agent import SimpleAgent
from gismo.core.execution import ExecutionBoundaryError
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

    def _write_devices_config(self, root: Path, devices: list[dict]) -> None:
        config_dir = root / ".gismo"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "devices.json").write_text(json.dumps({"devices": devices}), encoding="utf-8")

    def _saved_light(self, device_id: str, name: str, ip: str) -> ConnectedDevice:
        return ConnectedDevice(
            id=device_id,
            ip=ip,
            hostname=name,
            device_type="light",
            brand="Tuya",
            metadata_json={"label": name, "gismo_device_id": device_id, "adapter": "tuya"},
            created_at=datetime.now(timezone.utc),
        )

    def _run_power(self, state_store: StateStore, orchestrator: Orchestrator, target: str):
        run = state_store.create_run(label="device-outcome", metadata={})
        task = state_store.create_task(
            run_id=run.id,
            title="Control light",
            description="Test structured physical outcome",
            input_json={"tool": "device_control", "payload": {"action": "turn_on", "target": target}},
        )
        return orchestrator.run_tool(
            run.id,
            task,
            "device_control",
            {"action": "turn_on", "target": target},
        )

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

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("needs a little more setup", result.output_json.get("summary", ""))
            self.assertEqual(result.output_json["device_command_result"]["status"], "failed")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_color_temp_routes_targeted_light_command_through_runtime(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            now = datetime.now(timezone.utc)
            state_store.upsert_device(
                ConnectedDevice(
                    id="dads-room-light",
                    ip="192.168.1.188",
                    hostname="dad-room-light",
                    device_type="light",
                    brand="FEIT",
                    metadata_json={
                        "label": "Dad's Room Light",
                        "gismo_device_id": "dads-room-light",
                        "device_id": "tuya-bulb-1",
                        "adapter": "tuya",
                        "open_ports": [6668],
                    },
                    created_at=now,
                )
            )
            run = state_store.create_run(label="device-color-temp", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Set Dad's light to cool white",
                description="Set saved light color temperature",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "set_color_temp",
                        "target": "Dad's Room Light",
                        "params": {"preset": "cool_white"},
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {
                            "id": "dads-room-light",
                            "ip": "192.168.1.188",
                            "name": "Dad's Room Light",
                            "device_type": "light",
                            "brand": "FEIT",
                            "status": "online",
                            "actions": ["check", "turn_on", "turn_off"],
                        }
                    ],
                    "changed": ["Dad's Room Light"],
                    "needs_setup": [],
                    "failed": [],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "set_color_temp",
                        "target": "Dad's Room Light",
                        "params": {"preset": "cool_white"},
                    },
                )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED, result.error)
            self.assertIn("cool white", result.output_json.get("summary", "").lower())
            self.assertEqual(result.output_json["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertEqual(runtime.call_args.kwargs["action"], "device_target_command")
            self.assertEqual(runtime.call_args.kwargs["payload"]["command"], "set_color_temp")
            self.assertEqual(runtime.call_args.kwargs["payload"]["params"], {"preset": "cool_white"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_brightness_matches_saved_device_alias_and_uses_target_command(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            now = datetime.now(timezone.utc)
            state_store.upsert_device(
                ConnectedDevice(
                    id="dads-room-light",
                    ip="192.168.1.188",
                    hostname="dad-room-light",
                    device_type="light",
                    brand="FEIT",
                    metadata_json={
                        "label": "Dad's Room Light",
                        "gismo_device_id": "dads-room-light",
                        "device_id": "tuya-bulb-1",
                        "adapter": "tuya",
                        "open_ports": [6668],
                    },
                    created_at=now,
                )
            )
            run = state_store.create_run(label="device-brightness", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Dim Dad's light",
                description="Set brightness",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "set_brightness",
                        "target": "dads-room-light",
                        "params": {"brightness": 20},
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {
                            "id": "dads-room-light",
                            "ip": "192.168.1.188",
                            "name": "Dad's Room Light",
                            "device_type": "light",
                            "brand": "FEIT",
                            "status": "online",
                            "actions": ["check", "turn_on", "turn_off"],
                        }
                    ],
                    "changed": ["Dad's Room Light"],
                    "needs_setup": [],
                    "failed": [],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "set_brightness",
                        "target": "dads-room-light",
                        "params": {"brightness": 20},
                    },
                )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("brightness to 20%", result.output_json.get("summary", "").lower())
            self.assertEqual(runtime.call_args.kwargs["action"], "device_target_command")
            self.assertEqual(runtime.call_args.kwargs["payload"]["command"], "set_brightness")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_turn_off_saved_configured_light_without_connected_device_uses_config_lookup(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            self._write_devices_config(
                tmpdir,
                [
                    {
                        "gismo_device_id": "dads-room-light",
                        "name": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "local_key": "SECRET-DO-NOT-STORE",
                        "ip": "192.168.1.188",
                        "version": "3.3",
                        "platform": "tuya",
                        "device_type": "light",
                    }
                ],
            )
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="device-config-light", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Turn off Dad's light",
                description="Turn off saved configured light",
                input_json={"tool": "device_control", "payload": {"action": "turn_off", "target": "Dad's Room Light"}},
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {
                            "id": "dads-room-light",
                            "ip": "192.168.1.188",
                            "name": "Dad's Room Light",
                            "device_type": "light",
                            "brand": "Tuya",
                            "status": "online",
                            "actions": ["check", "turn_on", "turn_off"],
                        }
                    ],
                    "changed": ["Dad's Room Light"],
                    "needs_setup": [],
                    "failed": [],
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {"action": "turn_off", "target": "Dad's Room Light"},
                )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED, result.error)
            self.assertIn("turned off", result.output_json.get("summary", "").lower())
            payload_device = runtime.call_args.kwargs["payload"]["devices"][0]
            self.assertEqual(payload_device["id"], "dads-room-light")
            self.assertEqual(payload_device["ip"], "192.168.1.188")
            self.assertNotIn("local_key", payload_device.get("metadata_json", {}))
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

    def test_adapter_rejection_marks_task_failed(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            state_store, orchestrator = self._build_orchestrator(str(tmpdir / "state.db"))
            state_store.upsert_device(self._saved_light("lamp-1", "Desk Lamp", "192.168.1.51"))
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [{"id": "lamp-1", "name": "Desk Lamp", "control": "failed"}],
                    "changed": [],
                    "failed": ["Desk Lamp: adapter rejected command"],
                },
            ):
                result = self._run_power(state_store, orchestrator, "lamp-1")
            self.assertEqual(result.status, TaskStatus.FAILED)
            outcome = result.output_json["device_command_result"]
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(outcome["succeeded"], [])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_execution_boundary_timeout_marks_task_failed(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            state_store, orchestrator = self._build_orchestrator(str(tmpdir / "state.db"))
            state_store.upsert_device(self._saved_light("lamp-1", "Desk Lamp", "192.168.1.51"))
            timeout = ExecutionBoundaryError("worker timeout", report=mock.Mock())
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                side_effect=timeout,
            ):
                result = self._run_power(state_store, orchestrator, "lamp-1")
            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertEqual(result.output_json["device_command_result"]["status"], "failed")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_multi_device_execution_preserves_partial_result(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            state_store, orchestrator = self._build_orchestrator(str(tmpdir / "state.db"))
            state_store.upsert_device(self._saved_light("lamp-1", "Desk Lamp", "192.168.1.51"))
            state_store.upsert_device(self._saved_light("lamp-2", "Floor Lamp", "192.168.1.52"))
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [
                        {
                            "id": "lamp-1",
                            "name": "Desk Lamp",
                            "control": "changed",
                            "confirmed": True,
                            "verified_state": {"is_on": True},
                        },
                        {"id": "lamp-2", "name": "Floor Lamp", "control": "failed"},
                    ],
                    "changed": ["Desk Lamp"],
                    "failed": ["Floor Lamp: rejected"],
                },
            ):
                result = self._run_power(state_store, orchestrator, "lights")
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            outcome = result.output_json["device_command_result"]
            self.assertEqual(outcome["status"], "partial")
            self.assertEqual(outcome["succeeded"], ["lamp-1"])
            self.assertEqual(outcome["verified_state"], {"lamp-1": {"is_on": True}})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_verified_device_state_marks_physical_result_confirmed(self) -> None:
        tmpdir = Path("tmp") / f"device-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            state_store, orchestrator = self._build_orchestrator(str(tmpdir / "state.db"))
            state_store.upsert_device(self._saved_light("lamp-1", "Desk Lamp", "192.168.1.51"))
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "devices": [{
                        "id": "lamp-1",
                        "name": "Desk Lamp",
                        "control": "changed",
                        "confirmed": True,
                        "verified_state": {"is_on": True},
                    }],
                    "changed": ["Desk Lamp"],
                    "failed": [],
                },
            ):
                result = self._run_power(state_store, orchestrator, "lamp-1")
            outcome = result.output_json["device_command_result"]
            self.assertEqual(outcome["status"], "confirmed")
            self.assertTrue(outcome["physical_result_confirmed"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
