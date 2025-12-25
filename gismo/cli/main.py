"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gismo.cli.operator import (
    make_idempotency_key,
    normalize_command,
    parse_command,
    required_tools,
)
from gismo.core.agent import SimpleAgent
from gismo.core.daemon import run_daemon_loop
from gismo.core.export import export_latest_run_jsonl, export_run_jsonl
from gismo.core.models import QueueStatus
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
    max_attempts: int,
) -> None:
    state_store = StateStore(db_path)
    item = state_store.enqueue_command(
        command_text=command_text,
        run_id=run_id,
        max_attempts=max_attempts,
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
        max_attempts=args.max_attempts,
    )


def _handle_daemon(args: argparse.Namespace) -> None:
    run_daemon(
        args.db_path,
        args.policy,
        sleep_seconds=args.sleep,
        once=args.once,
        requeue_stale_seconds=args.requeue_stale_seconds,
    )


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
                    "max_attempts": it.max_attempts,
                    "last_error": it.last_error,
                    "command_text": it.command_text,
                }
            )
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {args.db_path}")
    print(f"Items: {len(items)} (limit={args.limit})")
    header = f"{'ID':8}  {'STATUS':12}  {'ATT':7}  {'CREATED':20}  {'UPDATED':20}  COMMAND"
    print(header)
    print("-" * len(header))
    cmd_width = 200 if args.full else 80
    for it in items:
        att = f"{it.attempt_count}/{it.max_attempts}"
        cmd = it.command_text if args.full else _truncate(it.command_text, cmd_width)
        print(
            f"{it.id[:8]:8}  {it.status.value:12}  {att:7}  "
            f"{_fmt_dt(it.created_at):20}  {_fmt_dt(it.updated_at):20}  {cmd}"
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
            "max_attempts": item.max_attempts,
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
    print(f"Attempts:   {item.attempt_count}/{item.max_attempts}")
    if item.last_error:
        print("Last error:")
        print(item.last_error)
    print("Command:")
    print(item.command_text)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GISMO CLI")
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db",
        "--db-path",
        dest="db_path",
        default=str(Path(".gismo") / "state.db"),
        help="Path to SQLite state database",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the demo workflow",
        parents=[db_parent],
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
        parents=[db_parent],
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
        parents=[db_parent],
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
        parents=[db_parent],
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
        parents=[db_parent],
    )
    enqueue_parser.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="Optional existing run ID to attach tasks to",
    )
    enqueue_parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum attempts for this queue item",
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
        parents=[db_parent],
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

    queue_parser = subparsers.add_parser(
        "queue",
        help="Inspect the queue (stats, list, show)",
        parents=[db_parent],
    )
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command", required=True)

    queue_stats_parser = queue_subparsers.add_parser(
        "stats",
        help="Show queue summary statistics",
        parents=[db_parent],
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
        parents=[db_parent],
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
        parents=[db_parent],
    )
    queue_show_parser.add_argument("id", help="Queue item id")
    queue_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    queue_show_parser.set_defaults(handler=_handle_queue_show)

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
