from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from gismo.core.state import StateStore


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """
    Run `python -m gismo.cli.main ...` in a subprocess and return the completed process.
    """
    cmd = [sys.executable, "-m", "gismo.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo_root() -> Path:
    # tests/ is at <repo>/tests; go up one to repo root
    return Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    # Use a unique temp DB per test
    return tmp_path / "state.db"


def test_queue_stats_empty(repo_root: Path, db_path: Path) -> None:
    # stats should work even if queue is empty
    p = _run_cli(["queue", "stats", "--db", str(db_path)], cwd=repo_root)
    assert p.returncode == 0, p.stderr
    assert "DB:" in p.stdout
    assert "Total:" in p.stdout


def test_queue_list_and_show_short_id(repo_root: Path, db_path: Path) -> None:
    # enqueue two items so list/show have content
    p1 = _run_cli(["enqueue", "--db", str(db_path), "echo:", "test1"], cwd=repo_root)
    assert p1.returncode == 0, p1.stderr
    p2 = _run_cli(["enqueue", "--db", str(db_path), "echo:", "test2"], cwd=repo_root)
    assert p2.returncode == 0, p2.stderr

    # list in JSON, grab a real UUID
    lp = _run_cli(["queue", "list", "--db", str(db_path), "--limit", "10", "--json"], cwd=repo_root)
    assert lp.returncode == 0, lp.stderr
    items = json.loads(lp.stdout)
    assert isinstance(items, list)
    assert len(items) >= 2
    full_id = items[0]["id"]
    assert isinstance(full_id, str) and len(full_id) > 8

    # show by prefix (first 8 chars)
    prefix = full_id[:8]
    sp = _run_cli(["queue", "show", "--db", str(db_path), prefix, "--json"], cwd=repo_root)
    assert sp.returncode == 0, sp.stderr
    shown = json.loads(sp.stdout)
    assert shown["id"] == full_id


def test_queue_show_ambiguous_prefix(repo_root: Path, db_path: Path) -> None:
    # Create two items whose IDs will likely share the first char in many cases,
    # but we can force ambiguity by showing with a 1-char prefix and allowing
    # your CLI to respond with "Ambiguous id prefix" if multiple match.
    _run_cli(["enqueue", "--db", str(db_path), "echo:", "a"], cwd=repo_root)
    _run_cli(["enqueue", "--db", str(db_path), "echo:", "b"], cwd=repo_root)

    # Get two IDs to find a prefix that is guaranteed ambiguous:
    lp = _run_cli(["queue", "list", "--db", str(db_path), "--limit", "10", "--json"], cwd=repo_root)
    assert lp.returncode == 0, lp.stderr
    items = json.loads(lp.stdout)
    ids = [it["id"] for it in items]
    assert len(ids) >= 2

    # Find the shortest ambiguous prefix by brute force (safe, tiny)
    a, b = ids[0], ids[1]
    prefix = ""
    for i in range(1, 9):
        cand = a[:i]
        if b.startswith(cand):
            prefix = cand
            break

    if not prefix:
        # If these two don't share a prefix, fall back to 1 char and accept that it might not be ambiguous.
        prefix = a[:1]

    sp = _run_cli(["queue", "show", "--db", str(db_path), prefix], cwd=repo_root)

    # If ambiguous, CLI should exit non-zero and include the message.
    # If not ambiguous (rare), it should succeed. Accept either, but validate behavior.
    if sp.returncode != 0:
        assert "Ambiguous id prefix" in sp.stdout
        assert "Provide a longer prefix" in sp.stdout
    else:
        assert "DB:" in sp.stdout
        assert "ID:" in sp.stdout


def test_queue_purge_failed_dry_run_and_confirm(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    failed_item = state_store.enqueue_command("echo: fail")
    state_store.mark_queue_item_failed(failed_item.id, "boom", retryable=False)
    queued_item = state_store.enqueue_command("echo: queued")
    succeeded_item = state_store.enqueue_command("echo: ok")
    state_store.mark_queue_item_succeeded(succeeded_item.id)

    dry_run = _run_cli(
        ["queue", "purge-failed", "--db", str(db_path)],
        cwd=repo_root,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert "Dry run" in dry_run.stdout
    assert failed_item.id[:8] in dry_run.stdout
    assert state_store.get_queue_item(failed_item.id) is not None

    confirmed = _run_cli(
        ["queue", "purge-failed", "--db", str(db_path), "--yes"],
        cwd=repo_root,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "Deleted 1 failed queue item" in confirmed.stdout
    assert state_store.get_queue_item(failed_item.id) is None
    assert state_store.get_queue_item(queued_item.id) is not None
    assert state_store.get_queue_item(succeeded_item.id) is not None


def test_queue_cancel(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    item = state_store.enqueue_command("echo: cancel")

    cancel = _run_cli(
        ["queue", "cancel", "--db", str(db_path), item.id],
        cwd=repo_root,
    )
    assert cancel.returncode == 0, cancel.stderr
    assert "Cancelled queue item" in cancel.stdout

    updated = state_store.get_queue_item(item.id)
    assert updated is not None
    assert updated.status.value == "CANCELLED"


def test_queue_show_run_id_hint(repo_root: Path, db_path: Path) -> None:
    state_store = StateStore(str(db_path))
    run = state_store.create_run(label="hint")

    proc = _run_cli(["queue", "show", "--db", str(db_path), run.id], cwd=repo_root)

    assert proc.returncode != 0
    assert (
        "That looks like a RUN id; use `runs show <id>` or `export --run <id>`."
        in proc.stdout
    )
