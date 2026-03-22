import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.agent import SimpleAgent
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.plugin_runtime import execute_verified_plugin
from gismo.core.plugin_signing import (
    PluginManifest,
    PluginVerificationError,
    sign_manifest,
)
from gismo.core.state import StateStore
from gismo.core.toolpacks.plugin_tool import PluginRuntimeTool
from gismo.core.tools import ToolRegistry
from gismo.core.trust_zones import EXECUTION_MODE_SANDBOXED


class PluginRuntimeTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _write_plugin_fixture(self, tmpdir: Path) -> tuple[Path, Path, Path]:
        module_path = tmpdir / "demo_plugin.py"
        module_path.write_text(
            "def main(payload):\n"
            "    return {\n"
            "        'echo': payload.get('message'),\n"
            "        'count': len(payload),\n"
            "    }\n",
            encoding="utf-8",
        )
        trust_store_path = tmpdir / "plugin-trust.json"
        trust_store_path.write_text(
            json.dumps(
                {
                    "signers": {
                        "demo-signer": {
                            "shared_secret": "shared-secret",
                            "trusted": True,
                            "note": "test signer",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = PluginManifest(
            plugin_id="demo.plugin",
            version="1.0.0",
            entrypoint="demo_plugin:main",
            signer_id="demo-signer",
            capabilities=["echo"],
            constraints={"timeout_s": 5},
        )
        manifest_path = tmpdir / "demo.plugin.json"
        manifest_path.write_text(
            json.dumps(
                {
                    **manifest.to_dict(),
                    "signature": sign_manifest(manifest, shared_secret="shared-secret"),
                }
            ),
            encoding="utf-8",
        )
        return module_path, manifest_path, trust_store_path

    def test_plugin_runtime_tool_executes_verified_manifest_in_sandbox(self) -> None:
        tmpdir = self._tmpdir("plugin-runtime")
        state_store = None
        try:
            _, manifest_path, trust_store_path = self._write_plugin_fixture(tmpdir)
            db_path = str(tmpdir / "state.db")
            state_store = StateStore(db_path)
            registry = ToolRegistry()
            registry.register(PluginRuntimeTool(trust_store_path=str(trust_store_path)))
            orchestrator = Orchestrator(
                state_store=state_store,
                registry=registry,
                policy=PermissionPolicy(allowed_tools={"plugin_runtime"}),
                agent=SimpleAgent(registry=registry),
            )
            run = state_store.create_run(label="plugin-runtime", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Run plugin",
                description="Execute a verified plugin",
                input_json={
                    "tool": "plugin_runtime",
                    "payload": {
                        "manifest_path": str(manifest_path),
                        "payload": {"message": "hello"},
                    },
                },
            )

            result = orchestrator.run_tool(
                run.id,
                task,
                "plugin_runtime",
                {"manifest_path": str(manifest_path), "payload": {"message": "hello"}},
            )
            receipts = list(state_store.list_tool_receipts(run.id))
            events = state_store.list_security_events(limit=20)

            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertEqual(result.output_json["output"]["echo"], "hello")
            self.assertEqual(result.output_json["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertEqual(result.output_json["execution"]["zone"], "plugin_runtime")
            self.assertEqual(receipts[0].policy_snapshot["execution"]["mode"], EXECUTION_MODE_SANDBOXED)
            self.assertEqual(receipts[0].policy_snapshot["execution"]["zone"], "plugin_runtime")
            event_types = [event.event_type for event in events]
            self.assertIn("plugin_signature_verified", event_types)
            self.assertIn("execution_mode_selected", event_types)
            self.assertIn("isolated_execution_finished", event_types)
        finally:
            if state_store is not None:
                state_store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_plugin_runtime_requires_verified_manifest(self) -> None:
        tmpdir = self._tmpdir("plugin-runtime-untrusted")
        try:
            (tmpdir / "demo_plugin.py").write_text(
                "def main(payload):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            manifest_path = tmpdir / "unsigned.plugin.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "plugin_id": "demo.plugin",
                        "version": "1.0.0",
                        "entrypoint": "demo_plugin:main",
                        "signer_id": "demo-signer",
                        "capabilities": ["echo"],
                        "constraints": {},
                    }
                ),
                encoding="utf-8",
            )
            trust_store_path = tmpdir / "plugin-trust.json"
            trust_store_path.write_text(
                json.dumps(
                    {
                        "signers": {
                            "demo-signer": {
                                "shared_secret": "shared-secret",
                                "trusted": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PluginVerificationError):
                execute_verified_plugin(
                    manifest_path,
                    payload={"message": "hello"},
                    trust_store_path=trust_store_path,
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
