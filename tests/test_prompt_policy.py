import unittest
from pathlib import Path

from gismo.core.permissions import FileSystemPolicy, PermissionPolicy, ShellPolicy
from gismo.core.policy_summary import summarize_policy
from gismo.llm.prompts import build_system_prompt


class PromptPolicyTest(unittest.TestCase):
    def test_prompt_includes_policy_summary(self) -> None:
        policy = PermissionPolicy(
            allowed_tools={"echo", "write_note", "run_shell"},
            fs=FileSystemPolicy(Path(".")),
            shell=ShellPolicy(Path("."), allowlist=[["dir"]], timeout_seconds=10.0),
        )
        summary = summarize_policy(policy)
        prompt = build_system_prompt(policy_summary=summary, max_actions=7)

        self.assertIn("deny-by-default", prompt)
        self.assertIn("allowed_tools: echo, run_shell, write_note", prompt)
        self.assertIn("shell allowlist: 1 entry(s)", prompt)
        self.assertIn("max_actions: 7", prompt)
        self.assertIn("enqueue-only", prompt)


if __name__ == "__main__":
    unittest.main()
