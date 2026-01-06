import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.memory.store import create_profile as memory_create_profile


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class AgentRoleCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_policy(self, policy: dict) -> Path:
        path = Path(self.temp_dir.name) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def test_agent_role_create_show_list_and_retire(self) -> None:
        profile = memory_create_profile(
            str(self.db_path),
            name="planner-profile",
            description=None,
            include_namespaces=["global"],
            exclude_namespaces=None,
            include_kinds=["fact"],
            exclude_kinds=None,
            max_items=None,
        )
        policy_path = self._write_policy(
            {
                "allowed_tools": ["agent.role.create", "agent.role.retire"],
                "memory": {"allow": {}},
            }
        )
        create = _run_cli(
            [
                "agent",
                "role",
                "create",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--name",
                "planner",
                "--description",
                "Planner role",
                "--memory-profile",
                profile.name,
                "--yes",
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(create.returncode, 0, create.stderr)
        created = json.loads(create.stdout)
        self.assertEqual(created["name"], "planner")
        self.assertEqual(created["memory_profile_id"], profile.profile_id)

        listed = _run_cli(
            [
                "agent",
                "role",
                "list",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "planner")

        show = _run_cli(
            [
                "agent",
                "role",
                "show",
                "planner",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(show.returncode, 0, show.stderr)
        details = json.loads(show.stdout)
        self.assertEqual(details["name"], "planner")
        self.assertEqual(details["memory_profile_id"], profile.profile_id)

        retire = _run_cli(
            [
                "agent",
                "role",
                "retire",
                "planner",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--yes",
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(retire.returncode, 0, retire.stderr)
        retired = json.loads(retire.stdout)
        self.assertEqual(retired["name"], "planner")
        self.assertIsNotNone(retired["retired_at"])
