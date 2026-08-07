import json
import os
import unittest
from pathlib import Path
from uuid import uuid4
import shutil

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
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_device_command_parsing(self) -> None:
        plan = parse_command("device: turn on kitchen lights")
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["steps"][0]["tool_name"], "device_control")
        self.assertEqual(plan["steps"][0]["input_json"]["action"], "turn_on")
        self.assertEqual(plan["steps"][0]["input_json"]["target"], "kitchen lights")

    def test_light_power_command_parsing(self) -> None:
        plan = parse_command("device: turn Dad's light on")
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["steps"][0]["input_json"]["action"], "turn_on")
        self.assertEqual(plan["steps"][0]["input_json"]["target"], "Dad's light")

    def test_light_power_off_command_parsing(self) -> None:
        plan = parse_command("device: turn Dad's light off")
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["steps"][0]["input_json"]["action"], "turn_off")
        self.assertEqual(plan["steps"][0]["input_json"]["target"], "Dad's light")

    def test_light_cool_white_command_parsing(self) -> None:
        plan = parse_command("device: set Dad's light to cool white")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual([step["input_json"]["action"] for step in plan["steps"]], ["turn_on", "set_color_temp"])
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["preset"], "cool_white")

    def test_light_warm_command_parsing(self) -> None:
        plan = parse_command("device: make Dad's light warm")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual(plan["steps"][1]["input_json"]["action"], "set_color_temp")
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["preset"], "warm")

    def test_light_blue_command_parsing(self) -> None:
        plan = parse_command("device: make Dad's light blue")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual(plan["steps"][1]["input_json"]["action"], "set_color_rgb")
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["color_name"], "blue")

    def test_light_red_command_parsing(self) -> None:
        plan = parse_command("device: set Dad's light to red")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual(plan["steps"][1]["input_json"]["action"], "set_color_rgb")
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["color_name"], "red")

    def test_light_brightness_command_parsing(self) -> None:
        plan = parse_command("device: dim Dad's light to 20 percent")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual([step["input_json"]["action"] for step in plan["steps"]], ["turn_on", "set_brightness"])
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["brightness"], 20)

    def test_light_brighten_command_parsing(self) -> None:
        plan = parse_command("device: brighten Dad's light to 80")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual([step["input_json"]["action"] for step in plan["steps"]], ["turn_on", "set_brightness"])
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["brightness"], 80)

    def test_light_raise_brightness_command_parsing(self) -> None:
        plan = parse_command("device: raise brightness on Dad's light to 80 percent")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual([step["input_json"]["action"] for step in plan["steps"]], ["turn_on", "set_brightness"])
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["brightness"], 80)

    def test_light_combined_power_and_white_command_parsing(self) -> None:
        plan = parse_command("device: turn Dad's room light on cool white")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual([step["input_json"]["action"] for step in plan["steps"]], ["turn_on", "set_color_temp"])
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["preset"], "cool_white")

    def test_light_combined_color_and_brightness_parsing(self) -> None:
        plan = parse_command("device: set Dad's light to blue at 50 percent")
        self.assertEqual(plan["mode"], "graph")
        self.assertEqual(
            [step["input_json"]["action"] for step in plan["steps"]],
            ["turn_on", "set_color_rgb", "set_brightness"],
        )
        self.assertEqual(plan["steps"][1]["input_json"]["params"]["color_name"], "blue")
        self.assertEqual(plan["steps"][2]["input_json"]["params"]["brightness"], 50)

    def test_ambiguous_light_command_requires_clarification(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not understand|need a light setting"):
            parse_command("device: make Dad's light nice")

    def test_calendar_command_parsing(self) -> None:
        plan = parse_command(
            'calendar: add {"title":"Dinner","start_at":"2026-03-20T18:00:00","end_at":"2026-03-20T19:00:00"}'
        )
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["steps"][0]["tool_name"], "calendar_control")
        self.assertEqual(plan["steps"][0]["input_json"]["action"], "add")
        self.assertEqual(plan["steps"][0]["input_json"]["payload"]["title"], "Dinner")

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
        tmpdir = self._tmpdir("operator-shell")
        state_store = None
        try:
            repo_root = tmpdir
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
        finally:
            if state_store is not None:
                state_store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
