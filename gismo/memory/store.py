"""SQLite-backed memory storage primitives."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

MAX_EVENT_STRING_LEN = 1000


@dataclass(frozen=True)
class MemoryItem:
    id: str
    namespace: str
    key: str
    kind: str
    value: Any
    tags: list[str]
    confidence: str
    source: str
    ttl_seconds: Optional[int]
    is_tombstoned: bool
    created_at: str
    updated_at: str


class MemoryStore:
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
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    tags_json TEXT NULL,
                    confidence TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ttl_seconds INTEGER NULL,
                    is_tombstoned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_meta_json TEXT NOT NULL,
                    related_run_id TEXT NULL,
                    related_ask_event_id TEXT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_namespace_key
                ON memory_items (namespace, key)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_items_namespace
                ON memory_items (namespace)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_items_kind
                ON memory_items (kind)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_items_tombstoned
                ON memory_items (is_tombstoned)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_timestamp
                ON memory_events (timestamp)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_operation
                ON memory_events (operation)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_actor
                ON memory_events (actor)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_related_run
                ON memory_events (related_run_id)
                """
            )
            connection.commit()

    def put_item(
        self,
        *,
        namespace: str,
        key: str,
        kind: str,
        value: Any,
        tags: Optional[list[str]],
        confidence: str,
        source: str,
        ttl_seconds: Optional[int],
        actor: str,
        policy_hash: str,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> MemoryItem:
        created_at = _utc_now().isoformat()
        updated_at = created_at
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        tags_json = json.dumps(tags, ensure_ascii=False, sort_keys=True) if tags else None
        new_id = str(uuid4())
        with self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO memory_items (
                    id,
                    namespace,
                    key,
                    kind,
                    value_json,
                    tags_json,
                    confidence,
                    source,
                    ttl_seconds,
                    is_tombstoned,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(namespace, key)
                DO UPDATE SET
                    kind = excluded.kind,
                    value_json = excluded.value_json,
                    tags_json = excluded.tags_json,
                    confidence = excluded.confidence,
                    source = excluded.source,
                    ttl_seconds = excluded.ttl_seconds,
                    is_tombstoned = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id,
                    namespace,
                    key,
                    kind,
                    value_json,
                    tags_json,
                    confidence,
                    source,
                    ttl_seconds,
                    created_at,
                    updated_at,
                ),
            )
            connection.commit()
            item = self._fetch_item(
                connection,
                namespace=namespace,
                key=key,
                include_tombstoned=True,
            )
            if item is None:
                raise RuntimeError("Failed to load memory item after put")
            request = {
                "namespace": namespace,
                "key": key,
                "kind": kind,
                "value_json": value_json,
                "tags_json": tags_json,
                "confidence": confidence,
                "source": source,
                "ttl_seconds": ttl_seconds,
            }
            result_meta = {
                "item_id": item.id,
                "updated_at": item.updated_at,
                "is_tombstoned": item.is_tombstoned,
            }
            append_event(
                connection,
                operation="put",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
            return item

    def get_item(
        self,
        namespace: str,
        key: str,
        *,
        include_tombstoned: bool,
        actor: str,
        policy_hash: str,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> MemoryItem | None:
        with self._connection() as connection:
            item = self._fetch_item(
                connection,
                namespace=namespace,
                key=key,
                include_tombstoned=include_tombstoned,
            )
            request = {
                "namespace": namespace,
                "key": key,
                "include_tombstoned": include_tombstoned,
            }
            result_meta = {
                "found": item is not None,
                "item_id": item.id if item else None,
                "updated_at": item.updated_at if item else None,
            }
            append_event(
                connection,
                operation="get",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
            return item

    def search_items(
        self,
        query: str,
        *,
        namespace: Optional[str] = None,
        kind: Optional[str] = None,
        tag: Optional[str] = None,
        source: Optional[str] = None,
        confidence_min: Optional[str] = None,
        include_tombstoned: bool = False,
        limit: int = 50,
        actor: str,
        policy_hash: str,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> list[MemoryItem]:
        filters = []
        params: list[Any] = []
        if namespace:
            filters.append("namespace = ?")
            params.append(namespace)
        if kind:
            filters.append("kind = ?")
            params.append(kind)
        if source:
            filters.append("source = ?")
            params.append(source)
        if tag:
            filters.append("tags_json LIKE ?")
            params.append(f"%\"{tag}\"%")
        if confidence_min:
            filters.append(
                "(CASE confidence "
                "WHEN 'low' THEN 1 "
                "WHEN 'medium' THEN 2 "
                "WHEN 'high' THEN 3 "
                "ELSE 0 END) >= ?"
            )
            params.append(_confidence_rank(confidence_min))
        if not include_tombstoned:
            filters.append("is_tombstoned = 0")
        if query:
            filters.append("(key LIKE ? OR value_json LIKE ?)")
            like_query = f"%{query}%"
            params.extend([like_query, like_query])
        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)
        sql = (
            "SELECT * FROM memory_items "
            f"{where_clause} "
            "ORDER BY updated_at DESC, key ASC "
            "LIMIT ?"
        )
        params.append(limit)

        with self._connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, params).fetchall()
            items = [_row_to_item(row) for row in rows]
            request = {
                "query": query,
                "namespace": namespace,
                "kind": kind,
                "tag": tag,
                "source": source,
                "confidence_min": confidence_min,
                "include_tombstoned": include_tombstoned,
                "limit": limit,
            }
            result_meta = {
                "count": len(items),
            }
            append_event(
                connection,
                operation="search",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
            return items

    def tombstone_item(
        self,
        namespace: str,
        key: str,
        *,
        actor: str,
        policy_hash: str,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> MemoryItem | None:
        updated_at = _utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE memory_items
                SET is_tombstoned = 1, updated_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (updated_at, namespace, key),
            )
            connection.commit()
            item = self._fetch_item(
                connection,
                namespace=namespace,
                key=key,
                include_tombstoned=True,
            )
            request = {
                "namespace": namespace,
                "key": key,
            }
            result_meta = {
                "found": item is not None,
                "item_id": item.id if item else None,
                "updated_at": item.updated_at if item else None,
            }
            append_event(
                connection,
                operation="delete",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
            return item

    def _fetch_item(
        self,
        connection: sqlite3.Connection,
        *,
        namespace: str,
        key: str,
        include_tombstoned: bool,
    ) -> MemoryItem | None:
        sql = "SELECT * FROM memory_items WHERE namespace = ? AND key = ?"
        params: list[Any] = [namespace, key]
        if not include_tombstoned:
            sql += " AND is_tombstoned = 0"
        cursor = connection.cursor()
        row = cursor.execute(sql, params).fetchone()
        if not row:
            return None
        return _row_to_item(row)


def put_item(
    db_path: str,
    *,
    namespace: str,
    key: str,
    kind: str,
    value: Any,
    tags: Optional[list[str]],
    confidence: str,
    source: str,
    ttl_seconds: Optional[int],
    actor: str,
    policy_hash: str,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> MemoryItem:
    return MemoryStore(db_path).put_item(
        namespace=namespace,
        key=key,
        kind=kind,
        value=value,
        tags=tags,
        confidence=confidence,
        source=source,
        ttl_seconds=ttl_seconds,
        actor=actor,
        policy_hash=policy_hash,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def get_item(
    db_path: str,
    namespace: str,
    key: str,
    *,
    include_tombstoned: bool,
    actor: str,
    policy_hash: str,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> MemoryItem | None:
    return MemoryStore(db_path).get_item(
        namespace,
        key,
        include_tombstoned=include_tombstoned,
        actor=actor,
        policy_hash=policy_hash,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def search_items(
    db_path: str,
    query: str,
    *,
    namespace: Optional[str] = None,
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    source: Optional[str] = None,
    confidence_min: Optional[str] = None,
    include_tombstoned: bool = False,
    limit: int = 50,
    actor: str,
    policy_hash: str,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> list[MemoryItem]:
    return MemoryStore(db_path).search_items(
        query,
        namespace=namespace,
        kind=kind,
        tag=tag,
        source=source,
        confidence_min=confidence_min,
        include_tombstoned=include_tombstoned,
        limit=limit,
        actor=actor,
        policy_hash=policy_hash,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def tombstone_item(
    db_path: str,
    namespace: str,
    key: str,
    *,
    actor: str,
    policy_hash: str,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> MemoryItem | None:
    return MemoryStore(db_path).tombstone_item(
        namespace,
        key,
        actor=actor,
        policy_hash=policy_hash,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def append_event(
    connection: sqlite3.Connection,
    *,
    operation: str,
    actor: str,
    policy_hash: str,
    request: dict[str, Any],
    result_meta: dict[str, Any],
    related_run_id: Optional[str],
    related_ask_event_id: Optional[str],
) -> None:
    timestamp = _utc_now().isoformat()
    event_id = str(uuid4())
    request_json = _serialize_bounded_json(request)
    result_meta_json = _serialize_bounded_json(result_meta)
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO memory_events (
            id,
            timestamp,
            operation,
            actor,
            policy_hash,
            request_json,
            result_meta_json,
            related_run_id,
            related_ask_event_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            timestamp,
            operation,
            actor,
            policy_hash,
            request_json,
            result_meta_json,
            related_run_id,
            related_ask_event_id,
        ),
    )


def policy_hash_for_path(policy_path: str | None) -> str:
    if policy_path:
        contents = Path(policy_path).read_bytes()
    else:
        contents = b"default"
    return hashlib.sha256(contents).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _confidence_rank(value: str) -> int:
    lowered = value.lower()
    if lowered == "high":
        return 3
    if lowered == "medium":
        return 2
    if lowered == "low":
        return 1
    return 0


def _row_to_item(row: sqlite3.Row) -> MemoryItem:
    tags_json = row["tags_json"]
    return MemoryItem(
        id=row["id"],
        namespace=row["namespace"],
        key=row["key"],
        kind=row["kind"],
        value=json.loads(row["value_json"]),
        tags=json.loads(tags_json) if tags_json else [],
        confidence=row["confidence"],
        source=row["source"],
        ttl_seconds=row["ttl_seconds"],
        is_tombstoned=bool(row["is_tombstoned"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _serialize_bounded_json(payload: dict[str, Any]) -> str:
    normalized = _truncate_large_strings(payload)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _truncate_large_strings(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_EVENT_STRING_LEN:
            return value
        return value[: max(0, MAX_EVENT_STRING_LEN - 1)] + "…"
    if isinstance(value, list):
        return [_truncate_large_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_large_strings(val) for key, val in value.items()}
    return value
