"""Deterministic memory injection selection and tracing."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Sequence

from gismo.memory.snapshot import memory_item_hash
from gismo.memory.store import (
    MemoryItem,
    MemoryProfile,
    PROMPT_ALLOWED_CONFIDENCES,
    PROMPT_ALLOWED_KINDS,
)

MEMORY_INJECTION_ITEM_CAP = 20
MEMORY_INJECTION_BYTE_CAP = 8192
MEMORY_INJECTION_TRACE_AUDIT_LIMIT = 10
MEMORY_INJECTION_TRACE_SCHEMA_VERSION = 1
MEMORY_INJECTION_TRACE_ORDERING = "updated_at_desc,namespace,key,id"


@dataclass(frozen=True)
class NamespaceFilter:
    exact: tuple[str, ...]
    prefixes: tuple[str, ...]
    allow_all: bool = False

    @classmethod
    def from_patterns(cls, patterns: Iterable[str]) -> "NamespaceFilter":
        exact: list[str] = []
        prefixes: list[str] = []
        allow_all = False
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern == "*":
                allow_all = True
                continue
            if pattern.endswith("*"):
                prefixes.append(pattern[:-1])
            else:
                exact.append(pattern)
        return cls(exact=tuple(sorted(set(exact))), prefixes=tuple(sorted(set(prefixes))), allow_all=allow_all)

    def matches(self, namespace: str) -> bool:
        if self.allow_all:
            return True
        if namespace in self.exact:
            return True
        return any(namespace.startswith(prefix) for prefix in self.prefixes)

    def sql_clause(self) -> tuple[str | None, list[object]]:
        if self.allow_all:
            return None, []
        parts: list[str] = []
        params: list[object] = []
        if self.exact:
            placeholders = ",".join("?" for _ in self.exact)
            parts.append(f"namespace IN ({placeholders})")
            params.extend(self.exact)
        for prefix in self.prefixes:
            parts.append("namespace LIKE ?")
            params.append(f"{prefix}%")
        if not parts:
            return None, []
        return f"({' OR '.join(parts)})", params


@dataclass(frozen=True)
class MemorySelectionFilters:
    include_groups: tuple[NamespaceFilter, ...]
    exclude_namespaces: tuple[str, ...]
    include_kinds: tuple[str, ...]
    exclude_kinds: tuple[str, ...]
    include_confidences: tuple[str, ...]

    def namespace_only(self) -> "MemorySelectionFilters":
        return MemorySelectionFilters(
            include_groups=self.include_groups,
            exclude_namespaces=self.exclude_namespaces,
            include_kinds=(),
            exclude_kinds=(),
            include_confidences=(),
        )

    def matches_namespace(self, namespace: str) -> bool:
        for group in self.include_groups:
            if not group.matches(namespace):
                return False
        if self.exclude_namespaces and namespace in self.exclude_namespaces:
            return False
        return True


@dataclass(frozen=True)
class MemoryInjectionSelection:
    items: list[MemoryItem]
    entries: list[dict[str, str]]
    total_bytes: int
    excluded_items: list[MemoryItem]


@dataclass(frozen=True)
class MemoryInjectionTraceItem:
    namespace: str
    key: str
    kind: str
    confidence: str
    updated_at: str
    item_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "key": self.key,
            "kind": self.kind,
            "confidence": self.confidence,
            "updated_at": self.updated_at,
            "item_hash": self.item_hash,
        }


@dataclass(frozen=True)
class MemoryInjectionTraceCounts:
    total_items: int
    filtered_items: int
    selected_items: int
    dropped_items: int
    cap_items: int
    cap_bytes: int
    denied_items: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "total_items": self.total_items,
            "filtered_items": self.filtered_items,
            "selected_items": self.selected_items,
            "dropped_items": self.dropped_items,
            "cap_items": self.cap_items,
            "cap_bytes": self.cap_bytes,
        }
        if self.denied_items is not None:
            payload["denied_items"] = self.denied_items
        return payload


@dataclass(frozen=True)
class MemoryInjectionTrace:
    enabled: bool
    source: str
    ordering: str
    profile: dict[str, object] | None
    counts: MemoryInjectionTraceCounts
    selected_items: list[MemoryInjectionTraceItem]
    injection_hash: str
    denied_namespaces: list[dict[str, object]] | None = None

    def to_dict(self, *, max_selected_items: int | None = None) -> dict[str, object]:
        selected = self.selected_items
        truncated = False
        remaining = 0
        if max_selected_items is not None and len(selected) > max_selected_items:
            truncated = True
            remaining = len(selected) - max_selected_items
            selected = selected[:max_selected_items]
        payload: dict[str, object] = {
            "schema_version": MEMORY_INJECTION_TRACE_SCHEMA_VERSION,
            "enabled": self.enabled,
            "source": self.source,
            "ordering": self.ordering,
            "profile": self.profile,
            "eligibility": self.counts.to_dict(),
            "selected": [item.to_dict() for item in selected],
            "selected_truncated": truncated,
            "selected_remaining": remaining,
            "injection_hash": self.injection_hash,
        }
        if self.denied_namespaces:
            payload["denied_namespaces"] = list(self.denied_namespaces)
        return payload


def prompt_selection_filters(*, namespace_filters: Iterable[str] | None = None) -> MemorySelectionFilters:
    include_groups = [NamespaceFilter.from_patterns(["global", "project:*"])]
    if namespace_filters:
        include_groups.append(NamespaceFilter.from_patterns(namespace_filters))
    return MemorySelectionFilters(
        include_groups=tuple(include_groups),
        exclude_namespaces=(),
        include_kinds=tuple(sorted(PROMPT_ALLOWED_KINDS)),
        exclude_kinds=(),
        include_confidences=tuple(sorted(PROMPT_ALLOWED_CONFIDENCES)),
    )


def profile_selection_filters(
    profile: MemoryProfile,
    *,
    namespace_filters: Iterable[str] | None = None,
) -> MemorySelectionFilters:
    include_groups: list[NamespaceFilter] = []
    if profile.include_namespaces:
        include_groups.append(NamespaceFilter.from_patterns(profile.include_namespaces))
    if namespace_filters:
        include_groups.append(NamespaceFilter.from_patterns(namespace_filters))
    return MemorySelectionFilters(
        include_groups=tuple(include_groups),
        exclude_namespaces=tuple(sorted(set(profile.exclude_namespaces))),
        include_kinds=tuple(sorted(set(profile.include_kinds))),
        exclude_kinds=tuple(sorted(set(profile.exclude_kinds))),
        include_confidences=(),
    )


def profile_filters_payload(profile: MemoryProfile, effective_limit: int | None) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "include_namespaces": profile.include_namespaces,
        "exclude_namespaces": profile.exclude_namespaces,
        "include_kinds": profile.include_kinds,
        "exclude_kinds": profile.exclude_kinds,
        "max_items": profile.max_items,
        "effective_limit": effective_limit,
    }


def order_memory_items(items: Sequence[MemoryItem]) -> list[MemoryItem]:
    return sorted(items, key=_memory_sort_key)


def select_injection_items(
    items: Sequence[MemoryItem],
    *,
    cap_items: int,
    cap_bytes: int,
) -> MemoryInjectionSelection:
    ordered_items = order_memory_items(items)
    entries = memory_entries_for_prompt(ordered_items)
    capped_entries: list[dict[str, str]] = []
    selected_items: list[MemoryItem] = []
    excluded_items: list[MemoryItem] = []
    total_bytes = 0
    for index, entry in enumerate(entries):
        serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entry_bytes = len(serialized.encode("utf-8"))
        if len(capped_entries) >= cap_items or total_bytes + entry_bytes > cap_bytes:
            excluded_items.extend(ordered_items[index:])
            break
        capped_entries.append(entry)
        selected_items.append(ordered_items[index])
        total_bytes += entry_bytes
    return MemoryInjectionSelection(
        items=selected_items,
        entries=capped_entries,
        total_bytes=total_bytes,
        excluded_items=excluded_items,
    )


def memory_entries_for_prompt(items: Sequence[MemoryItem]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in items:
        entries.append(
            {
                "namespace": item.namespace,
                "key": item.key,
                "kind": item.kind,
                "confidence": item.confidence,
                "source": item.source,
                "updated_at": item.updated_at,
                "value_json": serialize_memory_value(item.value),
            }
        )
    return entries


def serialize_memory_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def injection_hash_for_items(items: Sequence[MemoryItem]) -> str:
    ordered = order_memory_items(items)
    item_hashes = [memory_item_hash(item) for item in ordered]
    return _injection_hash(item_hashes)


def build_memory_injection_trace(
    db_path: str,
    *,
    selected_items: Sequence[MemoryItem],
    source: str,
    filters: MemorySelectionFilters,
    cap_items: int,
    cap_bytes: int,
    profile: dict[str, object] | None = None,
    policy_checker: Callable[[str], bool] | None = None,
) -> MemoryInjectionTrace:
    ordered_selected = order_memory_items(selected_items)
    trace_items = [
        MemoryInjectionTraceItem(
            namespace=item.namespace,
            key=item.key,
            kind=item.kind,
            confidence=item.confidence,
            updated_at=item.updated_at,
            item_hash=memory_item_hash(item),
        )
        for item in ordered_selected
    ]
    injection_hash = _injection_hash([item.item_hash for item in trace_items])
    total_by_namespace = _count_items_by_namespace(db_path, filters.namespace_only())
    filtered_by_namespace = _count_items_by_namespace(db_path, filters)
    total_count = sum(total_by_namespace.values())
    filtered_count = sum(filtered_by_namespace.values())
    denied_namespaces: list[dict[str, object]] = []
    denied_items = 0
    if policy_checker is not None:
        for namespace, count in filtered_by_namespace.items():
            if not policy_checker(namespace):
                denied_namespaces.append({"namespace": namespace, "count": count})
                denied_items += count
        denied_namespaces.sort(key=lambda entry: entry["namespace"])
    allowed_filtered = filtered_count - denied_items
    dropped_count = max(0, allowed_filtered - len(ordered_selected))
    counts = MemoryInjectionTraceCounts(
        total_items=total_count,
        filtered_items=filtered_count,
        selected_items=len(ordered_selected),
        dropped_items=dropped_count,
        cap_items=cap_items,
        cap_bytes=cap_bytes,
        denied_items=denied_items if denied_namespaces else None,
    )
    return MemoryInjectionTrace(
        enabled=True,
        source=source,
        ordering=MEMORY_INJECTION_TRACE_ORDERING,
        profile=profile,
        counts=counts,
        selected_items=trace_items,
        injection_hash=injection_hash,
        denied_namespaces=denied_namespaces or None,
    )


def _memory_sort_key(item: MemoryItem) -> tuple[float, str, str, str]:
    parsed = datetime.fromisoformat(item.updated_at)
    return (-parsed.timestamp(), item.namespace, item.key, item.id)


def _count_items_by_namespace(
    db_path: str,
    filters: MemorySelectionFilters,
) -> dict[str, int]:
    clauses: list[str] = ["is_tombstoned = 0"]
    params: list[object] = []
    for group in filters.include_groups:
        clause, group_params = group.sql_clause()
        if clause:
            clauses.append(clause)
            params.extend(group_params)
    if filters.exclude_namespaces:
        placeholders = ",".join("?" for _ in filters.exclude_namespaces)
        clauses.append(f"namespace NOT IN ({placeholders})")
        params.extend(filters.exclude_namespaces)
    if filters.include_kinds:
        placeholders = ",".join("?" for _ in filters.include_kinds)
        clauses.append(f"kind IN ({placeholders})")
        params.extend(filters.include_kinds)
    if filters.exclude_kinds:
        placeholders = ",".join("?" for _ in filters.exclude_kinds)
        clauses.append(f"kind NOT IN ({placeholders})")
        params.extend(filters.exclude_kinds)
    if filters.include_confidences:
        placeholders = ",".join("?" for _ in filters.include_confidences)
        clauses.append(f"confidence IN ({placeholders})")
        params.extend(filters.include_confidences)
    where_clause = " AND ".join(clauses)
    sql = (
        "SELECT namespace, COUNT(*) AS count "
        "FROM memory_items "
        f"WHERE {where_clause} "
        "GROUP BY namespace "
        "ORDER BY namespace ASC"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, params).fetchall()
    return {row["namespace"]: int(row["count"]) for row in rows}


def _injection_hash(item_hashes: Sequence[str]) -> str:
    joined = "".join(item_hashes)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
