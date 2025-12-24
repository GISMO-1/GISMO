"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
from pathlib import Path

from gismo.cli.operator import (
    make_idempotency_key,
    normalize_command,
    parse_command,
    required_tools,
)
from gismo.core.agent import SimpleAgent
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.tools import EchoTool, ToolRegistry, WriteNoteTool


def run_demo(db_path: str) -> None:
    state_store = StateStore(db_path)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(WriteNoteTool(state_store))

    policy = PermissionPolicy(allowed_tools={"echo"})
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


def run_demo_graph(db_path: str) -> None:
    state_store = StateStore(db_path)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(WriteNoteTool(state_store))

    policy = PermissionPolicy(allowed_tools={"echo", "write_note"})
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


def run_operator(db_path: str, command_parts: list[str]) -> None:
    command_text = " ".join(command_parts).strip()
    if not command_text:
        raise ValueError("Operator run requires a command string.")

    state_store = StateStore(db_path)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(WriteNoteTool(state_store))

    plan = parse_command(command_text)
    normalized = normalize_command(command_text)
    policy = PermissionPolicy(allowed_tools=required_tools(plan))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GISMO CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo_parser = subparsers.add_parser("demo", help="Run the demo workflow")
    demo_parser.add_argument(
        "--db-path",
        default=str(Path(".gismo") / "state.db"),
        help="Path to SQLite state database",
    )
    demo_graph_parser = subparsers.add_parser("demo-graph", help="Run the task graph demo")
    demo_graph_parser.add_argument(
        "--db-path",
        default=str(Path(".gismo") / "state.db"),
        help="Path to SQLite state database",
    )
    run_parser = subparsers.add_parser("run", help="Run an operator command")
    run_parser.add_argument(
        "--db-path",
        default=str(Path(".gismo") / "state.db"),
        help="Path to SQLite state database",
    )
    run_parser.add_argument(
        "operator_command",
        nargs=argparse.REMAINDER,
        help="Operator command string (echo:, note:, or graph:)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "demo":
        run_demo(args.db_path)
    elif args.command == "demo-graph":
        run_demo_graph(args.db_path)
    elif args.command == "run":
        run_operator(args.db_path, args.operator_command)


if __name__ == "__main__":
    main()
