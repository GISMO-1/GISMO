"""SQLite-backed memory storage primitives."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

MAX_EVENT_STRING_LEN = 1000
MEMORY_TABLE_NAMES = (
    "memory_items",
    "memory_events",
    "memory_namespaces",
    "memory_retention_rules",
    "memory_profiles",
)
MEMORY_INDEX_DEFINITIONS = (
    (
        "idx_memory_items_namespace_key",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_namespace_key
        ON memory_items (namespace, key)
        """,
    ),
    (
        "idx_memory_items_namespace",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_namespace
        ON memory_items (namespace)
        """,
    ),
    (
        "idx_memory_items_kind",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_kind
        ON memory_items (kind)
        """,
    ),
    (
        "idx_memory_items_tombstoned",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_tombstoned
        ON memory_items (is_tombstoned)
        """,
    ),
    (
        "idx_memory_events_timestamp",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_events_timestamp
        ON memory_events (timestamp)
        """,
    ),
    (
        "idx_memory_events_operation",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_events_operation
        ON memory_events (operation)
        """,
    ),
    (
        "idx_memory_events_actor",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_events_actor
        ON memory_events (actor)
        """,
    ),
    (
        "idx_memory_events_related_run",
        """
        CREATE INDEX IF NOT EXISTS idx_memory_events_related_run
        ON memory_events (related_run_id)
        """,
    ),
    (
        "idx_memory_profiles_name",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_profiles_name
        ON memory_profiles (name)
        """,
    ),
)


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


@dataclass(frozen=True)
class MemoryNamespaceSummary:
    namespace: str
    item_count: int
    tombstone_count: int
    last_write_at: str | None
    retired: bool
    retired_at: str | None


@dataclass(frozen=True)
class MemoryNamespaceDetail(MemoryNamespaceSummary):
    retired_reason: str | None


@dataclass(frozen=True)
class MemoryRetentionRule:
    namespace: str
    max_items: Optional[int]
    ttl_seconds: Optional[int]
    policy_source: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryRetentionDetail(MemoryRetentionRule):
    item_count: int
    tombstone_count: int
    last_write_at: str | None


@dataclass(frozen=True)
class MemoryRetentionEviction:
    item: MemoryItem
    reason: str


@dataclass(frozen=True)
class MemoryRetentionPlan:
    rule: MemoryRetentionRule
    evictions: list[MemoryRetentionEviction]
    before_count: int
    after_count: int
    incoming_new: bool
    evaluated_at: str
    ttl_cutoff: str | None
    shortfall: int


@dataclass(frozen=True)
class MemoryProfile:
    profile_id: str
    name: str
    description: str | None
    include_namespaces: list[str]
    exclude_namespaces: list[str]
    include_kinds: list[str]
    exclude_kinds: list[str]
    max_items: Optional[int]
    created_at: str
    retired_at: str | None


class MemoryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._open_connections: set[sqlite3.Connection] = set()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        self._apply_pragmas(connection)
        self._open_connections.add(connection)
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
            self._close_connection(connection)

    def _close_connection(self, connection: sqlite3.Connection) -> None:
        try:
            connection.close()
        finally:
            self._open_connections.discard(connection)

    def close(self) -> None:
        for connection in list(self._open_connections):
            self._close_connection(connection)

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
                CREATE TABLE IF NOT EXISTS memory_namespaces (
                    namespace TEXT PRIMARY KEY,
                    retired_at TEXT NULL,
                    retired_reason TEXT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_retention_rules (
                    namespace TEXT PRIMARY KEY,
                    max_items INTEGER NULL,
                    ttl_seconds INTEGER NULL,
                    policy_source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NULL,
                    include_namespaces_json TEXT NULL,
                    exclude_namespaces_json TEXT NULL,
                    include_kinds_json TEXT NULL,
                    exclude_kinds_json TEXT NULL,
                    max_items INTEGER NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT NULL
                )
                """
            )
            for _, statement in MEMORY_INDEX_DEFINITIONS:
                cursor.execute(statement)
            connection.commit()

    def list_namespaces(self) -> list[MemoryNamespaceSummary]:
        sql = """
            WITH namespace_union AS (
                SELECT namespace FROM memory_items
                UNION
                SELECT namespace FROM memory_namespaces
            ),
            stats AS (
                SELECT
                    namespace,
                    SUM(CASE WHEN is_tombstoned = 0 THEN 1 ELSE 0 END) AS item_count,
                    SUM(CASE WHEN is_tombstoned = 1 THEN 1 ELSE 0 END) AS tombstone_count,
                    MAX(updated_at) AS last_write_at
                FROM memory_items
                GROUP BY namespace
            )
            SELECT
                namespace_union.namespace AS namespace,
                COALESCE(stats.item_count, 0) AS item_count,
                COALESCE(stats.tombstone_count, 0) AS tombstone_count,
                stats.last_write_at AS last_write_at,
                memory_namespaces.retired_at AS retired_at,
                memory_namespaces.retired_reason AS retired_reason
            FROM namespace_union
            LEFT JOIN stats ON stats.namespace = namespace_union.namespace
            LEFT JOIN memory_namespaces ON memory_namespaces.namespace = namespace_union.namespace
            ORDER BY namespace_union.namespace ASC
        """
        with self._connection() as connection:
            rows = connection.cursor().execute(sql).fetchall()
            return [_row_to_namespace_summary(row) for row in rows]

    def get_namespace(self, *, namespace: str) -> MemoryNamespaceDetail | None:
        sql = """
            WITH namespace_union AS (
                SELECT namespace FROM memory_items
                UNION
                SELECT namespace FROM memory_namespaces
            ),
            stats AS (
                SELECT
                    namespace,
                    SUM(CASE WHEN is_tombstoned = 0 THEN 1 ELSE 0 END) AS item_count,
                    SUM(CASE WHEN is_tombstoned = 1 THEN 1 ELSE 0 END) AS tombstone_count,
                    MAX(updated_at) AS last_write_at
                FROM memory_items
                GROUP BY namespace
            )
            SELECT
                namespace_union.namespace AS namespace,
                COALESCE(stats.item_count, 0) AS item_count,
                COALESCE(stats.tombstone_count, 0) AS tombstone_count,
                stats.last_write_at AS last_write_at,
                memory_namespaces.retired_at AS retired_at,
                memory_namespaces.retired_reason AS retired_reason
            FROM namespace_union
            LEFT JOIN stats ON stats.namespace = namespace_union.namespace
            LEFT JOIN memory_namespaces ON memory_namespaces.namespace = namespace_union.namespace
            WHERE namespace_union.namespace = ?
        """
        with self._connection() as connection:
            row = connection.cursor().execute(sql, (namespace,)).fetchone()
            if not row:
                return None
            return _row_to_namespace_detail(row)

    def list_retention_rules(self) -> list[MemoryRetentionDetail]:
        sql = """
            WITH stats AS (
                SELECT
                    namespace,
                    SUM(CASE WHEN is_tombstoned = 0 THEN 1 ELSE 0 END) AS item_count,
                    SUM(CASE WHEN is_tombstoned = 1 THEN 1 ELSE 0 END) AS tombstone_count,
                    MAX(updated_at) AS last_write_at
                FROM memory_items
                GROUP BY namespace
            )
            SELECT
                memory_retention_rules.namespace AS namespace,
                memory_retention_rules.max_items AS max_items,
                memory_retention_rules.ttl_seconds AS ttl_seconds,
                memory_retention_rules.policy_source AS policy_source,
                memory_retention_rules.created_at AS created_at,
                memory_retention_rules.updated_at AS updated_at,
                COALESCE(stats.item_count, 0) AS item_count,
                COALESCE(stats.tombstone_count, 0) AS tombstone_count,
                stats.last_write_at AS last_write_at
            FROM memory_retention_rules
            LEFT JOIN stats ON stats.namespace = memory_retention_rules.namespace
            ORDER BY memory_retention_rules.namespace ASC
        """
        with self._connection() as connection:
            rows = connection.cursor().execute(sql).fetchall()
            return [_row_to_retention_detail(row) for row in rows]

    def get_retention_rule(self, *, namespace: str) -> MemoryRetentionRule | None:
        sql = "SELECT * FROM memory_retention_rules WHERE namespace = ?"
        with self._connection() as connection:
            row = connection.cursor().execute(sql, (namespace,)).fetchone()
            if not row:
                return None
            return _row_to_retention_rule(row)

    def get_retention_detail(self, *, namespace: str) -> MemoryRetentionDetail | None:
        sql = """
            WITH stats AS (
                SELECT
                    namespace,
                    SUM(CASE WHEN is_tombstoned = 0 THEN 1 ELSE 0 END) AS item_count,
                    SUM(CASE WHEN is_tombstoned = 1 THEN 1 ELSE 0 END) AS tombstone_count,
                    MAX(updated_at) AS last_write_at
                FROM memory_items
                GROUP BY namespace
            )
            SELECT
                memory_retention_rules.namespace AS namespace,
                memory_retention_rules.max_items AS max_items,
                memory_retention_rules.ttl_seconds AS ttl_seconds,
                memory_retention_rules.policy_source AS policy_source,
                memory_retention_rules.created_at AS created_at,
                memory_retention_rules.updated_at AS updated_at,
                COALESCE(stats.item_count, 0) AS item_count,
                COALESCE(stats.tombstone_count, 0) AS tombstone_count,
                stats.last_write_at AS last_write_at
            FROM memory_retention_rules
            LEFT JOIN stats ON stats.namespace = memory_retention_rules.namespace
            WHERE memory_retention_rules.namespace = ?
        """
        with self._connection() as connection:
            row = connection.cursor().execute(sql, (namespace,)).fetchone()
            if not row:
                return None
            return _row_to_retention_detail(row)

    def list_profiles(self) -> list[MemoryProfile]:
        sql = "SELECT * FROM memory_profiles ORDER BY name ASC"
        with self._connection() as connection:
            rows = connection.cursor().execute(sql).fetchall()
            return [_row_to_profile(row) for row in rows]

    def get_profile(
        self,
        *,
        profile_id: str | None = None,
        name: str | None = None,
    ) -> MemoryProfile | None:
        if bool(profile_id) == bool(name):
            raise ValueError("Provide exactly one of profile_id or name")
        sql = "SELECT * FROM memory_profiles WHERE profile_id = ?" if profile_id else (
            "SELECT * FROM memory_profiles WHERE name = ?"
        )
        value = profile_id or name
        with self._connection() as connection:
            row = connection.cursor().execute(sql, (value,)).fetchone()
            if not row:
                return None
            return _row_to_profile(row)

    def get_profile_by_selector(self, selector: str) -> MemoryProfile | None:
        sql = "SELECT * FROM memory_profiles WHERE profile_id = ?"
        with self._connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (selector,)).fetchone()
            if not row:
                row = cursor.execute(
                    "SELECT * FROM memory_profiles WHERE name = ?",
                    (selector,),
                ).fetchone()
            if not row:
                return None
            return _row_to_profile(row)

    def create_profile(
        self,
        *,
        name: str,
        description: str | None,
        include_namespaces: list[str] | None,
        exclude_namespaces: list[str] | None,
        include_kinds: list[str] | None,
        exclude_kinds: list[str] | None,
        max_items: Optional[int],
        created_at: Optional[str] = None,
    ) -> MemoryProfile:
        created_at = created_at or _utc_now().isoformat()
        profile_id = str(uuid4())
        include_namespaces_json = _serialize_profile_list(include_namespaces)
        exclude_namespaces_json = _serialize_profile_list(exclude_namespaces)
        include_kinds_json = _serialize_profile_list(include_kinds)
        exclude_kinds_json = _serialize_profile_list(exclude_kinds)
        with self._connection() as connection:
            cursor = connection.cursor()
            existing = cursor.execute(
                "SELECT profile_id FROM memory_profiles WHERE name = ?",
                (name,),
            ).fetchone()
            if existing:
                raise ValueError(f"Memory profile already exists: {name}")
            cursor.execute(
                """
                INSERT INTO memory_profiles (
                    profile_id,
                    name,
                    description,
                    include_namespaces_json,
                    exclude_namespaces_json,
                    include_kinds_json,
                    exclude_kinds_json,
                    max_items,
                    created_at,
                    retired_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    profile_id,
                    name,
                    description,
                    include_namespaces_json,
                    exclude_namespaces_json,
                    include_kinds_json,
                    exclude_kinds_json,
                    max_items,
                    created_at,
                ),
            )
            connection.commit()
        profile = self.get_profile(profile_id=profile_id)
        if profile is None:
            raise RuntimeError("Failed to load memory profile after create")
        return profile

    def retire_profile(
        self,
        *,
        profile_id: str,
        retired_at: Optional[str] = None,
    ) -> tuple[MemoryProfile, bool]:
        retired_at = retired_at or _utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.cursor()
            existing = cursor.execute(
                "SELECT retired_at FROM memory_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if not existing:
                raise ValueError(f"Memory profile not found: {profile_id}")
            if existing["retired_at"]:
                profile = self.get_profile(profile_id=profile_id)
                if profile is None:
                    raise RuntimeError("Failed to load memory profile after retire")
                return profile, False
            cursor.execute(
                "UPDATE memory_profiles SET retired_at = ? WHERE profile_id = ?",
                (retired_at, profile_id),
            )
            connection.commit()
        profile = self.get_profile(profile_id=profile_id)
        if profile is None:
            raise RuntimeError("Failed to load memory profile after retire")
        return profile, True

    def list_retired_namespaces(self) -> list[str]:
        sql = """
            SELECT namespace
            FROM memory_namespaces
            WHERE retired_at IS NOT NULL
            ORDER BY namespace ASC
        """
        with self._connection() as connection:
            rows = connection.cursor().execute(sql).fetchall()
            return [row["namespace"] for row in rows]

    def list_profile_items(
        self,
        *,
        profile: MemoryProfile,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        if _profile_is_empty(profile):
            return []
        filters: list[str] = ["is_tombstoned = 0"]
        params: list[Any] = []
        if profile.include_namespaces:
            placeholders = ",".join("?" for _ in profile.include_namespaces)
            filters.append(f"namespace IN ({placeholders})")
            params.extend(profile.include_namespaces)
        if profile.exclude_namespaces:
            placeholders = ",".join("?" for _ in profile.exclude_namespaces)
            filters.append(f"namespace NOT IN ({placeholders})")
            params.extend(profile.exclude_namespaces)
        if profile.include_kinds:
            placeholders = ",".join("?" for _ in profile.include_kinds)
            filters.append(f"kind IN ({placeholders})")
            params.extend(profile.include_kinds)
        if profile.exclude_kinds:
            placeholders = ",".join("?" for _ in profile.exclude_kinds)
            filters.append(f"kind NOT IN ({placeholders})")
            params.extend(profile.exclude_kinds)
        where_clause = "WHERE " + " AND ".join(filters)
        sql = (
            "SELECT * FROM memory_items "
            f"{where_clause} "
            "ORDER BY updated_at DESC, namespace ASC, key ASC, id ASC"
        )
        effective_limit = _profile_effective_limit(profile.max_items, limit)
        if effective_limit is not None:
            sql = f"{sql} LIMIT ?"
            params.append(effective_limit)
        with self._connection() as connection:
            rows = connection.cursor().execute(sql, params).fetchall()
            return [_row_to_item(row) for row in rows]

    def set_retention_rule(
        self,
        *,
        namespace: str,
        max_items: Optional[int],
        ttl_seconds: Optional[int],
        policy_source: str,
        updated_at: Optional[str] = None,
    ) -> tuple[MemoryRetentionRule, bool]:
        updated_at = updated_at or _utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.cursor()
            existing = cursor.execute(
                """
                SELECT namespace, max_items, ttl_seconds, policy_source, created_at
                FROM memory_retention_rules
                WHERE namespace = ?
                """,
                (namespace,),
            ).fetchone()
            if existing:
                created_at = existing["created_at"]
                changed = (
                    existing["max_items"] != max_items
                    or existing["ttl_seconds"] != ttl_seconds
                    or existing["policy_source"] != policy_source
                )
            else:
                created_at = updated_at
                changed = True
            cursor.execute(
                """
                INSERT INTO memory_retention_rules (
                    namespace,
                    max_items,
                    ttl_seconds,
                    policy_source,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace)
                DO UPDATE SET
                    max_items = excluded.max_items,
                    ttl_seconds = excluded.ttl_seconds,
                    policy_source = excluded.policy_source,
                    updated_at = excluded.updated_at
                """,
                (
                    namespace,
                    max_items,
                    ttl_seconds,
                    policy_source,
                    created_at,
                    updated_at,
                ),
            )
            connection.commit()
        rule = self.get_retention_rule(namespace=namespace)
        if rule is None:
            raise RuntimeError("Failed to load retention rule after set")
        return rule, changed

    def clear_retention_rule(self, *, namespace: str) -> bool:
        with self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "DELETE FROM memory_retention_rules WHERE namespace = ?",
                (namespace,),
            )
            changed = cursor.rowcount > 0
            connection.commit()
        return changed

    def plan_retention_for_write(
        self,
        *,
        namespace: str,
        key: str,
        now: Optional[datetime] = None,
    ) -> MemoryRetentionPlan | None:
        rule = self.get_retention_rule(namespace=namespace)
        if rule is None or (rule.max_items is None and rule.ttl_seconds is None):
            return None
        now = now or _utc_now()
        evaluated_at = now.isoformat()
        with self._connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(
                """
                SELECT * FROM memory_items
                WHERE namespace = ? AND is_tombstoned = 0
                ORDER BY created_at ASC, key ASC, id ASC
                """,
                (namespace,),
            ).fetchall()
        items = [_row_to_item(row) for row in rows]
        before_count = len(items)
        incoming_new = all(item.key != key for item in items)
        delta = 1 if incoming_new else 0

        ttl_cutoff = None
        evictions: list[MemoryRetentionEviction] = []
        evicted_ids: set[str] = set()
        if rule.ttl_seconds is not None:
            cutoff_dt = now - timedelta(seconds=rule.ttl_seconds)
            ttl_cutoff = cutoff_dt.isoformat()
            for item in items:
                created_dt = _parse_iso_timestamp(item.created_at)
                if created_dt <= cutoff_dt:
                    evictions.append(MemoryRetentionEviction(item=item, reason="ttl"))
                    evicted_ids.add(item.id)

        remaining = [item for item in items if item.id not in evicted_ids]
        shortfall = 0
        if rule.max_items is not None:
            max_items = rule.max_items
            desired_count = max(0, before_count + delta - len(evictions))
            if desired_count > max_items:
                evictions_needed = desired_count - max_items
                for item in remaining[:evictions_needed]:
                    evictions.append(
                        MemoryRetentionEviction(item=item, reason="max_items")
                    )
                    evicted_ids.add(item.id)
                if evictions_needed > len(remaining):
                    shortfall = evictions_needed - len(remaining)

        evictions_sorted = sorted(
            evictions,
            key=lambda entry: (entry.item.created_at, entry.item.key, entry.item.id, entry.reason),
        )
        after_count = max(0, before_count + delta - len(evictions_sorted))
        return MemoryRetentionPlan(
            rule=rule,
            evictions=evictions_sorted,
            before_count=before_count,
            after_count=after_count,
            incoming_new=incoming_new,
            evaluated_at=evaluated_at,
            ttl_cutoff=ttl_cutoff,
            shortfall=shortfall,
        )

    def record_retention_decision(
        self,
        *,
        plan: MemoryRetentionPlan,
        namespace: str,
        key: str,
        actor: str,
        policy_hash: str,
        policy_meta: Optional[dict[str, Any]] = None,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> str:
        request = {
            "namespace": namespace,
            "key": key,
            "rule": _serialize_retention_rule(plan.rule),
            "evaluated_at": plan.evaluated_at,
            "incoming_new": plan.incoming_new,
        }
        result_meta = {
            "counts": {"before": plan.before_count, "after": plan.after_count},
            "eviction_count": len(plan.evictions),
            "evictions": [_serialize_retention_eviction(entry) for entry in plan.evictions],
            "ttl_cutoff": plan.ttl_cutoff,
            "max_items": plan.rule.max_items,
            "shortfall": plan.shortfall,
        }
        if policy_meta:
            result_meta.update(policy_meta)
        with self._connection() as connection:
            event_id = append_event(
                connection,
                operation="retention.decision",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
        return event_id

    def apply_retention_evictions(
        self,
        *,
        plan: MemoryRetentionPlan,
        actor: str,
        policy_hash: str,
        retention_event_id: str,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> list[MemoryItem]:
        evicted: list[MemoryItem] = []
        for entry in plan.evictions:
            item = self.tombstone_item(
                entry.item.namespace,
                entry.item.key,
                actor=actor,
                policy_hash=policy_hash,
                result_meta_extra={
                    "retention_event_id": retention_event_id,
                    "retention_reason": entry.reason,
                },
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            if item is not None:
                evicted.append(item)
        return evicted

    def retire_namespace(
        self,
        *,
        namespace: str,
        reason: str,
        retired_at: Optional[str] = None,
    ) -> tuple[MemoryNamespaceDetail, bool]:
        retired_at = retired_at or _utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.cursor()
            existing = cursor.execute(
                """
                SELECT retired_at, retired_reason
                FROM memory_namespaces
                WHERE namespace = ?
                """,
                (namespace,),
            ).fetchone()
            if existing and existing["retired_at"]:
                detail = self.get_namespace(namespace=namespace)
                if detail is None:
                    raise RuntimeError("Failed to load namespace metadata after retire")
                return detail, False
            cursor.execute(
                """
                INSERT INTO memory_namespaces (namespace, retired_at, retired_reason)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace)
                DO UPDATE SET
                    retired_at = excluded.retired_at,
                    retired_reason = excluded.retired_reason
                """,
                (namespace, retired_at, reason),
            )
            connection.commit()
        detail = self.get_namespace(namespace=namespace)
        if detail is None:
            raise RuntimeError("Failed to load namespace metadata after retire")
        return detail, True

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
        result_meta_extra: Optional[dict[str, Any]] = None,
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
            if result_meta_extra:
                result_meta.update(result_meta_extra)
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

    def list_prompt_items(self, *, limit: int = 20) -> list[MemoryItem]:
        kinds = ["preference", "constraint", "procedure", "fact"]
        confidences = ["high", "medium"]
        kind_placeholders = ",".join("?" for _ in kinds)
        confidence_placeholders = ",".join("?" for _ in confidences)
        sql = (
            "SELECT * FROM memory_items "
            "WHERE is_tombstoned = 0 "
            "AND (namespace = ? OR namespace LIKE ?) "
            f"AND kind IN ({kind_placeholders}) "
            f"AND confidence IN ({confidence_placeholders}) "
            "ORDER BY updated_at DESC, key ASC "
            "LIMIT ?"
        )
        params: list[Any] = [
            "global",
            "project:%",
            *kinds,
            *confidences,
            limit,
        ]
        with self._connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, params).fetchall()
            return [_row_to_item(row) for row in rows]

    def tombstone_item(
        self,
        namespace: str,
        key: str,
        *,
        actor: str,
        policy_hash: str,
        result_meta_extra: Optional[dict[str, Any]] = None,
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
            if result_meta_extra:
                result_meta.update(result_meta_extra)
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

    def fetch_item_raw(self, *, namespace: str, key: str) -> MemoryItem | None:
        with self._connection() as connection:
            return self._fetch_item(
                connection,
                namespace=namespace,
                key=key,
                include_tombstoned=True,
            )

    def list_items_for_snapshot(
        self,
        *,
        namespace: Optional[str],
        namespace_prefix: Optional[str],
    ) -> list[MemoryItem]:
        filters: list[str] = []
        params: list[Any] = []
        if namespace:
            filters.append("namespace = ?")
            params.append(namespace)
        elif namespace_prefix is not None:
            filters.append("namespace LIKE ?")
            params.append(f"{namespace_prefix}%")
        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)
        sql = (
            "SELECT * FROM memory_items "
            f"{where_clause} "
            "ORDER BY namespace ASC, key ASC"
        )
        with self._connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, params).fetchall()
            return [_row_to_item(row) for row in rows]

    def upsert_item_with_timestamps(
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
        is_tombstoned: bool,
        created_at: str,
        updated_at: str,
        update_created_at: bool,
        actor: str,
        policy_hash: str,
        operation: str,
        result_meta_extra: Optional[dict[str, Any]] = None,
        related_run_id: Optional[str] = None,
        related_ask_event_id: Optional[str] = None,
    ) -> MemoryItem:
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        tags_json = json.dumps(tags, ensure_ascii=False, sort_keys=True) if tags else None
        new_id = str(uuid4())
        update_created_clause = ", created_at = excluded.created_at" if update_created_at else ""
        with self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"""
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key)
                DO UPDATE SET
                    kind = excluded.kind,
                    value_json = excluded.value_json,
                    tags_json = excluded.tags_json,
                    confidence = excluded.confidence,
                    source = excluded.source,
                    ttl_seconds = excluded.ttl_seconds,
                    is_tombstoned = excluded.is_tombstoned,
                    updated_at = excluded.updated_at
                    {update_created_clause}
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
                    int(is_tombstoned),
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
                raise RuntimeError("Failed to load memory item after upsert")
            request = {
                "namespace": namespace,
                "key": key,
                "kind": kind,
                "value_json": value_json,
                "tags_json": tags_json,
                "confidence": confidence,
                "source": source,
                "ttl_seconds": ttl_seconds,
                "created_at": created_at,
                "updated_at": updated_at,
                "is_tombstoned": is_tombstoned,
            }
            result_meta = {
                "item_id": item.id,
                "updated_at": item.updated_at,
                "is_tombstoned": item.is_tombstoned,
            }
            if result_meta_extra:
                result_meta.update(result_meta_extra)
            append_event(
                connection,
                operation=operation,
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
                related_run_id=related_run_id,
                related_ask_event_id=related_ask_event_id,
            )
            connection.commit()
            return item


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
    result_meta_extra: Optional[dict[str, Any]] = None,
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
        result_meta_extra=result_meta_extra,
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


def list_items_for_snapshot(
    db_path: str,
    *,
    namespace: Optional[str],
    namespace_prefix: Optional[str],
) -> list[MemoryItem]:
    return MemoryStore(db_path).list_items_for_snapshot(
        namespace=namespace,
        namespace_prefix=namespace_prefix,
    )


def fetch_item_raw(db_path: str, *, namespace: str, key: str) -> MemoryItem | None:
    return MemoryStore(db_path).fetch_item_raw(namespace=namespace, key=key)


def upsert_item_with_timestamps(
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
    is_tombstoned: bool,
    created_at: str,
    updated_at: str,
    update_created_at: bool,
    actor: str,
    policy_hash: str,
    operation: str,
    result_meta_extra: Optional[dict[str, Any]] = None,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> MemoryItem:
    return MemoryStore(db_path).upsert_item_with_timestamps(
        namespace=namespace,
        key=key,
        kind=kind,
        value=value,
        tags=tags,
        confidence=confidence,
        source=source,
        ttl_seconds=ttl_seconds,
        is_tombstoned=is_tombstoned,
        created_at=created_at,
        updated_at=updated_at,
        update_created_at=update_created_at,
        actor=actor,
        policy_hash=policy_hash,
        operation=operation,
        result_meta_extra=result_meta_extra,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def list_prompt_items(
    db_path: str,
    *,
    limit: int = 20,
) -> list[MemoryItem]:
    return MemoryStore(db_path).list_prompt_items(limit=limit)


def list_profiles(db_path: str) -> list[MemoryProfile]:
    return MemoryStore(db_path).list_profiles()


def get_profile(
    db_path: str,
    *,
    profile_id: str | None = None,
    name: str | None = None,
) -> MemoryProfile | None:
    return MemoryStore(db_path).get_profile(profile_id=profile_id, name=name)


def get_profile_by_selector(db_path: str, selector: str) -> MemoryProfile | None:
    return MemoryStore(db_path).get_profile_by_selector(selector)


def create_profile(
    db_path: str,
    *,
    name: str,
    description: str | None,
    include_namespaces: list[str] | None,
    exclude_namespaces: list[str] | None,
    include_kinds: list[str] | None,
    exclude_kinds: list[str] | None,
    max_items: Optional[int],
    created_at: Optional[str] = None,
) -> MemoryProfile:
    return MemoryStore(db_path).create_profile(
        name=name,
        description=description,
        include_namespaces=include_namespaces,
        exclude_namespaces=exclude_namespaces,
        include_kinds=include_kinds,
        exclude_kinds=exclude_kinds,
        max_items=max_items,
        created_at=created_at,
    )


def retire_profile(
    db_path: str,
    *,
    profile_id: str,
    retired_at: Optional[str] = None,
) -> tuple[MemoryProfile, bool]:
    return MemoryStore(db_path).retire_profile(profile_id=profile_id, retired_at=retired_at)


def list_retired_namespaces(db_path: str) -> list[str]:
    return MemoryStore(db_path).list_retired_namespaces()


def list_profile_items(
    db_path: str,
    *,
    profile: MemoryProfile,
    limit: int | None = None,
) -> list[MemoryItem]:
    return MemoryStore(db_path).list_profile_items(profile=profile, limit=limit)


def list_namespaces(db_path: str) -> list[MemoryNamespaceSummary]:
    return MemoryStore(db_path).list_namespaces()


def get_namespace(db_path: str, *, namespace: str) -> MemoryNamespaceDetail | None:
    return MemoryStore(db_path).get_namespace(namespace=namespace)


def list_retention_rules(db_path: str) -> list[MemoryRetentionDetail]:
    return MemoryStore(db_path).list_retention_rules()


def get_retention_rule(db_path: str, *, namespace: str) -> MemoryRetentionRule | None:
    return MemoryStore(db_path).get_retention_rule(namespace=namespace)


def get_retention_detail(db_path: str, *, namespace: str) -> MemoryRetentionDetail | None:
    return MemoryStore(db_path).get_retention_detail(namespace=namespace)


def set_retention_rule(
    db_path: str,
    *,
    namespace: str,
    max_items: Optional[int],
    ttl_seconds: Optional[int],
    policy_source: str,
    updated_at: Optional[str] = None,
) -> tuple[MemoryRetentionRule, bool]:
    return MemoryStore(db_path).set_retention_rule(
        namespace=namespace,
        max_items=max_items,
        ttl_seconds=ttl_seconds,
        policy_source=policy_source,
        updated_at=updated_at,
    )


def clear_retention_rule(db_path: str, *, namespace: str) -> bool:
    return MemoryStore(db_path).clear_retention_rule(namespace=namespace)


def plan_retention_for_write(
    db_path: str,
    *,
    namespace: str,
    key: str,
    now: Optional[datetime] = None,
) -> MemoryRetentionPlan | None:
    return MemoryStore(db_path).plan_retention_for_write(
        namespace=namespace,
        key=key,
        now=now,
    )


def record_retention_decision(
    db_path: str,
    *,
    plan: MemoryRetentionPlan,
    namespace: str,
    key: str,
    actor: str,
    policy_hash: str,
    policy_meta: Optional[dict[str, Any]] = None,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> str:
    return MemoryStore(db_path).record_retention_decision(
        plan=plan,
        namespace=namespace,
        key=key,
        actor=actor,
        policy_hash=policy_hash,
        policy_meta=policy_meta,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def apply_retention_evictions(
    db_path: str,
    *,
    plan: MemoryRetentionPlan,
    actor: str,
    policy_hash: str,
    retention_event_id: str,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> list[MemoryItem]:
    return MemoryStore(db_path).apply_retention_evictions(
        plan=plan,
        actor=actor,
        policy_hash=policy_hash,
        retention_event_id=retention_event_id,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def retire_namespace(
    db_path: str,
    *,
    namespace: str,
    reason: str,
    retired_at: Optional[str] = None,
) -> tuple[MemoryNamespaceDetail, bool]:
    return MemoryStore(db_path).retire_namespace(
        namespace=namespace,
        reason=reason,
        retired_at=retired_at,
    )


def tombstone_item(
    db_path: str,
    namespace: str,
    key: str,
    *,
    actor: str,
    policy_hash: str,
    result_meta_extra: Optional[dict[str, Any]] = None,
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> MemoryItem | None:
    return MemoryStore(db_path).tombstone_item(
        namespace,
        key,
        actor=actor,
        policy_hash=policy_hash,
        result_meta_extra=result_meta_extra,
        related_run_id=related_run_id,
        related_ask_event_id=related_ask_event_id,
    )


def record_event(
    db_path: str,
    *,
    event_id: Optional[str] = None,
    operation: str,
    actor: str,
    policy_hash: str,
    request: dict[str, Any],
    result_meta: dict[str, Any],
    related_run_id: Optional[str] = None,
    related_ask_event_id: Optional[str] = None,
) -> str:
    store = MemoryStore(db_path)
    with store._connection() as connection:
        event_id = append_event(
            connection,
            event_id=event_id,
            operation=operation,
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
            related_run_id=related_run_id,
            related_ask_event_id=related_ask_event_id,
        )
        connection.commit()
        return event_id


def append_event(
    connection: sqlite3.Connection,
    *,
    event_id: Optional[str] = None,
    operation: str,
    actor: str,
    policy_hash: str,
    request: dict[str, Any],
    result_meta: dict[str, Any],
    related_run_id: Optional[str],
    related_ask_event_id: Optional[str],
) -> str:
    timestamp = _utc_now().isoformat()
    event_id = event_id or str(uuid4())
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
    return event_id


def policy_hash_for_path(policy_path: str | None) -> str:
    if policy_path:
        contents = Path(policy_path).read_bytes()
    else:
        contents = b"default"
    return hashlib.sha256(contents).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def _row_to_namespace_summary(row: sqlite3.Row) -> MemoryNamespaceSummary:
    retired_at = row["retired_at"]
    return MemoryNamespaceSummary(
        namespace=row["namespace"],
        item_count=int(row["item_count"]),
        tombstone_count=int(row["tombstone_count"]),
        last_write_at=row["last_write_at"],
        retired=bool(retired_at),
        retired_at=retired_at,
    )


def _row_to_namespace_detail(row: sqlite3.Row) -> MemoryNamespaceDetail:
    summary = _row_to_namespace_summary(row)
    return MemoryNamespaceDetail(
        namespace=summary.namespace,
        item_count=summary.item_count,
        tombstone_count=summary.tombstone_count,
        last_write_at=summary.last_write_at,
        retired=summary.retired,
        retired_at=summary.retired_at,
        retired_reason=row["retired_reason"],
    )


def _row_to_retention_rule(row: sqlite3.Row) -> MemoryRetentionRule:
    return MemoryRetentionRule(
        namespace=row["namespace"],
        max_items=row["max_items"],
        ttl_seconds=row["ttl_seconds"],
        policy_source=row["policy_source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_retention_detail(row: sqlite3.Row) -> MemoryRetentionDetail:
    return MemoryRetentionDetail(
        namespace=row["namespace"],
        max_items=row["max_items"],
        ttl_seconds=row["ttl_seconds"],
        policy_source=row["policy_source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        item_count=int(row["item_count"]),
        tombstone_count=int(row["tombstone_count"]),
        last_write_at=row["last_write_at"],
    )


def _row_to_profile(row: sqlite3.Row) -> MemoryProfile:
    return MemoryProfile(
        profile_id=row["profile_id"],
        name=row["name"],
        description=row["description"],
        include_namespaces=_deserialize_profile_list(row["include_namespaces_json"]),
        exclude_namespaces=_deserialize_profile_list(row["exclude_namespaces_json"]),
        include_kinds=_deserialize_profile_list(row["include_kinds_json"]),
        exclude_kinds=_deserialize_profile_list(row["exclude_kinds_json"]),
        max_items=row["max_items"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
    )


def _serialize_profile_list(values: list[str] | None) -> str | None:
    if not values:
        return None
    return json.dumps(sorted(set(values)), ensure_ascii=False, sort_keys=True)


def _deserialize_profile_list(value: str | None) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("Profile list must be a JSON list")
    return [str(item) for item in parsed if str(item)]


def _profile_is_empty(profile: MemoryProfile) -> bool:
    if profile.include_namespaces:
        return False
    if profile.exclude_namespaces:
        return False
    if profile.include_kinds:
        return False
    if profile.exclude_kinds:
        return False
    return profile.max_items is None


def _profile_effective_limit(max_items: int | None, limit: int | None) -> int | None:
    if max_items is not None and limit is not None:
        return min(max_items, limit)
    if max_items is not None:
        return max_items
    return limit


def _serialize_retention_rule(rule: MemoryRetentionRule) -> dict[str, object]:
    return {
        "namespace": rule.namespace,
        "max_items": rule.max_items,
        "ttl_seconds": rule.ttl_seconds,
        "policy_source": rule.policy_source,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _serialize_retention_eviction(entry: MemoryRetentionEviction) -> dict[str, object]:
    item = entry.item
    return {
        "namespace": item.namespace,
        "key": item.key,
        "item_id": item.id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "reason": entry.reason,
    }
