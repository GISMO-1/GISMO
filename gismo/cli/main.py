"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from gismo.cli.operator import (
    make_idempotency_key,
    normalize_command,
    parse_command,
    required_tools,
)
from gismo.cli import ipc as ipc_cli
from gismo.cli import supervise as supervise_cli
from gismo.cli.windows_startup import (
    install_windows_startup_launcher,
    uninstall_windows_startup_launcher,
)
from gismo.cli.windows_tasks import WindowsTaskConfig, install_windows_task, uninstall_windows_task
from gismo.cli.windows_utils import quote_windows_arg
from gismo.core.agent import SimpleAgent
from gismo.core.daemon import run_daemon_loop
from gismo.core.export import export_latest_run_jsonl, export_run_jsonl
from gismo.core.models import QueueStatus, TaskStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool
from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool


def _fmt_dt(dt) -> str:
    return dt.isoformat(timespec="seconds") if dt else "-"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


def _summarize_value(value: object, max_len: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _truncate(text, max_len)


def _run_status(tasks: list) -> str:
    if not tasks:
        return "pending"
    statuses = {task.status for task in tasks}
    if TaskStatus.FAILED in statuses:
        return "failed"
    if TaskStatus.RUNNING in statuses:
        return "running"
    if statuses.issubset({TaskStatus.SUCCEEDED}):
        return "succeeded"
    return "pending"


def _run_time_bounds(
    run,
    tasks,
    tool_calls,
) -> tuple[datetime | None, datetime | None]:
    start_candidates = [run.created_at]
    start_candidates.extend(task.created_at for task in tasks)
    start_candidates.extend(call.started_at for call in tool_calls)
    start_time = min(start_candidates) if start_candidates else None
    end_candidates = [task.updated_at for task in tasks if task.updated_at]
    end_candidates.extend(call.finished_at for call in tool_calls if call.finished_at)
    end_time = max(end_candidates) if end_candidates else None
    return start_time, end_time


def run_demo(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(policy_path, repo_root=repo_root, default_allowed_tools={"echo"})
    registry = _build_registry(state_store, policy)

    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="demo", metadata={"purpose": "quickstart"})

    echo_task = state_store.create_task(
        run_id=run.id,
        title="Echo input",
        description="Echo the provided payload",
        input_json={"tool": "echo", "payload": {"message": "hello"}},
    )
    orchestrator.run_tool(run.id, echo_task, "echo", {"message": "hello"})

    note_task = state_store.create_task(
        run_id=run.id,
        title="Write note",
        description="Attempt to write a note",
        input_json={"tool": "write_note", "payload": {"note": "Hello, GISMO."}},
    )
    orchestrator.run_tool(run.id, note_task, "write_note", {"note": "Hello, GISMO."})

    policy.allow("write_note")
    orchestrator.run_tool(run.id, note_task, "write_note", {"note": "Hello, GISMO."})

    print("=== GISMO Demo Summary ===")
    print(f"Run: {run.id} ({run.label})")
    print("Tasks:")
    for task in state_store.list_tasks(run.id):
        print(f"- {task.id} {task.title} [{task.status}]")
        if task.error:
            print(f"  error: {task.error}")
        if task.output_json:
            print(f"  output: {task.output_json}")

    print("Tool Calls:")
    for call in state_store.list_tool_calls(run.id):
        print(
            f"- {call.id} tool={call.tool_name} status={call.status} "
            f"started={call.started_at.isoformat()}"
        )
        if call.error:
            print(f"  error: {call.error}")
        if call.output_json:
            print(f"  output: {call.output_json}")


def run_demo_graph(db_path: str, policy_path: str | None) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(
        policy_path,
        repo_root=repo_root,
        default_allowed_tools={"echo", "write_note"},
    )
    registry = _build_registry(state_store, policy)

    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="demo-graph", metadata={"purpose": "dag-demo"})

    task_a = state_store.create_task(
        run_id=run.id,
        title="Echo A",
        description="Echo A",
        input_json={"tool": "echo", "payload": {"message": "A"}},
    )
    task_b = state_store.create_task(
        run_id=run.id,
        title="Note B",
        description="Write note B",
        input_json={"tool": "write_note", "payload": {"note": "B"}},
        depends_on=[task_a.id],
    )
    task_c = state_store.create_task(
        run_id=run.id,
        title="Echo C",
        description="Echo C",
        input_json={"tool": "echo", "payload": {"message": "C"}},
        depends_on=[task_b.id],
    )

    orchestrator.run_task_graph(run.id)

    print("=== GISMO Demo Graph Summary ===")
    print(f"Run: {run.id} ({run.label})")
    print("Tasks:")
    for task in state_store.list_tasks(run.id):
        deps = ", ".join(task.depends_on) if task.depends_on else "none"
        print(f"- {task.id} {task.title} [{task.status}] depends_on={deps}")
        if task.error:
            print(f"  error: {task.error}")
        if task.output_json:
            print(f"  output: {task.output_json}")


def run_operator(db_path: str, command_parts: list[str], policy_path: str | None) -> None:
    command_text = " ".join(command_parts).strip()
    if not command_text:
        raise ValueError("Operator run requires a command string.")

    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    plan = parse_command(command_text)
    normalized = normalize_command(command_text)
    default_tools = required_tools(plan) if policy_path is None else ()
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(policy_path, repo_root=repo_root, default_allowed_tools=default_tools)
    registry = _build_registry(state_store, policy)
    agent = SimpleAgent(registry=registry)
    orchestrator = Orchestrator(
        state_store=state_store,
        registry=registry,
        policy=policy,
        agent=agent,
    )

    run = state_store.create_run(label="operator-run", metadata={"command": normalized})

    created_tasks = []
    previous_task_id = None
    for index, step in enumerate(plan["steps"]):
        tool_name = step["tool_name"]
        tool_input = step["input_json"]
        idempotency_key = make_idempotency_key(step, normalized, index)
        depends_on = [previous_task_id] if plan["mode"] == "graph" and previous_task_id else None
        task = state_store.create_task(
            run_id=run.id,
            title=step["title"],
            description="Operator command step",
            input_json={"tool": tool_name, "payload": tool_input},
            depends_on=depends_on,
            idempotency_key=idempotency_key,
        )
        created_tasks.append(task)
        previous_task_id = task.id

    if plan["mode"] == "single":
        task = created_tasks[0]
        orchestrator.run_tool(run.id, task, task.input_json["tool"], task.input_json["payload"])
    else:
        orchestrator.run_task_graph(run.id)

    _print_operator_summary(state_store, run.id)


def run_show(db_path: str, run_id: str) -> None:
    state_store = StateStore(db_path)
    run = state_store.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        raise SystemExit(2)

    tasks = list(state_store.list_tasks(run.id))
    tool_calls = list(state_store.list_tool_calls(run.id))
    status = _run_status(tasks)
    start_time, end_time = _run_time_bounds(run, tasks, tool_calls)

    print("=== GISMO Run Summary ===")
    print(f"Run ID:     {run.id}")
    print(f"Status:     {status}")
    print(f"Started:    {_fmt_dt(start_time)}")
    print(f"Finished:   {_fmt_dt(end_time)}")
    print("Tasks:")
    if not tasks:
        print("  (no tasks)")
        return

    for task in tasks:
        print(f"- {task.id} {task.title} [{task.status.value}]")
        if task.error:
            print(f"  error: {_summarize_value(task.error, 200)}")
        if task.output_json:
            print(f"  output: {_summarize_value(task.output_json, 200)}")
        task_calls = list(state_store.list_tool_calls_for_task(task.id))
        if not task_calls:
            print("  Tool Calls: none")
            continue
        print("  Tool Calls:")
        for call in task_calls:
            print(
                f"    - {call.id} tool={call.tool_name} status={call.status.value} "
                f"started={_fmt_dt(call.started_at)} finished={_fmt_dt(call.finished_at)}"
            )
            if call.output_json:
                print(f"      output: {_summarize_value(call.output_json, 200)}")
            if call.error:
                print(f"      error: {_summarize_value(call.error, 200)}")


def run_export(
    db_path: str,
    *,
    run_id: str | None,
    use_latest: bool,
    export_format: str,
    out_path: str | None,
    redact: bool,
    policy_path: str | None,
) -> None:
    if export_format != "jsonl":
        raise ValueError("Only jsonl export is supported")
    if run_id and use_latest:
        raise ValueError("Provide either --run or --latest, not both")
    if not run_id and not use_latest:
        raise ValueError("Export requires --run or --latest")

    repo_root = Path(__file__).resolve().parents[2]
    state_store = StateStore(db_path)
    policy_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    policy = load_policy(policy_path, repo_root=repo_root)
    base_dir = policy.fs.base_dir
    if use_latest:
        export_path = export_latest_run_jsonl(
            state_store,
            out_path=out_path,
            redact=redact,
            base_dir=base_dir,
        )
    else:
        export_path = export_run_jsonl(
            state_store,
            run_id,
            out_path=out_path,
            redact=redact,
            base_dir=base_dir,
        )
    print(f"Exported run audit to {export_path}")


def run_enqueue(
    db_path: str,
    command_text: str,
    *,
    run_id: str | None,
    max_retries: int,
    timeout_seconds: int,
) -> None:
    state_store = StateStore(db_path)
    item = state_store.enqueue_command(
        command_text=command_text,
        run_id=run_id,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
    )
    print(f"Enqueued {item.id} status={item.status.value}")


def run_daemon(
    db_path: str,
    policy_path: str | None,
    *,
    sleep_seconds: float,
    once: bool,
    requeue_stale_seconds: int,
) -> None:
    state_store = StateStore(db_path)
    state_store.requeue_stale_in_progress(older_than_seconds=requeue_stale_seconds)
    run_daemon_loop(
        state_store,
        policy_path=policy_path,
        sleep_seconds=sleep_seconds,
        once=once,
    )


def run_daemon_install_windows_task(
    name: str,
    db_path: str,
    python_exe: str,
    user: str | None,
    force: bool,
    on_startup: bool,
) -> None:
    config = WindowsTaskConfig(
        name=name,
        db_path=db_path,
        python_exe=python_exe,
        user=user,
        force=force,
        on_startup=on_startup,
    )
    install_windows_task(config)


def run_daemon_uninstall_windows_task(name: str, *, yes: bool) -> None:
    if not yes:
        print(f"Dry run: would remove task \"{name}\".")
        print("Re-run with --yes to confirm removal.")
        return
    uninstall_windows_task(name)


def run_daemon_install_windows_startup(
    name: str,
    db_path: str,
    python_exe: str,
    *,
    force: bool,
) -> None:
    launcher_path = install_windows_startup_launcher(
        name=name,
        db_path=db_path,
        python_exe=python_exe,
        force=force,
    )
    print(f"Startup launcher: {launcher_path}")
    python_arg = quote_windows_arg(python_exe)
    print(
        "Remove with: "
        f"{python_arg} -m gismo.cli.main daemon uninstall-windows-startup --name \"{name}\" --yes"
    )


def run_daemon_uninstall_windows_startup(name: str, *, yes: bool) -> None:
    launcher_path = uninstall_windows_startup_launcher(name, yes=yes)
    if yes:
        print(f"Removed startup launcher: {launcher_path}")


def _print_operator_summary(state_store: StateStore, run_id: str) -> None:
    print("=== GISMO Operator Summary ===")
    print(f"Run: {run_id}")
    print("Tasks:")
    for task in state_store.list_tasks(run_id):
        tool_calls = list(state_store.list_tool_calls_for_task(task.id))
        skipped = sum(1 for call in tool_calls if call.status.value == "SKIPPED")
        failure_type = task.failure_type.value if task.failure_type else "NONE"
        print(
            f"- {task.id} {task.title} [{task.status.value}] "
            f"failure_type={failure_type} tool_calls={len(tool_calls)} skipped={skipped}"
        )


def _resolve_default_policy_path(policy_path: str | None, repo_root: Path) -> tuple[str | None, bool]:
    if policy_path:
        return policy_path, False
    readonly_path = repo_root / "policy" / "readonly.json"
    if readonly_path.exists():
        return str(readonly_path), False
    return None, True


def _warn_missing_default_policy() -> None:
    print(
        "Warning: no policy provided and no policy/readonly.json found; "
        "continuing with existing default tool allowances.",
        file=sys.stderr,
    )


def _handle_demo(args: argparse.Namespace) -> None:
    run_demo(args.db_path, args.policy)


def _handle_demo_graph(args: argparse.Namespace) -> None:
    run_demo_graph(args.db_path, args.policy)


def _handle_run(args: argparse.Namespace) -> None:
    if args.operator_command and args.operator_command[0] == "show":
        if len(args.operator_command) != 2:
            raise ValueError("run show requires a run id")
        run_show(args.db_path, args.operator_command[1])
        return
    run_operator(args.db_path, args.operator_command, args.policy)


def _handle_export(args: argparse.Namespace) -> None:
    run_export(
        args.db_path,
        run_id=args.run_id,
        use_latest=args.latest,
        export_format=args.format,
        out_path=args.out,
        redact=args.redact,
        policy_path=args.policy,
    )


def _handle_enqueue(args: argparse.Namespace) -> None:
    command_text = " ".join(args.operator_command).strip()
    if not command_text:
        raise ValueError("enqueue requires a command string")
    run_enqueue(
        args.db_path,
        command_text,
        run_id=args.run_id,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout_seconds,
    )


def _handle_daemon(args: argparse.Namespace) -> None:
    run_daemon(
        args.db_path,
        args.policy,
        sleep_seconds=args.sleep,
        once=args.once,
        requeue_stale_seconds=args.requeue_stale_seconds,
    )


def _handle_daemon_install_windows_task(args: argparse.Namespace) -> None:
    run_daemon_install_windows_task(
        name=args.name,
        db_path=args.db_path,
        python_exe=args.python,
        user=args.user,
        force=args.force,
        on_startup=args.on_startup,
    )


def _handle_daemon_uninstall_windows_task(args: argparse.Namespace) -> None:
    run_daemon_uninstall_windows_task(args.name, yes=args.yes)


def _handle_daemon_install_windows_startup(args: argparse.Namespace) -> None:
    run_daemon_install_windows_startup(
        name=args.name,
        db_path=args.db_path,
        python_exe=args.python,
        force=args.force,
    )


def _handle_daemon_uninstall_windows_startup(args: argparse.Namespace) -> None:
    run_daemon_uninstall_windows_startup(args.name, yes=args.yes)


def _handle_queue_stats(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    stats = state_store.queue_stats()

    if args.json:
        def _dt(v):
            return v.isoformat() if v else None
        out = {
            "db_path": args.db_path,
            "total": stats["total"],
            "by_status": stats["by_status"],
            "created_at": {
                "oldest": _dt(stats["created_at"]["oldest"]),
                "newest": _dt(stats["created_at"]["newest"]),
            },
            "updated_at": {
                "oldest": _dt(stats["updated_at"]["oldest"]),
                "newest": _dt(stats["updated_at"]["newest"]),
            },
            "attempts": stats["attempts"],
        }
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"Total: {stats['total']}")
    print("By status:")
    for status in QueueStatus:
        print(f"  {status.value:12} {stats['by_status'].get(status.value, 0)}")
    print(
        f"Created: oldest={_fmt_dt(stats['created_at']['oldest'])} "
        f"newest={_fmt_dt(stats['created_at']['newest'])}"
    )
    print(
        f"Updated: oldest={_fmt_dt(stats['updated_at']['oldest'])} "
        f"newest={_fmt_dt(stats['updated_at']['newest'])}"
    )
    print(
        f"Attempts: items_with_attempts={stats['attempts']['items_with_attempts']} "
        f"max_attempt_count={stats['attempts']['max_attempt_count']}"
    )


def _handle_queue_list(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    status = QueueStatus(args.status) if args.status else None
    items = state_store.list_queue_items(
        status=status,
        limit=args.limit,
        newest_first=not args.oldest,
    )

    if args.json:
        out = []
        for it in items:
            out.append(
                {
                    "id": it.id,
                    "run_id": it.run_id,
                    "status": it.status.value,
                    "created_at": it.created_at.isoformat(),
                    "updated_at": it.updated_at.isoformat(),
                    "started_at": it.started_at.isoformat() if it.started_at else None,
                    "finished_at": it.finished_at.isoformat() if it.finished_at else None,
                    "attempt_count": it.attempt_count,
                    "max_attempts": it.max_retries,
                    "max_retries": it.max_retries,
                    "next_attempt_at": it.next_attempt_at.isoformat()
                    if it.next_attempt_at
                    else None,
                    "timeout_seconds": it.timeout_seconds,
                    "cancel_requested": it.cancel_requested,
                    "last_error": it.last_error,
                    "command_text": it.command_text,
                }
            )
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"Items: {len(items)} (limit={args.limit})")
    header = (
        f"{'ID':8}  {'STATUS':12}  {'ATT':7}  {'CREATED':20}  "
        f"{'UPDATED':20}  {'LAST ERROR':30}  COMMAND"
    )
    print(header)
    print("-" * len(header))
    cmd_width = 200 if args.full else 60
    error_width = 80 if args.full else 30
    for it in items:
        att = f"{it.attempt_count}/{it.max_retries}"
        last_error = _summarize_value(it.last_error, error_width)
        cmd = it.command_text if args.full else _truncate(it.command_text, cmd_width)
        print(
            f"{it.id[:8]:8}  {it.status.value:12}  {att:7}  "
            f"{_fmt_dt(it.created_at):20}  {_fmt_dt(it.updated_at):20}  "
            f"{last_error:{error_width}}  {cmd}"
        )


def _handle_queue_show(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)

    matches = state_store.resolve_queue_item_id(args.id)
    if not matches:
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)

    if len(matches) > 1:
        print(f"Ambiguous id prefix: {args.id}")
        print("Matches:")
        for mid in matches[:10]:
            print(f"  {mid}")
        if len(matches) > 10:
            print(f"  ... ({len(matches) - 10} more)")
        print("Provide a longer prefix.")
        raise SystemExit(2)

    item = state_store.get_queue_item(matches[0])
    if item is None:
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)

    if args.json:
        out = {
            "id": item.id,
            "run_id": item.run_id,
            "status": item.status.value,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_retries,
            "max_retries": item.max_retries,
            "next_attempt_at": item.next_attempt_at.isoformat()
            if item.next_attempt_at
            else None,
            "timeout_seconds": item.timeout_seconds,
            "cancel_requested": item.cancel_requested,
            "last_error": item.last_error,
            "command_text": item.command_text,
        }
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"ID:         {item.id}")
    print(f"Run ID:     {item.run_id or '-'}")
    print(f"Status:     {item.status.value}")
    print(f"Created:    {_fmt_dt(item.created_at)}")
    print(f"Updated:    {_fmt_dt(item.updated_at)}")
    print(f"Started:    {_fmt_dt(item.started_at)}")
    print(f"Finished:   {_fmt_dt(item.finished_at)}")
    print(f"Attempts:   {item.attempt_count}/{item.max_retries}")
    if item.last_error:
        print("Last error:")
        print(item.last_error)
    print("Command:")
    print(item.command_text)


def _handle_queue_purge_failed(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    failed_items = state_store.list_queue_items_by_status(QueueStatus.FAILED)
    if args.yes:
        deleted = state_store.delete_queue_items_by_status(QueueStatus.FAILED)
        print(f"Deleted {deleted} failed queue item(s).")
        return

    print(f"Dry run: would delete {len(failed_items)} failed queue item(s).")
    if not failed_items:
        return
    header = f"{'ID':8}  {'CREATED':20}  {'ATT':7}  {'LAST ERROR':30}  COMMAND"
    print(header)
    print("-" * len(header))
    cmd_width = 80
    for item in failed_items:
        att = f"{item.attempt_count}/{item.max_retries}"
        last_error = _summarize_value(item.last_error, 30)
        cmd = _truncate(item.command_text, cmd_width)
        print(
            f"{item.id[:8]:8}  {_fmt_dt(item.created_at):20}  {att:7}  "
            f"{last_error:30}  {cmd}"
        )


def _handle_ipc_serve(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None) or str(ipc_cli.DEFAULT_DB_PATH)
    ipc_cli.serve_ipc(db_path, token)


def _print_ipc_connection_error() -> None:
    print("IPC server not running. Start it with: python -m gismo.cli.main ipc serve")
    print("Ensure GISMO_IPC_TOKEN matches on server and client.")


def _handle_ipc_enqueue(args: argparse.Namespace) -> None:
    command_text = " ".join(args.operator_command).strip()
    if not command_text:
        raise ValueError("ipc enqueue requires a command string")
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "enqueue",
                {
                    "command": command_text,
                    "run_id": args.run_id,
                    "max_retries": args.max_retries,
                    "timeout_seconds": args.timeout_seconds,
                },
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_enqueue_output(response.data or {}))


def _handle_queue_cancel(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    item = state_store.request_queue_item_cancel(args.id)
    if item is None:
        print(f"Queue item not found: {args.id}")
        raise SystemExit(2)
    if item.status == QueueStatus.CANCELLED:
        print(f"Cancelled queue item {item.id}.")
        return
    if item.status == QueueStatus.IN_PROGRESS:
        print(f"Cancel requested for in-progress queue item {item.id}.")
        return
    print(f"Queue item already completed: {item.id} status={item.status.value}.")


def _handle_ipc_queue_cancel(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_cancel",
                {"queue_item_id": args.id},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        elif response.error == "not_found":
            print(f"Queue item not found: {args.id}")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_cancel_output(response.data or {}))


def _handle_ipc_ping(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("ping", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_ping_output(response.data or {}))


def _handle_ipc_queue_stats(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("queue_stats", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_stats_output(response.data or {}))


def _handle_ipc_run_show(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "run_show",
                {"run_id": args.run_id},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        elif response.error == "not_found":
            print(f"Run not found: {args.run_id}")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_run_show_output(response.data or {}))


def _handle_ipc_daemon_status(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_status", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_status_output(response.data or {}))


def _handle_ipc_daemon_pause(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_pause", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_pause_output(response.data or {}))


def _handle_ipc_daemon_resume(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request("daemon_resume", {}, token, getattr(args, "db_path", None))
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_daemon_resume_output(response.data or {}))


def _handle_ipc_purge_failed(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_purge_failed",
                {},
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_purge_failed_output(response.data or {}))


def _handle_ipc_requeue_stale(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    payload = {"older_than_minutes": args.older_than_minutes, "limit": args.limit}
    try:
        response = ipc_cli.parse_ipc_response(
            ipc_cli.ipc_request(
                "queue_requeue_stale",
                payload,
                token,
                getattr(args, "db_path", None),
            )
        )
    except ipc_cli.IPCConnectionError:
        _print_ipc_connection_error()
        raise SystemExit(2)
    if not response.ok:
        if response.error == "unauthorized":
            print("IPC unauthorized")
        else:
            print(f"IPC error: {response.error or 'unknown error'}")
        raise SystemExit(2)
    print(ipc_cli.format_queue_requeue_stale_output(response.data or {}))


def _handle_supervise_up(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None) or str(ipc_cli.DEFAULT_DB_PATH)
    supervise_cli.run_supervise_up(db_path, token)


def _handle_supervise_status(args: argparse.Namespace) -> None:
    try:
        token = ipc_cli.load_ipc_token(args.token)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    db_path = getattr(args, "db_path", None)
    supervise_cli.run_supervise_status(token, db_path=db_path)


def _handle_supervise_down(_args: argparse.Namespace) -> None:
    supervise_cli.run_supervise_down()


def build_parser() -> argparse.ArgumentParser:
    default_db_path = str(Path(".gismo") / "state.db")
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db",
        "--db-path",
        dest="db_path",
        default=default_db_path,
        help="Path to SQLite state database",
    )
    db_parent_optional = argparse.ArgumentParser(add_help=False)
    db_parent_optional.add_argument(
        "--db",
        "--db-path",
        dest="db_path",
        default=argparse.SUPPRESS,
        help="Path to SQLite state database",
    )
    parser = argparse.ArgumentParser(description="GISMO CLI", parents=[db_parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the demo workflow",
        parents=[db_parent_optional],
    )
    demo_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    demo_parser.set_defaults(handler=_handle_demo)

    demo_graph_parser = subparsers.add_parser(
        "demo-graph",
        help="Run the task graph demo",
        parents=[db_parent_optional],
    )
    demo_graph_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    demo_graph_parser.set_defaults(handler=_handle_demo_graph)

    run_parser = subparsers.add_parser(
        "run",
        help="Run an operator command",
        parents=[db_parent_optional],
    )
    run_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    run_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string (echo:, note:, or graph:)",
    )
    run_parser.set_defaults(handler=_handle_run)

    export_parser = subparsers.add_parser(
        "export",
        help="Export run audit trail",
        parents=[db_parent_optional],
    )
    export_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    export_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Run ID to export",
    )
    export_parser.add_argument(
        "--latest",
        action="store_true",
        help="Export the most recent run",
    )
    export_parser.add_argument(
        "--format",
        default="jsonl",
        help="Export format (jsonl only)",
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="Output file path (defaults to exports/<run_id>.jsonl)",
    )
    export_parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact file contents, shell output, and large tool outputs",
    )
    export_parser.set_defaults(handler=_handle_export)

    enqueue_parser = subparsers.add_parser(
        "enqueue",
        help="Enqueue an operator command",
        parents=[db_parent_optional],
    )
    enqueue_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Optional existing run ID to attach tasks to",
    )
    enqueue_parser.add_argument(
        "--retries",
        type=int,
        default=3,
        dest="max_retries",
        help="Maximum retries for this queue item",
    )
    enqueue_parser.add_argument(
        "--max-attempts",
        type=int,
        dest="max_retries",
        help="Alias for --retries (maximum attempts for this queue item)",
    )
    enqueue_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        dest="timeout_seconds",
        help="Timeout in seconds for this queue item (default: 300)",
    )
    enqueue_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string to enqueue",
    )
    enqueue_parser.set_defaults(handler=_handle_enqueue)

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run the GISMO daemon loop",
        parents=[db_parent_optional],
    )
    daemon_parser.add_argument(
        "--policy",
        default=None,
        help="Path to a JSON policy file",
    )
    daemon_parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="Sleep interval between queue polls",
    )
    daemon_parser.add_argument(
        "--once",
        action="store_true",
        help="Process queued items once and exit when the queue is empty",
    )
    daemon_parser.add_argument(
        "--requeue-stale-seconds",
        type=int,
        default=600,
        help="Requeue IN_PROGRESS items older than this many seconds",
    )
    daemon_parser.set_defaults(handler=_handle_daemon)
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_install_parser = daemon_subparsers.add_parser(
        "install-windows-task",
        help="Install a Windows Task Scheduler entry for the daemon",
        parents=[db_parent_optional],
    )
    daemon_install_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Task Scheduler task name",
    )
    daemon_install_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run the daemon",
    )
    daemon_install_parser.add_argument(
        "--user",
        default=None,
        help="Optional Windows username for the task (defaults to current user)",
    )
    daemon_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite task if it already exists",
    )
    daemon_install_parser.add_argument(
        "--on-startup",
        action="store_true",
        help="Also trigger at system startup (may require Administrator)",
    )
    daemon_install_parser.set_defaults(handler=_handle_daemon_install_windows_task)
    daemon_uninstall_parser = daemon_subparsers.add_parser(
        "uninstall-windows-task",
        help="Remove the Windows Task Scheduler entry for the daemon",
    )
    daemon_uninstall_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Task Scheduler task name",
    )
    daemon_uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal (required to delete the task)",
    )
    daemon_uninstall_parser.set_defaults(handler=_handle_daemon_uninstall_windows_task)
    daemon_install_startup_parser = daemon_subparsers.add_parser(
        "install-windows-startup",
        help="Install a Windows Startup folder entry for the daemon",
        parents=[db_parent_optional],
    )
    daemon_install_startup_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Startup launcher base name",
    )
    daemon_install_startup_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run the daemon",
    )
    daemon_install_startup_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite launcher if it already exists",
    )
    daemon_install_startup_parser.set_defaults(handler=_handle_daemon_install_windows_startup)
    daemon_uninstall_startup_parser = daemon_subparsers.add_parser(
        "uninstall-windows-startup",
        help="Remove the Windows Startup folder entry for the daemon",
    )
    daemon_uninstall_startup_parser.add_argument(
        "--name",
        default="GISMO Daemon",
        help="Startup launcher base name",
    )
    daemon_uninstall_startup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal (required to delete the launcher)",
    )
    daemon_uninstall_startup_parser.set_defaults(handler=_handle_daemon_uninstall_windows_startup)

    supervise_parser = subparsers.add_parser(
        "supervise",
        aliases=["svc"],
        help="Run IPC + daemon together",
        parents=[db_parent_optional],
    )
    supervise_subparsers = supervise_parser.add_subparsers(
        dest="supervise_command",
        required=True,
    )

    supervise_up_parser = supervise_subparsers.add_parser(
        "up",
        help="Start IPC server and daemon worker",
        parents=[db_parent_optional],
    )
    supervise_up_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    supervise_up_parser.set_defaults(handler=_handle_supervise_up)

    supervise_status_parser = supervise_subparsers.add_parser(
        "status",
        help="Show supervisor status",
        parents=[db_parent_optional],
    )
    supervise_status_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    supervise_status_parser.set_defaults(handler=_handle_supervise_status)

    supervise_down_parser = supervise_subparsers.add_parser(
        "down",
        help="Stop supervisor-managed processes",
        parents=[db_parent_optional],
    )
    supervise_down_parser.set_defaults(handler=_handle_supervise_down)

    queue_parser = subparsers.add_parser(
        "queue",
        help="Inspect the queue (stats, list, show)",
        parents=[db_parent],
    )
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command", required=True)

    queue_stats_parser = queue_subparsers.add_parser(
        "stats",
        help="Show queue summary statistics",
        parents=[db_parent_optional],
    )
    queue_stats_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_stats_parser.set_defaults(handler=_handle_queue_stats)

    queue_list_parser = queue_subparsers.add_parser(
        "list",
        help="List queue items",
        parents=[db_parent_optional],
    )
    queue_list_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of items to list (default: 25)",
    )
    queue_list_parser.add_argument(
        "--status",
        choices=[s.value for s in QueueStatus],
        help="Filter by status",
    )
    queue_list_parser.add_argument(
        "--oldest",
        action="store_true",
        help="Sort oldest-first (default: newest-first)",
    )
    queue_list_parser.add_argument(
        "--full",
        action="store_true",
        help="Do not truncate command text",
    )
    queue_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_list_parser.set_defaults(handler=_handle_queue_list)

    queue_show_parser = queue_subparsers.add_parser(
        "show",
        help="Show a single queue item by id",
        parents=[db_parent_optional],
    )
    queue_show_parser.add_argument("id", help="Queue item id")
    queue_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_show_parser.set_defaults(handler=_handle_queue_show)

    queue_purge_failed_parser = queue_subparsers.add_parser(
        "purge-failed",
        help="Delete FAILED queue items",
        parents=[db_parent_optional],
    )
    queue_purge_failed_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion (omit for dry-run)",
    )
    queue_purge_failed_parser.set_defaults(handler=_handle_queue_purge_failed)

    queue_cancel_parser = queue_subparsers.add_parser(
        "cancel",
        help="Request cancellation for a queue item",
        parents=[db_parent_optional],
    )
    queue_cancel_parser.add_argument("id", help="Queue item id")
    queue_cancel_parser.set_defaults(handler=_handle_queue_cancel)

    ipc_parser = subparsers.add_parser(
        "ipc",
        help="Local IPC control plane",
        parents=[db_parent_optional],
    )
    ipc_subparsers = ipc_parser.add_subparsers(dest="ipc_command", required=True)

    ipc_serve_parser = ipc_subparsers.add_parser(
        "serve",
        help="Start the IPC server",
        parents=[db_parent_optional],
    )
    ipc_serve_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_serve_parser.set_defaults(handler=_handle_ipc_serve)

    ipc_enqueue_parser = ipc_subparsers.add_parser(
        "enqueue",
        help="Enqueue an operator command via IPC",
        parents=[db_parent_optional],
    )
    ipc_enqueue_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_enqueue_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Optional existing run ID to attach tasks to",
    )
    ipc_enqueue_parser.add_argument(
        "--retries",
        type=int,
        default=3,
        dest="max_retries",
        help="Maximum retries for this queue item",
    )
    ipc_enqueue_parser.add_argument(
        "--max-attempts",
        type=int,
        dest="max_retries",
        help="Alias for --retries (maximum attempts for this queue item)",
    )
    ipc_enqueue_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        dest="timeout_seconds",
        help="Timeout in seconds for this queue item (default: 300)",
    )
    ipc_enqueue_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string to enqueue",
    )
    ipc_enqueue_parser.set_defaults(handler=_handle_ipc_enqueue)

    ipc_ping_parser = ipc_subparsers.add_parser(
        "ping",
        help="Ping the IPC server",
        parents=[db_parent_optional],
    )
    ipc_ping_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_ping_parser.set_defaults(handler=_handle_ipc_ping)

    ipc_queue_stats_parser = ipc_subparsers.add_parser(
        "queue-stats",
        help="Show queue summary statistics via IPC",
        parents=[db_parent_optional],
    )
    ipc_queue_stats_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_queue_stats_parser.set_defaults(handler=_handle_ipc_queue_stats)

    ipc_daemon_status_parser = ipc_subparsers.add_parser(
        "daemon-status",
        help="Show daemon status via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_status_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_status_parser.set_defaults(handler=_handle_ipc_daemon_status)

    ipc_daemon_pause_parser = ipc_subparsers.add_parser(
        "daemon-pause",
        help="Pause daemon processing via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_pause_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_pause_parser.set_defaults(handler=_handle_ipc_daemon_pause)

    ipc_daemon_resume_parser = ipc_subparsers.add_parser(
        "daemon-resume",
        help="Resume daemon processing via IPC",
        parents=[db_parent_optional],
    )
    ipc_daemon_resume_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_daemon_resume_parser.set_defaults(handler=_handle_ipc_daemon_resume)

    ipc_purge_failed_parser = ipc_subparsers.add_parser(
        "purge-failed",
        help="Delete failed queue items via IPC",
        parents=[db_parent_optional],
    )
    ipc_purge_failed_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_purge_failed_parser.set_defaults(handler=_handle_ipc_purge_failed)

    ipc_requeue_stale_parser = ipc_subparsers.add_parser(
        "requeue-stale",
        help="Requeue stale in-progress items via IPC",
        parents=[db_parent_optional],
    )
    ipc_requeue_stale_parser.add_argument(
        "--older-than-minutes",
        type=int,
        required=True,
        help="Requeue items older than this many minutes",
    )
    ipc_requeue_stale_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of stale items to requeue",
    )
    ipc_requeue_stale_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_requeue_stale_parser.set_defaults(handler=_handle_ipc_requeue_stale)

    ipc_queue_cancel_parser = ipc_subparsers.add_parser(
        "queue-cancel",
        help="Request cancellation for a queue item via IPC",
        parents=[db_parent_optional],
    )
    ipc_queue_cancel_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_queue_cancel_parser.add_argument("id", help="Queue item id")
    ipc_queue_cancel_parser.set_defaults(handler=_handle_ipc_queue_cancel)

    ipc_run_show_parser = ipc_subparsers.add_parser(
        "run-show",
        help="Show a run summary via IPC",
        parents=[db_parent_optional],
    )
    ipc_run_show_parser.add_argument(
        "--token",
        default=None,
        help="IPC auth token (or set GISMO_IPC_TOKEN)",
    )
    ipc_run_show_parser.add_argument(
        "run_id",
        help="Run ID to show",
    )
    ipc_run_show_parser.set_defaults(handler=_handle_ipc_run_show)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("No command provided.")
    handler(args)


def _build_registry(state_store: StateStore, policy: PermissionPolicy) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(WriteNoteTool(state_store))
    fs_config = FileSystemConfig(base_dir=policy.fs.base_dir)
    registry.register(ReadFileTool(fs_config))
    registry.register(WriteFileTool(fs_config))
    registry.register(ListDirTool(fs_config))
    shell_config = ShellConfig(
        base_dir=policy.shell.base_dir,
        allowlist=policy.shell.allowlist,
        timeout_seconds=policy.shell.timeout_seconds,
    )
    registry.register(ShellTool(shell_config))
    return registry


if __name__ == "__main__":
    main()
