import json
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gismo.cli.operator import parse_command
from gismo.core.agent import SimpleAgent
from gismo.core.models import TaskStatus
from gismo.core.orchestrator import Orchestrator
from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.core.toolpacks.calendar_tool import CalendarControlTool
from gismo.core.tools import ToolRegistry


class CalendarToolTest(unittest.TestCase):
    def _build_orchestrator(self, db_path: str) -> tuple[StateStore, Orchestrator]:
        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(CalendarControlTool(state_store))
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=PermissionPolicy(allowed_tools={"calendar_control"}),
            agent=SimpleAgent(registry=registry),
        )
        return state_store, orchestrator

    def test_add_event_succeeds(self) -> None:
        tmpdir = Path("tmp") / f"calendar-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="calendar-add", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Add event",
                description="Add a calendar event",
                input_json={
                    "tool": "calendar_control",
                    "payload": {
                        "action": "add",
                        "payload": {
                            "title": "Dinner",
                            "start_at": "2026-03-20T18:00:00",
                            "end_at": "2026-03-20T19:00:00",
                            "event_type": "event",
                        },
                    },
                },
            )
            result = orchestrator.run_tool(
                run.id,
                task,
                "calendar_control",
                {
                    "action": "add",
                    "payload": {
                        "title": "Dinner",
                        "start_at": "2026-03-20T18:00:00",
                        "end_at": "2026-03-20T19:00:00",
                    },
                },
            )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("I added Dinner", result.output_json.get("summary", ""))
            self.assertEqual(len(state_store.list_calendar_events(limit=10)), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_delete_range_removes_matching_events(self) -> None:
        tmpdir = Path("tmp") / f"calendar-tool-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            now = datetime.now(timezone.utc)
            event_a = parse_command(
                'calendar: add {"title":"Lunch","start_at":"2026-03-20T12:00:00","end_at":"2026-03-20T13:00:00"}'
            )
            event_b = parse_command(
                'calendar: add {"title":"Workout","start_at":"2026-03-21T08:00:00","end_at":"2026-03-21T09:00:00"}'
            )
            for idx, plan in enumerate([event_a, event_b], start=1):
                run = state_store.create_run(label=f"calendar-seed-{idx}", metadata={"seeded_at": now.isoformat()})
                task = state_store.create_task(
                    run_id=run.id,
                    title=plan["steps"][0]["title"],
                    description="Seed calendar",
                    input_json={"tool": plan["steps"][0]["tool_name"], "payload": plan["steps"][0]["input_json"]},
                )
                orchestrator.run_tool(run.id, task, plan["steps"][0]["tool_name"], plan["steps"][0]["input_json"])

            run = state_store.create_run(label="calendar-delete-range", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Delete range",
                description="Delete range",
                input_json={
                    "tool": "calendar_control",
                    "payload": {
                        "action": "delete_range",
                        "payload": {
                            "start_at": "2026-03-20T00:00:00",
                            "end_at": "2026-03-20T23:59:59",
                        },
                    },
                },
            )
            result = orchestrator.run_tool(
                run.id,
                task,
                "calendar_control",
                {
                    "action": "delete_range",
                    "payload": {
                        "start_at": "2026-03-20T00:00:00",
                        "end_at": "2026-03-20T23:59:59",
                    },
                },
            )

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.output_json.get("deleted_count"), 1)
            remaining = state_store.list_calendar_events(limit=10)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].title, "Workout")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
