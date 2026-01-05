import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.memory.store import retire_namespace as memory_retire_namespace


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class MemoryProfileCliTest(unittest.TestCase):
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

    def test_memory_profile_create_show_list_and_retire(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [],
                "memory": {
                    "allow": {
                        "memory.profile.create": ["*"],
                        "memory.profile.retire": ["*"],
                    }
                },
            }
        )
        create = _run_cli(
            [
                "memory",
                "profile",
                "create",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--name",
                "operator",
                "--description",
                "Operator profile",
                "--include-namespace",
                "global",
                "--include-kind",
                "fact",
                "--max-items",
                "5",
                "--yes",
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(create.returncode, 0, create.stderr)
        created = json.loads(create.stdout)
        self.assertEqual(created["name"], "operator")

        listed = _run_cli(
            [
                "memory",
                "profile",
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
        self.assertEqual(payload[0]["name"], "operator")

        show = _run_cli(
            [
                "memory",
                "profile",
                "show",
                "operator",
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
        self.assertEqual(details["name"], "operator")
        self.assertEqual(details["include_namespaces"], ["global"])

        retire = _run_cli(
            [
                "memory",
                "profile",
                "retire",
                "operator",
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
        self.assertIsNotNone(retired["retired_at"])

    def test_memory_profile_create_requires_confirmation_non_interactive(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [],
                "memory": {
                    "allow": {
                        "memory.profile.create": ["*"],
                    }
                },
            }
        )
        create = _run_cli(
            [
                "memory",
                "profile",
                "create",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--name",
                "operator",
                "--include-namespace",
                "global",
                "--non-interactive",
            ],
            cwd=self.repo_root,
        )
        self.assertNotEqual(create.returncode, 0)
        self.assertIn("Confirmation required", create.stderr)

    def test_memory_profile_show_warns_on_retired_namespace(self) -> None:
        policy_path = self._write_policy(
            {
                "allowed_tools": [],
                "memory": {
                    "allow": {"memory.profile.create": ["*"]},
                },
            }
        )
        _run_cli(
            [
                "memory",
                "profile",
                "create",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--name",
                "operator",
                "--include-namespace",
                "global",
                "--yes",
            ],
            cwd=self.repo_root,
        )
        memory_retire_namespace(
            str(self.db_path),
            namespace="global",
            reason="policy-freeze",
        )
        show = _run_cli(
            [
                "memory",
                "profile",
                "show",
                "operator",
                "--db",
                str(self.db_path),
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(show.returncode, 0, show.stderr)
        payload = json.loads(show.stdout)
        self.assertTrue(payload["warnings"])
