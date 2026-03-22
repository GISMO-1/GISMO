"""Append-only tamper-evident security event storage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


_CANONICAL_KWARGS = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}


@dataclass(frozen=True)
class SecurityEvent:
    seq: int
    id: str
    timestamp: datetime
    event_type: str
    actor: str
    action: str
    resource: str
    payload: dict[str, Any] = field(default_factory=dict)
    related_run_id: str | None = None
    related_task_id: str | None = None
    related_plan_id: str | None = None
    related_approval_id: str | None = None
    prev_event_id: str | None = None
    prev_hash: str | None = None
    event_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "payload": dict(self.payload),
            "related_run_id": self.related_run_id,
            "related_task_id": self.related_task_id,
            "related_plan_id": self.related_plan_id,
            "related_approval_id": self.related_approval_id,
            "prev_event_id": self.prev_event_id,
            "prev_hash": self.prev_hash,
            "event_hash": self.event_hash,
        }


@dataclass(frozen=True)
class SecurityEventChainStatus:
    valid: bool
    checked: int
    mismatch_seq: int | None = None
    mismatch_event_id: str | None = None
    reason: str | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked": self.checked,
            "mismatch_seq": self.mismatch_seq,
            "mismatch_event_id": self.mismatch_event_id,
            "reason": self.reason,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
        }


def ensure_security_events_schema(connection: sqlite3.Connection) -> None:
    with closing(connection.cursor()) as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS security_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                related_run_id TEXT NULL,
                related_task_id TEXT NULL,
                related_plan_id TEXT NULL,
                related_approval_id TEXT NULL,
                prev_event_id TEXT NULL,
                prev_hash TEXT NULL,
                event_hash TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_timestamp
            ON security_events (timestamp)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_type
            ON security_events (event_type)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_related_run
            ON security_events (related_run_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_related_task
            ON security_events (related_task_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_related_plan
            ON security_events (related_plan_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_related_approval
            ON security_events (related_approval_id)
            """
        )


def append_security_event(
    *,
    event_type: str,
    actor: str,
    action: str,
    resource: str,
    payload: dict[str, Any] | None = None,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    related_approval_id: str | None = None,
    event_id: str | None = None,
    timestamp: datetime | None = None,
    db_path: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> SecurityEvent:
    if not event_type or not event_type.strip():
        raise ValueError("event_type must be a non-empty string")
    if not actor or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    if not action or not action.strip():
        raise ValueError("action must be a non-empty string")
    if not resource or not resource.strip():
        raise ValueError("resource must be a non-empty string")
    owns_connection = connection is None
    if owns_connection:
        if not db_path or not str(db_path).strip():
            raise ValueError("db_path is required when connection is not provided")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
    assert connection is not None
    try:
        ensure_security_events_schema(connection)
        last_row = connection.execute(
            "SELECT seq, id, event_hash FROM security_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_event_id = str(last_row["id"]) if last_row is not None else None
        prev_hash = str(last_row["event_hash"]) if last_row is not None else None
        event_timestamp = timestamp or _utc_now()
        normalized_payload = dict(payload or {})
        normalized = {
            "id": event_id or str(uuid4()),
            "timestamp": event_timestamp.isoformat(),
            "event_type": event_type.strip(),
            "actor": actor.strip(),
            "action": action.strip(),
            "resource": resource.strip(),
            "payload": normalized_payload,
            "related_run_id": _optional_str(related_run_id),
            "related_task_id": _optional_str(related_task_id),
            "related_plan_id": _optional_str(related_plan_id),
            "related_approval_id": _optional_str(related_approval_id),
            "prev_event_id": prev_event_id,
            "prev_hash": prev_hash,
        }
        event_hash = hashlib.sha256(
            canonical_json(normalized).encode("utf-8")
        ).hexdigest()
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO security_events (
                    id,
                    timestamp,
                    event_type,
                    actor,
                    action,
                    resource,
                    payload_json,
                    related_run_id,
                    related_task_id,
                    related_plan_id,
                    related_approval_id,
                    prev_event_id,
                    prev_hash,
                    event_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["id"],
                    normalized["timestamp"],
                    normalized["event_type"],
                    normalized["actor"],
                    normalized["action"],
                    normalized["resource"],
                    json.dumps(normalized_payload, **_CANONICAL_KWARGS),
                    normalized["related_run_id"],
                    normalized["related_task_id"],
                    normalized["related_plan_id"],
                    normalized["related_approval_id"],
                    prev_event_id,
                    prev_hash,
                    event_hash,
                ),
            )
            seq = int(cursor.lastrowid)
        if owns_connection:
            connection.commit()
        return SecurityEvent(
            seq=seq,
            id=normalized["id"],
            timestamp=event_timestamp,
            event_type=normalized["event_type"],
            actor=normalized["actor"],
            action=normalized["action"],
            resource=normalized["resource"],
            payload=normalized_payload,
            related_run_id=normalized["related_run_id"],
            related_task_id=normalized["related_task_id"],
            related_plan_id=normalized["related_plan_id"],
            related_approval_id=normalized["related_approval_id"],
            prev_event_id=prev_event_id,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )
    finally:
        if owns_connection:
            connection.close()


def list_security_events(
    *,
    limit: int = 100,
    event_type: str | None = None,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    related_approval_id: str | None = None,
    event_id: str | None = None,
    db_path: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> list[SecurityEvent]:
    if limit <= 0:
        raise ValueError("limit must be > 0")
    owns_connection = connection is None
    if owns_connection:
        if not db_path or not str(db_path).strip():
            raise ValueError("db_path is required when connection is not provided")
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
    assert connection is not None
    try:
        ensure_security_events_schema(connection)
        clauses: list[str] = []
        params: list[object] = []
        if event_id and event_id.strip():
            clauses.append("id = ?")
            params.append(event_id.strip())
        if event_type and event_type.strip():
            clauses.append("event_type = ?")
            params.append(event_type.strip())
        if related_run_id and related_run_id.strip():
            clauses.append("related_run_id = ?")
            params.append(related_run_id.strip())
        if related_task_id and related_task_id.strip():
            clauses.append("related_task_id = ?")
            params.append(related_task_id.strip())
        if related_plan_id and related_plan_id.strip():
            clauses.append("related_plan_id = ?")
            params.append(related_plan_id.strip())
        if related_approval_id and related_approval_id.strip():
            clauses.append("related_approval_id = ?")
            params.append(related_approval_id.strip())
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = connection.execute(
            f"""
            SELECT * FROM security_events
            {where_clause}
            ORDER BY seq DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [_row_to_security_event(row) for row in rows]
    finally:
        if owns_connection:
            connection.close()


def get_security_event(
    *,
    event_id: str | None = None,
    seq: int | None = None,
    db_path: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> SecurityEvent | None:
    if event_id is None and seq is None:
        raise ValueError("Provide event_id or seq.")
    if event_id is not None and seq is not None:
        raise ValueError("Provide only one of event_id or seq.")
    owns_connection = connection is None
    if owns_connection:
        if not db_path or not str(db_path).strip():
            raise ValueError("db_path is required when connection is not provided")
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
    assert connection is not None
    try:
        ensure_security_events_schema(connection)
        if event_id is not None:
            row = connection.execute(
                "SELECT * FROM security_events WHERE id = ?",
                (event_id.strip(),),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM security_events WHERE seq = ?",
                (seq,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_security_event(row)
    finally:
        if owns_connection:
            connection.close()


def validate_security_event_chain(
    *,
    db_path: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> SecurityEventChainStatus:
    owns_connection = connection is None
    if owns_connection:
        if not db_path or not str(db_path).strip():
            raise ValueError("db_path is required when connection is not provided")
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
    assert connection is not None
    try:
        ensure_security_events_schema(connection)
        rows = connection.execute(
            "SELECT * FROM security_events ORDER BY seq ASC"
        ).fetchall()
        previous_id: str | None = None
        previous_hash: str | None = None
        checked = 0
        for row in rows:
            checked += 1
            event = _row_to_security_event(row)
            if event.prev_event_id != previous_id or event.prev_hash != previous_hash:
                return SecurityEventChainStatus(
                    valid=False,
                    checked=checked,
                    mismatch_seq=event.seq,
                    mismatch_event_id=event.id,
                    reason="security event chain linkage mismatch",
                    expected_hash=previous_hash,
                    actual_hash=event.prev_hash,
                )
            expected_hash = _hash_event(row)
            if event.event_hash != expected_hash:
                return SecurityEventChainStatus(
                    valid=False,
                    checked=checked,
                    mismatch_seq=event.seq,
                    mismatch_event_id=event.id,
                    reason="security event hash mismatch",
                    expected_hash=expected_hash,
                    actual_hash=event.event_hash,
                )
            previous_id = event.id
            previous_hash = event.event_hash
        return SecurityEventChainStatus(valid=True, checked=checked)
    finally:
        if owns_connection:
            connection.close()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, **_CANONICAL_KWARGS)


def _hash_event(row: sqlite3.Row) -> str:
    payload = {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "actor": row["actor"],
        "action": row["action"],
        "resource": row["resource"],
        "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
        "related_run_id": row["related_run_id"],
        "related_task_id": row["related_task_id"],
        "related_plan_id": row["related_plan_id"],
        "related_approval_id": row["related_approval_id"],
        "prev_event_id": row["prev_event_id"],
        "prev_hash": row["prev_hash"],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _row_to_security_event(row: sqlite3.Row) -> SecurityEvent:
    return SecurityEvent(
        seq=int(row["seq"]),
        id=row["id"],
        timestamp=_parse_dt(row["timestamp"]),
        event_type=row["event_type"],
        actor=row["actor"],
        action=row["action"],
        resource=row["resource"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        related_run_id=row["related_run_id"],
        related_task_id=row["related_task_id"],
        related_plan_id=row["related_plan_id"],
        related_approval_id=row["related_approval_id"],
        prev_event_id=row["prev_event_id"],
        prev_hash=row["prev_hash"],
        event_hash=row["event_hash"],
    )


def _parse_dt(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
