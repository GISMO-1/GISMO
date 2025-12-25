from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gismo.core.models import ToolCall
from gismo.core.state import StateStore


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_run_show_outputs_summary(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    run = state_store.create_run(label="introspection")

    task_ok = state_store.create_task(
        run_id=run.id,
        title="Echo hi",
        description="Echo hi",
        input_json={"tool": "echo", "payload": {"message": "hi"}},
    )
    task_ok.mark_succeeded({"message": "hi"})
    state_store.update_task(task_ok)

    task_fail = state_store.create_task(
        run_id=run.id,
        title="Echo boom",
        description="Echo boom",
        input_json={"tool": "echo", "payload": {"message": "boom"}},
    )
    task_fail.mark_failed("boom")
    state_store.update_task(task_fail)

    call = ToolCall(
        run_id=run.id,
        task_id=task_ok.id,
        tool_name="echo",
        input_json={"message": "hi"},
    )
    state_store.record_tool_call(call)
    call.mark_succeeded({"message": "hi"})
    state_store.update_tool_call(call)

    proc = _run_cli(["run", "--db", str(db_path), "show", run.id], cwd=repo_root)
    assert proc.returncode == 0, proc.stderr
    stdout = proc.stdout

    assert "=== GISMO Run Summary ===" in stdout
    assert f"Run ID:     {run.id}" in stdout
    assert "Status:     failed" in stdout
    assert task_ok.id in stdout
    assert task_fail.id in stdout
    assert "tool=echo" in stdout
    assert "output:" in stdout
