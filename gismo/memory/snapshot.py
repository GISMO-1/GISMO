"""Deterministic snapshot export/import helpers for memory."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gismo.memory.store import MemoryItem, list_items_for_snapshot

SNAPSHOT_SCHEMA_VERSION = 1
_CANONICAL_KWARGS = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}


@dataclass(frozen=True)
class SnapshotItem:
    namespace: str
    key: str
    kind: str
    value: Any
    value_json: str
    confidence: str
    source: str
    tags: list[str]
    created_at: str
    updated_at: str
    is_tombstoned: bool
    item_hash: str


def export_snapshot(
    db_path: str,
    *,
    namespace_filter: str,
) -> dict[str, object]:
    namespace, namespace_prefix = _parse_namespace_filter(namespace_filter)
    items = list_items_for_snapshot(
        db_path,
        namespace=namespace,
        namespace_prefix=namespace_prefix,
    )
    snapshot_items = [_snapshot_item_from_memory(item) for item in items]
    item_hashes = [item["item_hash"] for item in snapshot_items]
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "created_at": _utc_now().isoformat(),
        "namespaces": sorted({item.namespace for item in items}),
        "items": snapshot_items,
        "snapshot_hash": _snapshot_hash(item_hashes),
    }
    return snapshot


def load_snapshot(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_snapshot(snapshot: dict[str, object]) -> tuple[list[SnapshotItem], str]:
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Unsupported schema_version in snapshot")
    items_raw = snapshot.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("Snapshot items must be a list")
    snapshot_hash = snapshot.get("snapshot_hash")
    if not isinstance(snapshot_hash, str):
        raise ValueError("Snapshot snapshot_hash must be a string")
    items: list[SnapshotItem] = []
    computed_hashes: list[str] = []
    for raw in items_raw:
        if not isinstance(raw, dict):
            raise ValueError("Snapshot item entries must be objects")
        item = _snapshot_item_from_payload(raw)
        items.append(item)
        computed_hashes.append(item.item_hash)
    computed_snapshot_hash = _snapshot_hash(computed_hashes)
    if computed_snapshot_hash != snapshot_hash:
        raise ValueError("Snapshot snapshot_hash mismatch")
    return items, snapshot_hash


def canonical_value_json(value: Any) -> str:
    return json.dumps(value, **_CANONICAL_KWARGS)


def canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, **_CANONICAL_KWARGS)


def memory_item_hash(item: MemoryItem) -> str:
    payload = _normalized_payload(
        namespace=item.namespace,
        key=item.key,
        kind=item.kind,
        value=item.value,
        confidence=item.confidence,
        source=item.source,
        tags=item.tags,
        created_at=item.created_at,
        updated_at=item.updated_at,
        is_tombstoned=item.is_tombstoned,
    )
    return _item_hash(payload)


def _parse_namespace_filter(namespace_filter: str) -> tuple[str | None, str | None]:
    if namespace_filter == "*":
        return None, ""
    if namespace_filter.endswith("*"):
        return None, namespace_filter[:-1]
    return namespace_filter, None


def _snapshot_item_from_memory(item: MemoryItem) -> dict[str, object]:
    payload = _normalized_payload(
        namespace=item.namespace,
        key=item.key,
        kind=item.kind,
        value=item.value,
        confidence=item.confidence,
        source=item.source,
        tags=item.tags,
        created_at=item.created_at,
        updated_at=item.updated_at,
        is_tombstoned=item.is_tombstoned,
    )
    return {**payload, "item_hash": _item_hash(payload)}


def _snapshot_item_from_payload(payload: dict[str, object]) -> SnapshotItem:
    namespace = _require_str(payload.get("namespace"), "namespace")
    key = _require_str(payload.get("key"), "key")
    kind = _require_str(payload.get("kind"), "kind")
    value_json = _require_str(payload.get("value_json"), "value_json")
    confidence = _require_str(payload.get("confidence"), "confidence")
    source = _require_str(payload.get("source"), "source")
    created_at = _require_str(payload.get("created_at"), "created_at")
    updated_at = _require_str(payload.get("updated_at"), "updated_at")
    is_tombstoned = payload.get("is_tombstoned")
    if not isinstance(is_tombstoned, bool):
        raise ValueError("Snapshot item is_tombstoned must be a boolean")
    tags = payload.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Snapshot item tags must be a list of strings")
    item_hash = _require_str(payload.get("item_hash"), "item_hash")
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Snapshot item value_json is not valid JSON") from exc
    normalized_payload = _normalized_payload(
        namespace=namespace,
        key=key,
        kind=kind,
        value=value,
        confidence=confidence,
        source=source,
        tags=tags,
        created_at=created_at,
        updated_at=updated_at,
        is_tombstoned=is_tombstoned,
    )
    computed_hash = _item_hash(normalized_payload)
    if computed_hash != item_hash:
        raise ValueError(f"Snapshot item hash mismatch for {namespace}/{key}")
    return SnapshotItem(
        namespace=namespace,
        key=key,
        kind=kind,
        value=value,
        value_json=normalized_payload["value_json"],
        confidence=confidence,
        source=source,
        tags=normalized_payload["tags"],
        created_at=created_at,
        updated_at=updated_at,
        is_tombstoned=is_tombstoned,
        item_hash=item_hash,
    )


def _normalized_payload(
    *,
    namespace: str,
    key: str,
    kind: str,
    value: Any,
    confidence: str,
    source: str,
    tags: Iterable[str],
    created_at: str,
    updated_at: str,
    is_tombstoned: bool,
) -> dict[str, object]:
    return {
        "namespace": namespace,
        "key": key,
        "kind": kind,
        "value_json": canonical_value_json(value),
        "confidence": confidence,
        "source": source,
        "tags": sorted(tags),
        "created_at": created_at,
        "updated_at": updated_at,
        "is_tombstoned": is_tombstoned,
    }


def _item_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _snapshot_hash(item_hashes: Iterable[str]) -> str:
    return hashlib.sha256("".join(item_hashes).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Snapshot item {field} must be a non-empty string")
    return value
