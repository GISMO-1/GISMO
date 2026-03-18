import json
import os
import tempfile
import unittest
from pathlib import Path

from gismo.cli.operator import normalize_command, parse_command, required_tools
from gismo.core.agent import SimpleAgent
from gismo.core.models import TaskStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool
from gismo.core.tools import ToolRegistry


def run_operator_plan(
    state_store: StateStore,
    orchestrator: Orchestrator,
    plan: dict,
    normalized_command: str,
) -> tuple[str, list[str]]:
    run = state_store.create_run(label="operator-shell", metadata={"command": normalized_command})
    created_task_ids = []
    previous_task_id = None
    for index, step in enumerate(plan["steps"]):
        tool_name = step["tool_name"]
        tool_input = step["input_json"]
        task = state_store.create_task(
            run_id=run.id,
            title=step["title"],
            description="Operator shell test",
            input_json={"tool": tool_name, "payload": tool_input},
            depends_on=[previous_task_id] if previous_task_id else None,
        )
        created_task_ids.append(task.id)
        previous_task_id = task.id

    task = state_store.get_task(created_task_ids[0])
    assert task is not None
    orchestrator.run_tool(run.id, task, task.input_json["tool"], task.input_json["payload"])
    return run.id, created_task_ids


class OperatorShellTest(unittest.TestCase):
    def test_device_command_parsing(self) -> None:
        plan = parse_command("device: turn on kitchen lights")
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["steps"][0]["tool_name"], "device_control")
        self.assertEqual(plan["steps"][0]["input_json"]["action"], "turn_on")
        self.assertEqual(plan["steps"][0]["input_json"]["target"], "kitchen lights")

    def test_shell_command_policy_gating(self) -> None:
        plan = parse_command("shell: echo hello")
        tools = required_tools(plan)
        self.assertEqual(tools, {"run_shell"})

        policy = PermissionPolicy(allowed_tools=tools)
        policy.check_tool_allowed("run_shell")

        denied_policy = PermissionPolicy(allowed_tools=set())
        with self.assertRaises(PermissionError):
            denied_policy.check_tool_allowed("run_shell")

    @unittest.skipUnless(os.name == "nt", "Windows-only builtin shell regression")
    def test_shell_builtin_echo_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            db_path = str(repo_root / "state.db")
            policy_path = repo_root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "allowed_tools": ["run_shell"],
                        "shell": {
                            "base_dir": ".",
                            "allowlist": [["echo", "hello"]],
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy = load_policy(str(policy_path), repo_root=repo_root)
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

            command = "shell: echo hello"
            plan = parse_command(command)
            normalized = normalize_command(command)
            run_id, task_ids = run_operator_plan(state_store, orchestrator, plan, normalized)

            task = state_store.get_task(task_ids[0])
            assert task is not None
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            output = task.output_json or {}
            stdout = output.get("stdout", "")
            self.assertIn("hello", stdout.lower())


if __name__ == "__main__":
    unittest.main()
