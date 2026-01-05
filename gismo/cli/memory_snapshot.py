"""Memory snapshot CLI handlers."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Callable
from uuid import uuid4

from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore
from gismo.memory.snapshot import (
    SnapshotItem,
    export_snapshot,
    load_snapshot,
    memory_item_hash,
    validate_snapshot,
)
from gismo.memory.store import (
    fetch_item_raw,
    record_event as memory_record_event,
    upsert_item_with_timestamps,
)


@dataclass(frozen=True)
class SnapshotDiffEntry:
    namespace: str
    key: str
    action: str
    snapshot_hash: str
    existing_hash: str | None
    snapshot_tombstoned: bool
    existing_tombstoned: bool | None


@dataclass(frozen=True)
class SnapshotDependencies:
    load_memory_policy: Callable[[str | None], tuple[PermissionPolicy, str | None]]
    memory_policy_hash: Callable[[str | None], str]
    memory_decision_path: Callable[..., str]
    evaluate_memory_policy: Callable[[PermissionPolicy, str, str], object]
    memory_policy_result_meta: Callable[[object], dict[str, object]]
    is_interactive_tty: Callable[[], bool]
    memory_decision_cls: type


def _memory_request_from_snapshot_item(item: SnapshotItem) -> dict[str, object]:
    return {
        "namespace": item.namespace,
        "key": item.key,
        "kind": item.kind,
        "value_json": item.value_json,
        "tags_json": json.dumps(item.tags, ensure_ascii=False, sort_keys=True)
        if item.tags
        else None,
        "confidence": item.confidence,
        "source": item.source,
        "ttl_seconds": None,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "is_tombstoned": item.is_tombstoned,
    }


def _validate_snapshot_namespace_filter(namespace_filter: str) -> None:
    if "*" in namespace_filter and not (
        namespace_filter == "*" or namespace_filter.endswith("*")
    ):
        print(
            "Namespace filters may only use '*' as a trailing wildcard (e.g., project:*).",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _snapshot_item_action(item: SnapshotItem) -> str:
    return "memory.delete" if item.is_tombstoned else "memory.put"


def _record_snapshot_import_audit(
    *,
    db_path: str,
    event_id: str,
    actor: str,
    policy_hash: str,
    request: dict[str, object],
    result_meta: dict[str, object],
    dry_run: bool,
) -> None:
    if dry_run:
        state_store = StateStore(db_path)
        try:
            state_store.record_event(
                actor=actor,
                event_type="memory.snapshot_import",
                message="Dry-run memory snapshot import",
                json_payload={
                    "event_id": event_id,
                    "operation": "snapshot_import",
                    "policy_hash": policy_hash,
                    "request": request,
                    "result_meta": result_meta,
                    "dry_run": True,
                },
            )
        finally:
            state_store.close()
        return
    memory_record_event(
        db_path,
        event_id=event_id,
        operation="snapshot_import",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )


def _snapshot_diff_entry_payload(entry: SnapshotDiffEntry) -> dict[str, object]:
    return {
        "namespace": entry.namespace,
        "key": entry.key,
        "snapshot_hash": entry.snapshot_hash,
        "existing_hash": entry.existing_hash,
        "snapshot_tombstoned": entry.snapshot_tombstoned,
        "existing_tombstoned": entry.existing_tombstoned,
    }


def _render_snapshot_diff_json(
    entries: list[SnapshotDiffEntry],
    summary: dict[str, int],
) -> str:
    payload = {
        "adds": [_snapshot_diff_entry_payload(entry) for entry in entries if entry.action == "add"],
        "updates": [
            _snapshot_diff_entry_payload(entry) for entry in entries if entry.action == "update"
        ],
        "tombstones": [
            _snapshot_diff_entry_payload(entry)
            for entry in entries
            if entry.action == "tombstone"
        ],
        "unchanged": [
            _snapshot_diff_entry_payload(entry)
            for entry in entries
            if entry.action == "unchanged"
        ],
        "summary": summary,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _render_snapshot_diff_human(
    *,
    snapshot_path: Path,
    db_path: str,
    entries: list[SnapshotDiffEntry],
    summary: dict[str, int],
) -> str:
    grouped: dict[str, dict[str, list[SnapshotDiffEntry]]] = {}
    for entry in entries:
        grouped.setdefault(entry.namespace, {}).setdefault(entry.action, []).append(entry)

    lines = [
        f"Snapshot diff for {snapshot_path}",
        f"DB: {db_path}",
    ]
    for namespace in sorted(grouped):
        lines.append(f"Namespace: {namespace}")
        for label, action in (
            ("ADD", "add"),
            ("UPDATE", "update"),
            ("TOMBSTONE", "tombstone"),
            ("UNCHANGED", "unchanged"),
        ):
            items_for_action = grouped[namespace].get(action, [])
            lines.append(f"  {label} ({len(items_for_action)})")
            for entry in items_for_action:
                lines.append(f"    - {entry.key}")
    lines.append(
        "Summary: "
        f"adds={summary['adds']} "
        f"updates={summary['updates']} "
        f"tombstones={summary['tombstones']} "
        f"unchanged={summary['unchanged']}"
    )
    return "\n".join(lines)


def run_memory_snapshot_export(args: argparse.Namespace, deps: SnapshotDependencies) -> None:
    actor = "operator"
    _validate_snapshot_namespace_filter(args.namespace)
    _, resolved_policy_path = deps.load_memory_policy(args.policy)
    policy_hash = deps.memory_policy_hash(resolved_policy_path)
    snapshot = export_snapshot(
        args.db_path,
        namespace_filter=args.namespace,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
    out_path.write_text(payload + "\n", encoding="utf-8")
    memory_record_event(
        args.db_path,
        event_id=str(uuid4()),
        operation="snapshot_export",
        actor=actor,
        policy_hash=policy_hash,
        request={
            "namespace_filter": args.namespace,
            "out_path": str(out_path),
        },
        result_meta={
            "item_count": len(snapshot["items"]),
            "snapshot_hash": snapshot["snapshot_hash"],
        },
    )
    print(f"Snapshot exported to {out_path}")
    print(f"Items: {len(snapshot['items'])}")


def run_memory_snapshot_diff(args: argparse.Namespace, deps: SnapshotDependencies) -> None:
    actor = "operator"
    _, resolved_policy_path = deps.load_memory_policy(args.policy)
    policy_hash = deps.memory_policy_hash(resolved_policy_path)
    snapshot_path = Path(args.in_path)
    snapshot_event_id = str(uuid4())
    try:
        snapshot_payload = load_snapshot(snapshot_path)
        items, snapshot_hash = validate_snapshot(snapshot_payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        memory_record_event(
            args.db_path,
            event_id=snapshot_event_id,
            operation="snapshot_diff",
            actor=actor,
            policy_hash=policy_hash,
            request={
                "in_path": str(snapshot_path),
            },
            result_meta={
                "status": "failed",
                "error": str(exc),
            },
        )
        print(f"Invalid snapshot: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    entries: list[SnapshotDiffEntry] = []
    for item in items:
        existing = fetch_item_raw(args.db_path, namespace=item.namespace, key=item.key)
        existing_hash = memory_item_hash(existing) if existing else None
        if existing_hash == item.item_hash:
            action = "unchanged"
        elif item.is_tombstoned:
            action = "tombstone"
        elif existing is None:
            action = "add"
        else:
            action = "update"
        entries.append(
            SnapshotDiffEntry(
                namespace=item.namespace,
                key=item.key,
                action=action,
                snapshot_hash=item.item_hash,
                existing_hash=existing_hash,
                snapshot_tombstoned=item.is_tombstoned,
                existing_tombstoned=existing.is_tombstoned if existing else None,
            )
        )

    entries.sort(key=lambda entry: (entry.namespace, entry.key))

    summary = {
        "adds": len([entry for entry in entries if entry.action == "add"]),
        "updates": len([entry for entry in entries if entry.action == "update"]),
        "tombstones": len([entry for entry in entries if entry.action == "tombstone"]),
        "unchanged": len([entry for entry in entries if entry.action == "unchanged"]),
    }

    if args.json:
        print(_render_snapshot_diff_json(entries, summary))
    else:
        print(
            _render_snapshot_diff_human(
                snapshot_path=snapshot_path,
                db_path=args.db_path,
                entries=entries,
                summary=summary,
            )
        )

    memory_record_event(
        args.db_path,
        event_id=snapshot_event_id,
        operation="snapshot_diff",
        actor=actor,
        policy_hash=policy_hash,
        request={
            "in_path": str(snapshot_path),
            "snapshot_hash": snapshot_hash,
        },
        result_meta={
            "status": "completed",
            "summary": summary,
        },
    )


def run_memory_snapshot_import(args: argparse.Namespace, deps: SnapshotDependencies) -> None:
    actor = "operator"
    policy, resolved_policy_path = deps.load_memory_policy(args.policy)
    policy_hash = deps.memory_policy_hash(resolved_policy_path)
    snapshot_event_id = str(uuid4())
    snapshot_path = Path(args.in_path)
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        snapshot_payload = load_snapshot(snapshot_path)
        items, snapshot_hash = validate_snapshot(snapshot_payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _record_snapshot_import_audit(
            db_path=args.db_path,
            event_id=snapshot_event_id,
            actor=actor,
            policy_hash=policy_hash,
            request={
                "in_path": str(snapshot_path),
                "mode": args.mode,
                "yes": args.yes,
                "non_interactive": args.non_interactive,
                "dry_run": dry_run,
            },
            result_meta={
                "status": "failed",
                "error": str(exc),
                "validated": 0,
                "applied": 0,
                "skipped": 0,
                "denied": 0,
                "mode": args.mode,
                "dry_run": dry_run,
            },
            dry_run=dry_run,
        )
        print(f"Invalid snapshot: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    validated = len(items)
    applied = 0
    skipped = 0
    denied = 0
    exit_code: int | None = None
    update_created_at = args.mode == "overwrite"
    decision_path = deps.memory_decision_path(yes=args.yes, non_interactive=args.non_interactive)

    for item in items:
        existing = fetch_item_raw(args.db_path, namespace=item.namespace, key=item.key)
        if args.mode == "skip-existing" and existing is not None:
            skipped += 1
            continue
        action = _snapshot_item_action(item)
        decision = deps.evaluate_memory_policy(policy, action, item.namespace)
        request = _memory_request_from_snapshot_item(item)
        if not decision.allowed:
            if not dry_run:
                meta = deps.memory_policy_result_meta(decision)
                meta.update(
                    {
                        "snapshot_import_event_id": snapshot_event_id,
                        "snapshot_mode": args.mode,
                    }
                )
                memory_record_event(
                    args.db_path,
                    operation="delete" if item.is_tombstoned else "put",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=meta,
                )
            denied += 1
            exit_code = 2
            continue
        if decision.confirmation_required:
            if args.yes:
                decision.confirmation_provided = True
                decision.confirmation_mode = "yes-flag"
            elif args.non_interactive or not deps.is_interactive_tty():
                denied_decision = deps.memory_decision_cls(
                    action=decision.action,
                    allowed=False,
                    confirmation_required=True,
                    confirmation_provided=False,
                    confirmation_mode=None,
                    reason="confirmation_required",
                )
                if not dry_run:
                    meta = deps.memory_policy_result_meta(denied_decision)
                    meta.update(
                        {
                            "snapshot_import_event_id": snapshot_event_id,
                            "snapshot_mode": args.mode,
                        }
                    )
                    memory_record_event(
                        args.db_path,
                        operation="delete" if item.is_tombstoned else "put",
                        actor=actor,
                        policy_hash=policy_hash,
                        request=request,
                        result_meta=meta,
                    )
                denied += 1
                exit_code = 2
                continue
            else:
                response = input(
                    f"Import snapshot item {item.namespace}/{item.key}? [y/N]:"
                )
                if response.strip().lower() not in {"y", "yes"}:
                    denied_decision = deps.memory_decision_cls(
                        action=decision.action,
                        allowed=False,
                        confirmation_required=True,
                        confirmation_provided=False,
                        confirmation_mode=None,
                        reason="confirmation_declined",
                    )
                    if not dry_run:
                        meta = deps.memory_policy_result_meta(denied_decision)
                        meta.update(
                            {
                                "snapshot_import_event_id": snapshot_event_id,
                                "snapshot_mode": args.mode,
                            }
                        )
                        memory_record_event(
                            args.db_path,
                            operation="delete" if item.is_tombstoned else "put",
                            actor=actor,
                            policy_hash=policy_hash,
                            request=request,
                            result_meta=meta,
                        )
                    denied += 1
                    exit_code = 2
                    continue
                decision.confirmation_provided = True
                decision.confirmation_mode = "prompt"
        result_meta_extra = deps.memory_policy_result_meta(decision)
        result_meta_extra.update(
            {
                "snapshot_import_event_id": snapshot_event_id,
                "snapshot_mode": args.mode,
            }
        )
        if not dry_run:
            upsert_item_with_timestamps(
                args.db_path,
                namespace=item.namespace,
                key=item.key,
                kind=item.kind,
                value=item.value,
                tags=item.tags,
                confidence=item.confidence,
                source=item.source,
                ttl_seconds=None,
                is_tombstoned=item.is_tombstoned,
                created_at=item.created_at,
                updated_at=item.updated_at,
                update_created_at=update_created_at,
                actor=actor,
                policy_hash=policy_hash,
                operation="delete" if item.is_tombstoned else "put",
                result_meta_extra=result_meta_extra,
            )
        applied += 1

    _record_snapshot_import_audit(
        db_path=args.db_path,
        event_id=snapshot_event_id,
        actor=actor,
        policy_hash=policy_hash,
        request={
            "in_path": str(snapshot_path),
            "snapshot_hash": snapshot_hash,
            "mode": args.mode,
            "yes": args.yes,
            "non_interactive": args.non_interactive,
            "decision_path": decision_path,
            "dry_run": dry_run,
        },
        result_meta={
            "status": "completed",
            "validated": validated,
            "applied": applied,
            "skipped": skipped,
            "denied": denied,
            "mode": args.mode,
            "dry_run": dry_run,
        },
        dry_run=dry_run,
    )
    print(
        "Snapshot import summary: "
        f"validated={validated} applied={applied} skipped={skipped} denied={denied}"
    )
    if denied:
        raise SystemExit(exit_code or 2)
