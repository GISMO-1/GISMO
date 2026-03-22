import unittest
from pathlib import Path
from uuid import uuid4
import shutil

from gismo.core.agent import SimpleAgent
from gismo.core.models import FailureType
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool
from gismo.core.tools import ToolRegistry


class PolicyDevSafeTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _load_dev_safe_policy(self) -> tuple[Path, PermissionPolicy]:
        repo_root = Path(__file__).resolve().parents[1]
        policy_path = repo_root / "policy" / "dev-safe.json"
        policy = load_policy(str(policy_path), repo_root=repo_root)
        return repo_root, policy

    def _build_orchestrator(
        self, db_path: str, policy: PermissionPolicy
    ) -> tuple[StateStore, Orchestrator]:
        state_store = StateStore(db_path)
        registry = ToolRegistry()
        shell_config = ShellConfig(
            base_dir=policy.shell.base_dir,
            allowlist=policy.shell.allowlist,
            timeout_seconds=policy.shell.timeout_seconds,
        )
        registry.register(ShellTool(shell_config))
        agent = SimpleAgent(registry=registry)
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=policy,
            agent=agent,
        )
        return state_store, orchestrator

    def test_dev_safe_allows_shell_allowlist(self) -> None:
        tmpdir = self._tmpdir("policy-dev-safe-allow")
        state_store = None
        try:
            _, policy = self._load_dev_safe_policy()
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path, policy)
            run = state_store.create_run(label="dev-safe", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Shell",
                description="Allowed command",
                input_json={"tool": "run_shell", "payload": {"command": ["git", "status"]}},
            )

            result = orchestrator.run_tool(run.id, task, "run_shell", {"command": ["git", "status"]})

            self.assertEqual(result.status.value, "SUCCEEDED")
            tool_calls = list(state_store.list_tool_calls_for_task(task.id))
            self.assertEqual(tool_calls[0].status.value, "SUCCEEDED")
        finally:
            if state_store is not None:
                state_store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_dev_safe_denies_non_allowlisted_command(self) -> None:
        tmpdir = self._tmpdir("policy-dev-safe-deny")
        state_store = None
        try:
            _, policy = self._load_dev_safe_policy()
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path, policy)
            run = state_store.create_run(label="dev-safe", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Shell",
                description="Disallowed command",
                input_json={"tool": "run_shell", "payload": {"command": ["ls"]}},
            )

            result = orchestrator.run_tool(run.id, task, "run_shell", {"command": ["ls"]})

            self.assertEqual(result.failure_type, FailureType.PERMISSION_DENIED)
            tool_calls = list(state_store.list_tool_calls_for_task(task.id))
            self.assertEqual(tool_calls[0].failure_type, FailureType.PERMISSION_DENIED)
            self.assertIn("allowlist", tool_calls[0].error or "")
        finally:
            if state_store is not None:
                state_store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
