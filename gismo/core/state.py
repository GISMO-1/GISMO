"""SQLite-backed persistent state store."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from gismo.core.models import (
    DaemonHeartbeat,
    FailureType,
    QueueItem,
    QueueStatus,
    Run,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
)


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        self._apply_pragmas(connection)
        return connection

    def _apply_pragmas(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error:
            pass

    @contextmanager
    def _connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    id TEXT PRIMARY KEY,
                    run_id TEXT,
                    command_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    next_attempt_at TEXT,
                    timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_control (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    paused INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    version TEXT
                )
                """
            )
            self._ensure_columns(connection)
            cursor.execute(
                """
                INSERT OR IGNORE INTO daemon_control (id, paused, updated_at)
                VALUES (1, 0, ?)
                """,
                (_utc_now().isoformat(),),
            )
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
        self._ensure_column(
            connection,
            "queue_items",
            "attempt_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            connection,
            "queue_items",
            "max_attempts",
            "INTEGER NOT NULL DEFAULT 3",
        )
        self._ensure_column(
            connection,
            "queue_items",
            "max_retries",
            "INTEGER NOT NULL DEFAULT 3",
        )
        self._ensure_column(connection, "queue_items", "next_attempt_at", "TEXT")
        self._ensure_column(
            connection,
            "queue_items",
            "timeout_seconds",
            "INTEGER NOT NULL DEFAULT 300",
        )
        self._ensure_column(
            connection,
            "queue_items",
            "cancel_requested",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(connection, "queue_items", "last_error", "TEXT")
        self._ensure_column(connection, "queue_items", "started_at", "TEXT")
        self._ensure_column(connection, "queue_items", "finished_at", "TEXT")
        self._ensure_column(connection, "queue_items", "run_id", "TEXT")
        self._ensure_column(connection, "queue_items", "command_text", "TEXT NOT NULL")
        self._ensure_column(connection, "queue_items", "status", "TEXT NOT NULL")
        self._ensure_column(connection, "queue_items", "created_at", "TEXT NOT NULL")
        self._ensure_column(connection, "queue_items", "updated_at", "TEXT NOT NULL")
        self._sync_queue_retry_columns(connection)

    def _sync_queue_retry_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(queue_items)").fetchall()
        }
        if "max_attempts" in columns and "max_retries" in columns:
            connection.execute(
                """
                UPDATE queue_items
                SET max_retries = max_attempts
                WHERE max_attempts IS NOT NULL
                """
            )

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
        with self._connection() as connection:
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
        with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_tasks_by_ids(self, task_ids: list[str]) -> Iterable[Task]:
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})",
                tuple(task_ids),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tool_calls(self, run_id: str) -> Iterable[ToolCall]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def list_tool_calls_for_task(self, task_id: str) -> Iterable[ToolCall]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY started_at",
                (task_id,),
            ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[Run]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def enqueue_command(
        self,
        command_text: str,
        run_id: Optional[str] = None,
        max_retries: int = 3,
        timeout_seconds: int = 300,
    ) -> QueueItem:
        if not command_text or not command_text.strip():
            raise ValueError("command_text must be a non-empty string")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        item = QueueItem(
            command_text=command_text.strip(),
            run_id=run_id,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO queue_items (
                    id, run_id, command_text, status, created_at, updated_at,
                    started_at, finished_at, attempt_count, max_attempts, max_retries,
                    next_attempt_at, timeout_seconds, cancel_requested, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.run_id,
                    item.command_text,
                    item.status.value,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    item.started_at.isoformat() if item.started_at else None,
                    item.finished_at.isoformat() if item.finished_at else None,
                    item.attempt_count,
                    item.max_retries,
                    item.max_retries,
                    item.next_attempt_at.isoformat() if item.next_attempt_at else None,
                    item.timeout_seconds,
                    1 if item.cancel_requested else 0,
                    item.last_error,
                ),
            )
            connection.commit()
        return item

    def claim_next_queue_item(self) -> Optional[QueueItem]:
        connection = self._connect()
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _utc_now().isoformat()
            row = connection.execute(
                """
                SELECT * FROM queue_items
                WHERE status = ?
                  AND cancel_requested = 0
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (QueueStatus.QUEUED.value, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE queue_items
                SET status = ?, started_at = ?, updated_at = ?, finished_at = NULL
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (
                    QueueStatus.IN_PROGRESS.value,
                    now,
                    now,
                    row["id"],
                    QueueStatus.QUEUED.value,
                ),
            )
            if updated.rowcount == 0:
                connection.commit()
                return None
            connection.commit()
            row = connection.execute(
                "SELECT * FROM queue_items WHERE id = ?",
                (row["id"],),
            ).fetchone()
            return self._row_to_queue_item(row) if row else None
        except Exception:  # noqa: BLE001
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_queue_item_succeeded(self, item_id: str) -> None:
        now = _utc_now().isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_items
                SET status = ?, updated_at = ?, finished_at = ?, next_attempt_at = NULL
                WHERE id = ?
                """,
                (QueueStatus.SUCCEEDED.value, now, now, item_id),
            )
            connection.commit()

    def mark_queue_item_failed(self, item_id: str, error: str, retryable: bool) -> None:
        item = self.get_queue_item(item_id)
        if item is None:
            return
        now = _utc_now().isoformat()
        if retryable and item.attempt_count < item.max_retries:
            next_attempt = _utc_now() + _retry_backoff(item.attempt_count + 1)
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE queue_items
                    SET status = ?, updated_at = ?, attempt_count = ?, last_error = ?,
                        started_at = NULL, finished_at = ?, next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (
                        QueueStatus.QUEUED.value,
                        now,
                        item.attempt_count + 1,
                        error,
                        now,
                        next_attempt.isoformat(),
                        item_id,
                    ),
                )
                connection.commit()
            return
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_items
                SET status = ?, updated_at = ?, finished_at = ?, last_error = ?,
                    next_attempt_at = NULL
                WHERE id = ?
                """,
                (QueueStatus.FAILED.value, now, now, error, item_id),
            )
            connection.commit()

    def mark_queue_item_cancelled(self, item_id: str, reason: str | None = None) -> None:
        now = _utc_now().isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_items
                SET status = ?, updated_at = ?, finished_at = ?, last_error = ?,
                    cancel_requested = 1, next_attempt_at = NULL
                WHERE id = ?
                """,
                (QueueStatus.CANCELLED.value, now, now, reason, item_id),
            )
            connection.commit()

    def request_queue_item_cancel(self, item_id: str) -> Optional[QueueItem]:
        item = self.get_queue_item(item_id)
        if item is None:
            return None
        now = _utc_now().isoformat()
        if item.status in {QueueStatus.SUCCEEDED, QueueStatus.FAILED, QueueStatus.CANCELLED}:
            return item
        if item.status == QueueStatus.QUEUED:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE queue_items
                    SET status = ?, updated_at = ?, finished_at = ?, last_error = ?,
                        cancel_requested = 1, next_attempt_at = NULL
                    WHERE id = ?
                    """,
                    (
                        QueueStatus.CANCELLED.value,
                        now,
                        now,
                        "Cancellation requested before execution.",
                        item_id,
                    ),
                )
                connection.commit()
            return self.get_queue_item(item_id)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_items
                SET cancel_requested = 1, updated_at = ?
                WHERE id = ?
                """,
                (now, item_id),
            )
            connection.commit()
        return self.get_queue_item(item_id)

    def requeue_stale_in_progress(self, older_than_seconds: int = 600) -> int:
        threshold = _utc_now().timestamp() - older_than_seconds
        updated = 0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM queue_items
                WHERE status = ? AND started_at IS NOT NULL
                """,
                (QueueStatus.IN_PROGRESS.value,),
            ).fetchall()
            now = _utc_now().isoformat()
            for row in rows:
                started_at = _parse_dt(row["started_at"]).timestamp()
                if started_at >= threshold:
                    continue
                attempt_count = row["attempt_count"]
                max_retries = row["max_retries"]
                if attempt_count < max_retries:
                    connection.execute(
                        """
                        UPDATE queue_items
                        SET status = ?, updated_at = ?, attempt_count = ?, last_error = ?,
                            started_at = NULL, next_attempt_at = ?
                        WHERE id = ?
                        """,
                        (
                            QueueStatus.QUEUED.value,
                            now,
                            attempt_count + 1,
                            "Requeued stale in-progress item.",
                            now,
                            row["id"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE queue_items
                        SET status = ?, updated_at = ?, finished_at = ?, last_error = ?,
                            next_attempt_at = NULL
                        WHERE id = ?
                        """,
                        (
                            QueueStatus.FAILED.value,
                            now,
                            now,
                            "Exceeded max retries after stale in-progress.",
                            row["id"],
                        ),
                    )
                updated += 1
            connection.commit()
        return updated

    def requeue_stale_in_progress_queue(
        self,
        older_than_seconds: int,
        limit: int | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds must be > 0")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be > 0")
        current_time = now or _utc_now()
        threshold = current_time.timestamp() - older_than_seconds
        updated = 0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM queue_items
                WHERE status = ? AND started_at IS NOT NULL
                ORDER BY started_at ASC
                """,
                (QueueStatus.IN_PROGRESS.value,),
            ).fetchall()
            for row in rows:
                if limit is not None and updated >= limit:
                    break
                started_at = _parse_dt(row["started_at"]).timestamp()
                if started_at >= threshold:
                    continue
                attempt_count = row["attempt_count"]
                max_retries = row["max_retries"]
                if attempt_count < max_retries:
                    connection.execute(
                        """
                        UPDATE queue_items
                        SET status = ?, updated_at = ?, attempt_count = ?, last_error = ?,
                            started_at = NULL, next_attempt_at = ?
                        WHERE id = ?
                        """,
                        (
                            QueueStatus.QUEUED.value,
                            current_time.isoformat(),
                            attempt_count + 1,
                            "Requeued stale in-progress item.",
                            current_time.isoformat(),
                            row["id"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE queue_items
                        SET status = ?, updated_at = ?, finished_at = ?, last_error = ?,
                            next_attempt_at = NULL
                        WHERE id = ?
                        """,
                        (
                            QueueStatus.FAILED.value,
                            current_time.isoformat(),
                            current_time.isoformat(),
                            "Exceeded max retries after stale in-progress.",
                            row["id"],
                        ),
                    )
                updated += 1
            connection.commit()
        return updated

    def get_daemon_paused(self) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT paused FROM daemon_control WHERE id = 1",
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO daemon_control (id, paused, updated_at)
                    VALUES (1, 0, ?)
                    """,
                    (_utc_now().isoformat(),),
                )
                connection.commit()
                return False
            return bool(row["paused"])

    def set_daemon_paused(self, paused: bool) -> None:
        now = _utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE daemon_control
                SET paused = ?, updated_at = ?
                WHERE id = 1
                """,
                (1 if paused else 0, now),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO daemon_control (id, paused, updated_at)
                    VALUES (1, ?, ?)
                    """,
                    (1 if paused else 0, now),
                )
            connection.commit()

    def get_daemon_heartbeat(self) -> Optional[DaemonHeartbeat]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT pid, started_at, last_seen, version
                FROM daemon_heartbeat
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return DaemonHeartbeat(
            pid=int(row["pid"]),
            started_at=_parse_dt(row["started_at"]),
            last_seen=_parse_dt(row["last_seen"]),
            version=row["version"],
        )

    def set_daemon_heartbeat(
        self,
        pid: int,
        started_at: datetime,
        last_seen: datetime,
        version: str | None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE daemon_heartbeat
                SET pid = ?, started_at = ?, last_seen = ?, version = ?
                WHERE id = 1
                """,
                (pid, started_at.isoformat(), last_seen.isoformat(), version),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO daemon_heartbeat (id, pid, started_at, last_seen, version)
                    VALUES (1, ?, ?, ?, ?)
                    """,
                    (pid, started_at.isoformat(), last_seen.isoformat(), version),
                )
            connection.commit()

    def get_queue_item(self, item_id: str) -> Optional[QueueItem]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM queue_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_queue_item(row)

    def resolve_queue_item_id(self, item_id_or_prefix: str) -> list[str]:
        """Resolve an id or id prefix to matching queue item ids (0..n).

        - If an exact id match exists, returns [id].
        - Otherwise returns up to 50 ids that start with the prefix, newest-first.
        """
        value = (item_id_or_prefix or "").strip()
        if not value:
            return []

        with self._connection() as connection:
            exact = connection.execute(
                "SELECT id FROM queue_items WHERE id = ?",
                (value,),
            ).fetchone()
            if exact is not None:
                return [exact["id"]]

            rows = connection.execute(
                "SELECT id FROM queue_items WHERE id LIKE ? ORDER BY created_at DESC LIMIT 50",
                (value + "%",),
            ).fetchall()

        return [row["id"] for row in rows]

    def queue_stats(self) -> Dict[str, Any]:
        """Return summary statistics for queue_items."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM queue_items GROUP BY status"
            ).fetchall()

            total_row = connection.execute("SELECT COUNT(*) AS c FROM queue_items").fetchone()
            total = int(total_row["c"]) if total_row else 0

            range_row = connection.execute(
                """
                SELECT
                    MIN(created_at) AS oldest_created_at,
                    MAX(created_at) AS newest_created_at,
                    MIN(updated_at) AS oldest_updated_at,
                    MAX(updated_at) AS newest_updated_at
                FROM queue_items
                """
            ).fetchone()

            attempts_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN attempt_count > 0 THEN 1 ELSE 0 END) AS items_with_attempts,
                    MAX(attempt_count) AS max_attempt_count
                FROM queue_items
                """
            ).fetchone()

        counts: Dict[str, int] = {row["status"]: int(row["count"]) for row in rows}
        for status in QueueStatus:
            counts.setdefault(status.value, 0)

        def _maybe_parse(value: Optional[str]) -> Optional[datetime]:
            return _parse_dt(value) if value else None

        payload: Dict[str, Any] = {
            "total": total,
            "by_status": counts,
            "created_at": {
                "oldest": _maybe_parse(range_row["oldest_created_at"]) if range_row else None,
                "newest": _maybe_parse(range_row["newest_created_at"]) if range_row else None,
            },
            "updated_at": {
                "oldest": _maybe_parse(range_row["oldest_updated_at"]) if range_row else None,
                "newest": _maybe_parse(range_row["newest_updated_at"]) if range_row else None,
            },
            "attempts": {
                "items_with_attempts": int(attempts_row["items_with_attempts"] or 0)
                if attempts_row
                else 0,
                "max_attempt_count": int(attempts_row["max_attempt_count"] or 0)
                if attempts_row
                else 0,
            },
        }
        return payload

    def list_queue_items(
        self,
        status: Optional[QueueStatus] = None,
        limit: int = 25,
        newest_first: bool = True,
    ) -> list[QueueItem]:
        """List queue items with optional filtering."""
        if limit <= 0:
            raise ValueError("limit must be > 0")

        where = ""
        params: tuple[Any, ...] = ()
        if status is not None:
            where = "WHERE status = ?"
            params = (status.value,)

        order = "DESC" if newest_first else "ASC"
        sql = f"""
            SELECT * FROM queue_items
            {where}
            ORDER BY created_at {order}
            LIMIT ?
        """
        with self._connection() as connection:
            rows = connection.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_queue_item(row) for row in rows]

    def list_queue_items_by_status(self, status: QueueStatus) -> list[QueueItem]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM queue_items WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
        return [self._row_to_queue_item(row) for row in rows]

    def delete_queue_items_by_status(self, status: QueueStatus) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM queue_items WHERE status = ?",
                (status.value,),
            )
            connection.commit()
        return cursor.rowcount

    def get_latest_run(self) -> Optional[Run]:
        with self._connection() as connection:
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
        with self._connection() as connection:
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
        with self._connection() as connection:
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
            depends_on=json.loads(row["depends_on_json"]) if row["depends_on_json"] else [],
            idempotency_key=row["idempotency_key"],
            input_hash=row["input_hash"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            input_json=json.loads(row["input_json"]),
            output_json=json.loads(row["output_json"]) if row["output_json"] else None,
            error=row["error"],
            failure_type=FailureType(row["failure_type"]) if row["failure_type"] else FailureType.NONE,
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
            failure_type=FailureType(row["failure_type"]) if row["failure_type"] else FailureType.NONE,
        )
        return tool_call

    def _row_to_queue_item(self, row: sqlite3.Row) -> QueueItem:
        max_retries = row["max_retries"] if "max_retries" in row.keys() else None
        if max_retries is None:
            max_retries = row["max_attempts"]
        return QueueItem(
            id=row["id"],
            run_id=row["run_id"],
            command_text=row["command_text"],
            status=QueueStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            started_at=_parse_dt(row["started_at"]) if row["started_at"] else None,
            finished_at=_parse_dt(row["finished_at"]) if row["finished_at"] else None,
            attempt_count=row["attempt_count"],
            max_retries=max_retries,
            next_attempt_at=_parse_dt(row["next_attempt_at"])
            if row["next_attempt_at"]
            else None,
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row.keys() else 300,
            cancel_requested=bool(row["cancel_requested"])
            if "cancel_requested" in row.keys()
            else False,
            last_error=row["last_error"],
        )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_backoff(attempt_count: int) -> timedelta:
    if attempt_count <= 0:
        return timedelta(seconds=0)
    backoff_seconds = min(60, 2 ** (attempt_count - 1))
    return timedelta(seconds=backoff_seconds)
