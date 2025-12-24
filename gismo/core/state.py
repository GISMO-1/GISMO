"""SQLite-backed persistent state store."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from gismo.core.models import Run, Task, TaskStatus, ToolCall, ToolCallStatus


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    label TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT,
                    error TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    input_json TEXT NOT NULL,
                    output_json TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
                """
            )
            connection.commit()

    def create_run(self, label: str, metadata: Optional[Dict[str, Any]] = None) -> Run:
        run = Run(label=label, metadata_json=metadata or {})
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (id, created_at, label, metadata_json) VALUES (?, ?, ?, ?)",
                (
                    run.id,
                    run.created_at.isoformat(),
                    run.label,
                    json.dumps(run.metadata_json),
                ),
            )
            connection.commit()
        return run

    def create_task(
        self,
        run_id: str,
        title: str,
        description: str,
        input_json: Dict[str, Any],
    ) -> Task:
        task = Task(
            run_id=run_id,
            title=title,
            description=description,
            input_json=input_json,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, run_id, title, description, status,
                    created_at, updated_at, input_json, output_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.run_id,
                    task.title,
                    task.description,
                    task.status.value,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    json.dumps(task.input_json),
                    json.dumps(task.output_json) if task.output_json is not None else None,
                    task.error,
                ),
            )
            connection.commit()
        return task

    def update_task(self, task: Task) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, output_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    task.status.value,
                    task.updated_at.isoformat(),
                    json.dumps(task.output_json) if task.output_json is not None else None,
                    task.error,
                    task.id,
                ),
            )
            connection.commit()

    def record_tool_call(self, tool_call: ToolCall) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls (
                    id, run_id, task_id, tool_name, started_at, finished_at,
                    input_json, output_json, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_call.id,
                    tool_call.run_id,
                    tool_call.task_id,
                    tool_call.tool_name,
                    tool_call.started_at.isoformat(),
                    tool_call.finished_at.isoformat() if tool_call.finished_at else None,
                    json.dumps(tool_call.input_json),
                    json.dumps(tool_call.output_json) if tool_call.output_json is not None else None,
                    tool_call.status.value,
                    tool_call.error,
                ),
            )
            connection.commit()

    def update_tool_call(self, tool_call: ToolCall) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_calls
                SET finished_at = ?, output_json = ?, status = ?, error = ?
                WHERE id = ?
                """,
                (
                    tool_call.finished_at.isoformat() if tool_call.finished_at else None,
                    json.dumps(tool_call.output_json) if tool_call.output_json is not None else None,
                    tool_call.status.value,
                    tool_call.error,
                    tool_call.id,
                ),
            )
            connection.commit()

    def list_tasks(self, run_id: str) -> Iterable[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tool_calls(self, run_id: str) -> Iterable[ToolCall]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        task = Task(
            id=row["id"],
            run_id=row["run_id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            input_json=json.loads(row["input_json"]),
            output_json=json.loads(row["output_json"]) if row["output_json"] else None,
            error=row["error"],
        )
        return task

    def _row_to_tool_call(self, row: sqlite3.Row) -> ToolCall:
        tool_call = ToolCall(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            tool_name=row["tool_name"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]) if row["finished_at"] else None,
            input_json=json.loads(row["input_json"]),
            output_json=json.loads(row["output_json"]) if row["output_json"] else None,
            status=ToolCallStatus(row["status"]),
            error=row["error"],
        )
        return tool_call


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
