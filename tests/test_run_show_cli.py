from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from uuid import uuid4

from gismo.core.models import EVENT_TYPE_LLM_PLAN, ToolCall
from gismo.core.state import StateStore
from gismo.memory.store import record_event as memory_record_event


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


def test_run_show_includes_agent_role_context(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    run = state_store.create_run(
        label="role-run",
        metadata={
            "agent_role": {
                "role_id": "role-123",
                "role_name": "planner",
                "memory_profile_id": "profile-123",
            }
        },
    )
    state_store.close()

    proc = _run_cli(["run", "--db", str(db_path), "show", run.id], cwd=repo_root)
    assert proc.returncode == 0, proc.stderr
    stdout = proc.stdout
    assert "Role:" in stdout
    assert "planner" in stdout
    assert "profile-123" in stdout

    proc_json = _run_cli(["runs", "--db", str(db_path), "show", "--json", run.id], cwd=repo_root)
    assert proc_json.returncode == 0, proc_json.stderr
    payload = json.loads(proc_json.stdout)
    assert payload["agent_role"]["role_id"] == "role-123"


def test_run_show_includes_memory_provenance(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    plan_event_id = str(uuid4())
    run = state_store.create_run(
        label="memory",
        metadata={"plan_event_id": plan_event_id},
    )
    payload = {
        "plan": {
            "memory_suggestions": [
                {
                    "namespace": "global",
                    "key": "default_model",
                    "kind": "preference",
                    "confidence": "high",
                    "source": "llm",
                }
            ]
        },
        "memory_injection_enabled": True,
        "memory_injected_count": 1,
        "memory_injected_keys": [{"namespace": "global", "key": "default_model"}],
        "memory_injected_bytes": 128,
        "memory_injected_cap_items": 20,
        "memory_injected_cap_bytes": 8192,
        "apply_memory_suggestions_requested": True,
        "apply_memory_suggestions_result": {"applied": 0, "skipped": 0, "denied": 1},
        "apply_memory_suggestions_applied": [],
        "apply_memory_policy_path": "policy/readonly.json",
        "apply_memory_yes": True,
        "apply_memory_non_interactive": False,
        "apply_memory_decision_path": "non-interactive",
    }
    state_store.record_event(
        actor="agent",
        event_type=EVENT_TYPE_LLM_PLAN,
        message="LLM plan generated.",
        json_payload=payload,
        event_id=plan_event_id,
    )
    memory_record_event(
        str(db_path),
        operation="put",
        actor="agent",
        policy_hash="test-hash",
        request={
            "namespace": "global",
            "key": "default_model",
            "kind": "preference",
            "value_json": "\"phi3:mini\"",
            "tags_json": None,
            "confidence": "high",
            "source": "llm",
            "ttl_seconds": None,
        },
        result_meta={
            "policy_decision": "denied",
            "policy_reason": "confirmation_required",
            "confirmation": {"required": True, "provided": False, "mode": None},
        },
        related_ask_event_id=plan_event_id,
    )
    state_store.close()

    proc = _run_cli(["runs", "--db", str(db_path), "show", run.id], cwd=repo_root)
    assert proc.returncode == 0, proc.stderr
    stdout = proc.stdout
    assert "Memory provenance:" in stdout
    assert "Injected memory:" in stdout
    assert "Suggested memory updates:" in stdout
    assert "Apply results:" in stdout
    assert "Policy/confirmation:" in stdout


def test_run_show_json_includes_memory_provenance(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    plan_event_id = str(uuid4())
    run = state_store.create_run(
        label="memory-json",
        metadata={"plan_event_id": plan_event_id},
    )
    payload = {
        "plan": {
            "memory_suggestions": [
                {
                    "namespace": "global",
                    "key": "default_model",
                    "kind": "preference",
                    "confidence": "high",
                    "source": "llm",
                }
            ]
        },
        "memory_injection_enabled": True,
        "memory_injected_count": 1,
        "memory_injected_keys": [{"namespace": "global", "key": "default_model"}],
        "memory_injected_bytes": 128,
        "memory_injected_cap_items": 20,
        "memory_injected_cap_bytes": 8192,
        "apply_memory_suggestions_requested": False,
        "apply_memory_suggestions_result": {"applied": 0, "skipped": 0, "denied": 0},
        "apply_memory_suggestions_applied": [],
        "apply_memory_policy_path": None,
        "apply_memory_yes": False,
        "apply_memory_non_interactive": True,
        "apply_memory_decision_path": "non-interactive",
    }
    state_store.record_event(
        actor="agent",
        event_type=EVENT_TYPE_LLM_PLAN,
        message="LLM plan generated.",
        json_payload=payload,
        event_id=plan_event_id,
    )
    state_store.close()

    proc = _run_cli(
        ["runs", "--db", str(db_path), "show", "--json", run.id],
        cwd=repo_root,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["run"]["id"] == run.id
    memory_provenance = output["memory_provenance"]
    assert memory_provenance["injected"]["count"] == 1
    assert memory_provenance["suggested"]["count"] == 1
    assert "policy" in memory_provenance
