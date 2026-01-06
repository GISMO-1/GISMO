import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gismo.memory.store import (
    create_profile as memory_create_profile,
    policy_hash_for_path as memory_policy_hash_for_path,
    put_item as memory_put_item,
)


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class MemoryPreviewCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_memory_preview_includes_hash_and_counts(self) -> None:
        memory_create_profile(
            str(self.db_path),
            name="operator",
            description="Operator profile",
            include_namespaces=["global"],
            exclude_namespaces=[],
            include_kinds=["preference"],
            exclude_kinds=[],
            max_items=None,
        )
        memory_put_item(
            str(self.db_path),
            namespace="global",
            key="default_model",
            kind="preference",
            value="phi3:mini",
            tags=None,
            confidence="high",
            source="operator",
            ttl_seconds=None,
            actor="operator",
            policy_hash=memory_policy_hash_for_path(None),
        )
        policy_path = self.repo_root / "policy" / "readonly.json"
        preview = _run_cli(
            [
                "memory",
                "preview",
                "--db",
                str(self.db_path),
                "--memory-profile",
                "operator",
                "--policy",
                str(policy_path),
                "--json",
            ],
            cwd=self.repo_root,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        payload = json.loads(preview.stdout)
        self.assertIn("injection_hash", payload)
        eligibility = payload.get("eligibility", {})
        self.assertEqual(eligibility.get("selected_items"), 1)


if __name__ == "__main__":
    unittest.main()
