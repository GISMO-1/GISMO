"""CLI entrypoint for GISMO."""
from __future__ import annotations

import argparse
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GISMO CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo_parser = subparsers.add_parser("demo", help="Run the demo workflow")
    demo_parser.add_argument(
        "--db-path",
        default=str(Path(".gismo") / "state.db"),
        help="Path to SQLite state database",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "demo":
        run_demo(args.db_path)


if __name__ == "__main__":
    main()
