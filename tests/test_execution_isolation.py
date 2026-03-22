import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.agent import SimpleAgent
from gismo.core.execution import (
    ExecutionBoundaryError,
    TrustZoneViolationError,
    build_execution_request,
    build_worker_command,
    run_isolated_process,
    run_sandboxed_process,
)
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import NetworkPolicy, NetworkRule, PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool
from gismo.core.tools import ToolRegistry
from gismo.core.trust_zones import (
    EXECUTION_MODE_IN_PROCESS,
    EXECUTION_MODE_ISOLATED_SUBPROCESS,
    EXECUTION_MODE_SANDBOXED,
    ZONE_MODEL_RUNTIME,
    ZONE_TOOL_EXECUTION,
    get_component_binding,
)
from gismo.llm import ollama


class DummyResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class ExecutionIsolationTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_trust_zone_mapping_exposes_real_component_bindings(self) -> None:
        shell_binding = get_component_binding("run_shell")
        self.assertEqual(shell_binding.zone, ZONE_TOOL_EXECUTION)
        self.assertEqual(shell_binding.default_execution_mode, EXECUTION_MODE_ISOLATED_SUBPROCESS)

        ollama_binding = get_component_binding("ollama_client")
        self.assertEqual(ollama_binding.zone, ZONE_MODEL_RUNTIME)
        self.assertEqual(ollama_binding.default_execution_mode, EXECUTION_MODE_SANDBOXED)

    def test_run_shell_uses_isolated_subprocess_and_records_receipt_context(self) -> None:
        tmpdir = self._tmpdir("exec-shell")
        state_store = None
        try:
            db_path = str(tmpdir / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(
                ShellTool(
                    ShellConfig(
                        base_dir=tmpdir,
                        allowlist=[["echo", "hello"]],
                        timeout_seconds=5,
                    )
                )
            )
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(allowed_tools={"run_shell"}),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="shell", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Echo",
                description="Run shell echo",
                input_json={"tool": "run_shell", "payload": {"command": ["echo", "hello"]}},
            )

            result = orchestrator.run_tool(run.id, task, "run_shell", {"command": ["echo", "hello"]})
            receipt = list(state_store.list_tool_receipts(run.id))[0]
            events = state_store.list_security_events(limit=20)

            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertIn("hello", (result.output_json or {}).get("stdout", "").lower())
            self.assertEqual(result.output_json["execution"]["mode"], EXECUTION_MODE_ISOLATED_SUBPROCESS)
            self.assertEqual(result.output_json["execution"]["zone"], ZONE_TOOL_EXECUTION)
            self.assertEqual(receipt.policy_snapshot["execution"]["mode"], EXECUTION_MODE_ISOLATED_SUBPROCESS)
            self.assertTrue(receipt.capability_summary["valid"])
            self.assertIn("execution_mode_selected", [event.event_type for event in events])
            self.assertIn("isolated_execution_started", [event.event_type for event in events])
            self.assertIn("isolated_execution_finished", [event.event_type for event in events])
        finally:
            if state_store is not None:
                state_store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_isolated_execution_blocks_direct_trusted_state_access(self) -> None:
        tmpdir = self._tmpdir("exec-zone-block")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            request = build_execution_request(
                component="run_shell",
                action="execute",
                actor="test",
                db_path=db_path,
                related_run_id="run-1",
                related_task_id="task-1",
                state_access="direct_handle",
            )

            with self.assertRaises(TrustZoneViolationError):
                run_isolated_process(
                    request,
                    worker_command=build_worker_command("shell"),
                    worker_input={
                        "command": ["echo", "hello"],
                        "cwd": str(tmpdir),
                        "timeout_seconds": 3,
                    },
                    timeout_s=3,
                )

            with StateStore(db_path) as store:
                events = store.list_security_events(
                    limit=10,
                    event_type="trust_zone_violation_blocked",
                )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["component"], "run_shell")
            self.assertEqual(events[0].payload["state_access"], "direct_handle")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sandboxed_execution_sets_reduced_env_and_logs(self) -> None:
        tmpdir = self._tmpdir("exec-sandbox")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            captured = {}

            def fake_worker_run(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "output": {"status": "ok"},
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                        }
                    ),
                    stderr="",
                )

            request = build_execution_request(
                component="plugin_runtime",
                action="execute",
                actor="test",
                db_path=db_path,
                related_run_id="run-sandbox",
            )
            with (
                mock.patch.dict(os.environ, {"GISMO_TEST_SECRET": "should-not-leak"}),
                mock.patch("gismo.core.execution.subprocess.run", side_effect=fake_worker_run),
            ):
                result = run_sandboxed_process(
                    request,
                    worker_command=build_worker_command("plugin"),
                    worker_input={
                        "manifest": {
                            "plugin_id": "demo.plugin",
                            "version": "1.0.0",
                            "entrypoint": "demo:main",
                            "signer_id": "demo",
                            "capabilities": [],
                            "constraints": {},
                            "signature": "signed",
                        },
                        "payload": {"message": "hi"},
                    },
                    timeout_s=3,
                    event_details={"test": "sandbox"},
                )

            self.assertEqual(result["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertTrue(result["execution"]["sandbox_profile"])
            self.assertEqual(captured["command"][3], "plugin")
            self.assertIn("cwd", captured["kwargs"])
            self.assertIn("env", captured["kwargs"])
            self.assertNotIn("GISMO_TEST_SECRET", captured["kwargs"]["env"])
            self.assertEqual(
                Path(captured["kwargs"]["cwd"]).name,
                "plugin_runtime",
            )
            with StateStore(db_path) as store:
                events = store.list_security_events(limit=10)
            event_types = [event.event_type for event in events]
            self.assertIn("execution_mode_selected", event_types)
            self.assertIn("isolated_execution_finished", event_types)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sandbox_timeout_is_logged_as_failure(self) -> None:
        tmpdir = self._tmpdir("exec-sandbox-timeout")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            request = build_execution_request(
                component="plugin_runtime",
                action="execute",
                actor="test",
                db_path=db_path,
                related_run_id="run-timeout",
            )
            with mock.patch(
                "gismo.core.execution.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["python"], timeout=4),
            ):
                with self.assertRaises(ExecutionBoundaryError):
                    run_sandboxed_process(
                        request,
                        worker_command=build_worker_command("plugin"),
                        worker_input={
                            "manifest": {
                                "plugin_id": "demo.plugin",
                                "version": "1.0.0",
                                "entrypoint": "demo:main",
                                "signer_id": "demo",
                                "capabilities": [],
                                "constraints": {},
                                "signature": "signed",
                            },
                            "payload": {},
                        },
                        timeout_s=2,
                    )

            with StateStore(db_path) as store:
                events = store.list_security_events(limit=10)
            failed = next(event for event in events if event.event_type == "isolated_execution_failed")
            self.assertEqual(failed.payload["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertIn("timed out", failed.payload["failure_reason"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sandboxed_execution_blocks_trusted_state_locator_inputs(self) -> None:
        tmpdir = self._tmpdir("exec-sandbox-block")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            request = build_execution_request(
                component="plugin_runtime",
                action="execute",
                actor="test",
                db_path=db_path,
            )
            with self.assertRaises(TrustZoneViolationError):
                run_sandboxed_process(
                    request,
                    worker_command=build_worker_command("plugin"),
                    worker_input={"db_path": db_path},
                    timeout_s=1,
                )

            with StateStore(db_path) as store:
                events = store.list_security_events(
                    limit=10,
                    event_type="trust_zone_violation_blocked",
                )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["component"], "plugin_runtime")
            self.assertEqual(events[0].payload["path"], "input.db_path")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ollama_python_transport_logs_sandboxed_execution_mode(self) -> None:
        tmpdir = self._tmpdir("exec-ollama-python")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            network_policy = NetworkPolicy(
                default_action="deny",
                components={"ollama_client": NetworkRule(allow_loopback=True)},
            )

            def fake_worker_run(command, **kwargs):
                payload = json.loads(kwargs["input"])
                self.assertEqual(command[3], "ollama_python")
                self.assertEqual(payload["timeout_seconds"], 120)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "body": '{"message":{"content":"{}"}}',
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                        }
                    ),
                    stderr="",
                )

            with (
                mock.patch.dict(os.environ, {"GISMO_OLLAMA_TRANSPORT": "python"}),
                mock.patch("gismo.core.execution.subprocess.run", side_effect=fake_worker_run),
            ):
                response = ollama.ollama_chat(
                    "ping",
                    "return JSON",
                    network_policy=network_policy,
                    db_path=db_path,
                    actor="test",
                    related_run_id="run-2",
                    related_task_id="task-2",
                )

            self.assertEqual(response, "{}")
            with StateStore(db_path) as store:
                events = store.list_security_events(limit=10)
            event_types = [event.event_type for event in events]
            self.assertIn("outbound_allowed", event_types)
            self.assertIn("execution_mode_selected", event_types)
            self.assertIn("isolated_execution_started", event_types)
            self.assertIn("isolated_execution_finished", event_types)
            selected = next(event for event in events if event.event_type == "execution_mode_selected")
            self.assertEqual(selected.payload["component"], "ollama_client")
            self.assertEqual(selected.payload["mode"], EXECUTION_MODE_SANDBOXED)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ollama_curl_transport_keeps_outbound_and_isolation_events(self) -> None:
        tmpdir = self._tmpdir("exec-ollama-curl")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path):
                pass
            network_policy = NetworkPolicy(
                default_action="deny",
                components={"ollama_client": NetworkRule(allow_loopback=True)},
            )

            def fake_worker_run(*args, **kwargs):
                return subprocess.CompletedProcess(
                    args[0],
                    0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "stdout": '{"message":{"content":"{}"}}',
                            "stderr": "",
                            "exit_code": 0,
                        }
                    ),
                    stderr="",
                )

            with (
                mock.patch.dict(os.environ, {"GISMO_OLLAMA_TRANSPORT": "curl"}),
                mock.patch.object(ollama, "_resolve_curl_executable", return_value="curl.exe"),
                mock.patch("gismo.core.execution.subprocess.run", side_effect=fake_worker_run),
            ):
                response = ollama.ollama_chat(
                    "ping",
                    "return JSON",
                    network_policy=network_policy,
                    db_path=db_path,
                    actor="test",
                    related_run_id="run-3",
                    related_task_id="task-3",
                )

            self.assertEqual(response, "{}")
            with StateStore(db_path) as store:
                events = store.list_security_events(limit=20)
            event_types = [event.event_type for event in events]
            self.assertIn("outbound_allowed", event_types)
            self.assertIn("execution_mode_selected", event_types)
            self.assertIn("isolated_execution_started", event_types)
            self.assertIn("isolated_execution_finished", event_types)
            selected = next(event for event in events if event.event_type == "execution_mode_selected")
            self.assertEqual(selected.payload["component"], "ollama_client")
            self.assertEqual(selected.payload["mode"], EXECUTION_MODE_ISOLATED_SUBPROCESS)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
