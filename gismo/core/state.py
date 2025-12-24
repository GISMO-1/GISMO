"""SQLite-backed persistent state store."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from gismo.core.models import FailureType, Run, Task, TaskStatus, ToolCall, ToolCallStatus


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
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    input_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT,
                    error TEXT,
                    failure_type TEXT,
                    status_reason TEXT,
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
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    failure_type TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
                """
            )
            self._ensure_columns(connection)
            connection.commit()

    def _ensure_columns(self, connection: sqlite3.Connection) -> None:
        self._ensure_column(
            connection,
            "tasks",
            "idempotency_key",
            "TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            connection,
            "tasks",
            "input_hash",
            "TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            connection,
            "tasks",
            "depends_on_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        self._ensure_column(connection, "tasks", "failure_type", "TEXT")
        self._ensure_column(connection, "tasks", "status_reason", "TEXT")
        self._ensure_column(
            connection,
            "tool_calls",
            "attempt_number",
            "INTEGER NOT NULL DEFAULT 1",
        )
        self._ensure_column(connection, "tool_calls", "failure_type", "TEXT")

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def transaction(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:  # noqa: BLE001 - propagate for caller handling
            connection.rollback()
            raise
        finally:
            connection.close()

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
        depends_on: Optional[list[str]] = None,
        idempotency_key: str = "",
        input_hash: str = "",
    ) -> Task:
        task = Task(
            run_id=run_id,
            title=title,
            description=description,
            input_json=input_json,
            depends_on=list(depends_on or []),
            idempotency_key=idempotency_key,
            input_hash=input_hash,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, run_id, title, description, status,
                    depends_on_json, idempotency_key, input_hash,
                    created_at, updated_at, input_json, output_json,
                    error, failure_type, status_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.run_id,
                    task.title,
                    task.description,
                    task.status.value,
                    json.dumps(task.depends_on),
                    task.idempotency_key,
                    task.input_hash,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    json.dumps(task.input_json),
                    json.dumps(task.output_json) if task.output_json is not None else None,
                    task.error,
                    task.failure_type.value if task.failure_type else None,
                    task.status_reason,
                ),
            )
            connection.commit()
        return task

    def update_task(self, task: Task, connection: Optional[sqlite3.Connection] = None) -> None:
        if connection is None:
            with self._connect() as connection:
                self.update_task(task, connection=connection)
                connection.commit()
                return
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, output_json = ?, error = ?,
                idempotency_key = ?, input_hash = ?, failure_type = ?,
                depends_on_json = ?, status_reason = ?
            WHERE id = ?
            """,
            (
                task.status.value,
                task.updated_at.isoformat(),
                json.dumps(task.output_json) if task.output_json is not None else None,
                task.error,
                task.idempotency_key,
                task.input_hash,
                task.failure_type.value if task.failure_type else None,
                json.dumps(task.depends_on),
                task.status_reason,
                task.id,
            ),
        )

    def record_tool_call(
        self,
        tool_call: ToolCall,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        if connection is None:
            with self._connect() as connection:
                self.record_tool_call(tool_call, connection=connection)
                connection.commit()
                return
        connection.execute(
            """
            INSERT INTO tool_calls (
                id, run_id, task_id, tool_name, started_at, finished_at,
                input_json, output_json, status, error, attempt_number, failure_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                tool_call.attempt_number,
                tool_call.failure_type.value if tool_call.failure_type else None,
            ),
        )

    def update_tool_call(
        self,
        tool_call: ToolCall,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        if connection is None:
            with self._connect() as connection:
                self.update_tool_call(tool_call, connection=connection)
                connection.commit()
                return
        connection.execute(
            """
            UPDATE tool_calls
            SET finished_at = ?, output_json = ?, status = ?, error = ?, failure_type = ?
            WHERE id = ?
            """,
            (
                tool_call.finished_at.isoformat() if tool_call.finished_at else None,
                json.dumps(tool_call.output_json) if tool_call.output_json is not None else None,
                tool_call.status.value,
                tool_call.error,
                tool_call.failure_type.value if tool_call.failure_type else None,
                tool_call.id,
            ),
        )

    def list_tasks(self, run_id: str) -> Iterable[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_tasks_by_ids(self, task_ids: list[str]) -> Iterable[Task]:
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})",
                tuple(task_ids),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tool_calls(self, run_id: str) -> Iterable[ToolCall]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def list_tool_calls_for_task(self, task_id: str) -> Iterable[ToolCall]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY started_at",
                (task_id,),
            ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[Run]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def get_latest_run(self) -> Optional[Run]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1",
            ).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def find_succeeded_task_by_idempotency(
        self,
        idempotency_key: str,
        input_hash: str,
    ) -> Optional[Task]:
        if not idempotency_key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE idempotency_key = ? AND input_hash = ? AND status = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (idempotency_key, input_hash, TaskStatus.SUCCEEDED.value),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

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
            depends_on=json.loads(row["depends_on_json"])
            if row["depends_on_json"]
            else [],
            idempotency_key=row["idempotency_key"],
            input_hash=row["input_hash"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            input_json=json.loads(row["input_json"]),
            output_json=json.loads(row["output_json"]) if row["output_json"] else None,
            error=row["error"],
            failure_type=FailureType(row["failure_type"])
            if row["failure_type"]
            else FailureType.NONE,
            status_reason=row["status_reason"],
        )
        return task

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            created_at=_parse_dt(row["created_at"]),
            label=row["label"],
            metadata_json=json.loads(row["metadata_json"]),
        )

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
            attempt_number=row["attempt_number"],
            failure_type=FailureType(row["failure_type"])
            if row["failure_type"]
            else FailureType.NONE,
        )
        return tool_call


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
