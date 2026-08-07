import shutil
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.agent import SimpleAgent
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import NetworkPolicy, NetworkRule, PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.toolpacks.device_tool import DeviceControlTool
from gismo.core.tools import ToolRegistry


class NetworkPolicyTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_device_control_denied_without_network_scope(self) -> None:
        tmpdir = self._tmpdir("network-deny")
        try:
            state_store = StateStore(str(tmpdir / "state.db"))
            network_policy = NetworkPolicy(default_action="deny", components={})
            registry = ToolRegistry()
            registry.register(DeviceControlTool(state_store, network_policy=network_policy))
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(
                    allowed_tools={"device_control"},
                    network=network_policy,
                ),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="device", metadata={"source": "test"})
            task = state_store.create_task(
                run_id=run.id,
                title="Scan",
                description="Scan",
                input_json={"tool": "device_control", "payload": {"action": "scan", "target": "network"}},
            )

            with mock.patch("gismo.web.api.scan_devices", return_value=[]):
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {"action": "scan", "target": "network"},
                )

            receipt = list(state_store.list_tool_receipts(run.id))[0]
            events = state_store.list_security_events(limit=10, event_type="outbound_denied")
            self.assertEqual(result.failure_type.value, "PERMISSION_DENIED")
            self.assertEqual(
                receipt.policy_snapshot["network_decision"]["reason"],
                "network egress denied by default",
            )
            self.assertTrue(events)
            self.assertEqual(events[0].payload["component"], "device_control")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_device_control_allows_private_scope_when_configured(self) -> None:
        tmpdir = self._tmpdir("network-allow")
        try:
            state_store = StateStore(str(tmpdir / "state.db"))
            network_policy = NetworkPolicy(
                default_action="deny",
                components={"device_control": NetworkRule(allow_private=True)},
            )
            registry = ToolRegistry()
            registry.register(DeviceControlTool(state_store, network_policy=network_policy))
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(
                    allowed_tools={"device_control"},
                    network=network_policy,
                ),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="device", metadata={"source": "test"})
            task = state_store.create_task(
                run_id=run.id,
                title="Scan",
                description="Scan",
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
            ):
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {"action": "scan", "target": "network"},
                )

            self.assertEqual(result.status.value, "SUCCEEDED")
            events = state_store.list_security_events(limit=20, event_type="outbound_allowed")
            self.assertTrue(events)
            self.assertEqual(events[0].payload["component"], "device_control")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
