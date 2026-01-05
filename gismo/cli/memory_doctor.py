"""Memory doctor CLI handlers."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterable
from uuid import uuid4

from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.memory.store import (
    MEMORY_INDEX_DEFINITIONS,
    MEMORY_TABLE_NAMES,
    append_event,
    policy_hash_for_path,
)

EXPECTED_FOREIGN_KEYS = 1
EXPECTED_JOURNAL_MODE = "wal"
EXPECTED_SYNCHRONOUS = "full"
MAX_DETAIL_ENTRIES = 20
TOMBSTONE_WARN_THRESHOLD = 1000
DEFAULT_PURGE_LIMIT = 1000


@dataclass
class DoctorDecision:
    action: str
    allowed: bool
    confirmation_required: bool
    confirmation_provided: bool
    confirmation_mode: str | None
    reason: str | None


@dataclass
class RepairPlan:
    actions: list[str]
    missing_indexes: list[str]
    purge_candidates: list[tuple[str, str]]
    purge_count: int
    purge_limit: int
    purge_namespace: str | None
    purge_cutoff: str | None
    foreign_keys_before: int | None
    foreign_keys_after: int | None


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resolve_default_policy_path(policy_path: str | None, repo_root: Path) -> tuple[str | None, bool]:
    if policy_path:
        return policy_path, False
    readonly_path = repo_root / "policy" / "readonly.json"
    if readonly_path.exists():
        return str(readonly_path), False
    return None, True


def _warn_missing_default_policy() -> None:
    print(
        "Warning: no policy provided and no policy/readonly.json found; "
        "continuing with existing default tool allowances.",
        file=sys.stderr,
    )


def _load_memory_policy(policy_path: str | None) -> tuple[PermissionPolicy, str | None]:
    repo_root = Path(__file__).resolve().parents[2]
    resolved_path, warn = _resolve_default_policy_path(policy_path, repo_root)
    if warn:
        _warn_missing_default_policy()
    try:
        policy = load_policy(resolved_path, repo_root=repo_root)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Policy file not valid: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return policy, resolved_path


def _policy_hash(policy_path: str | None) -> str:
    try:
        return policy_hash_for_path(policy_path)
    except FileNotFoundError as exc:
        print(f"Policy file not found: {policy_path}", file=sys.stderr)
        raise SystemExit(2) from exc


def _decision_path(*, yes: bool, non_interactive: bool) -> str:
    if non_interactive or yes or not _is_interactive_tty():
        return "non-interactive"
    return "interactive"


def _evaluate_policy(
    policy: PermissionPolicy,
    *,
    action: str,
    namespace: str,
) -> DoctorDecision:
    allowed = policy.memory.is_allowed(action, namespace)
    return DoctorDecision(
        action=action,
        allowed=allowed,
        confirmation_required=policy.memory.requires_confirmation(action, namespace),
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None if allowed else "policy_denied",
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _open_write_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def _collect_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row["name"] for row in rows)


def _collect_indexes(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row["name"] for row in rows)


def _pragma_synchronous_label(value: int) -> str:
    return {
        0: "off",
        1: "normal",
        2: "full",
        3: "extra",
    }.get(value, f"unknown({value})")


def _cap_list(items: list, limit: int) -> tuple[list, bool]:
    if len(items) <= limit:
        return items, False
    return items[:limit], True


def _append_audit_event(
    *,
    db_path: Path,
    operation: str,
    actor: str,
    policy_hash: str,
    request: dict[str, object],
    result_meta: dict[str, object],
) -> bool:
    connection = _open_write_connection(db_path)
    try:
        if not _table_exists(connection, "memory_events"):
            return False
        append_event(
            connection,
            event_id=str(uuid4()),
            operation=operation,
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
            related_run_id=None,
            related_ask_event_id=None,
        )
        connection.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _render_check_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _render_check_human(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    lines = [
        "Memory doctor check",
        f"Status: {payload['status']} (errors={summary['errors']}, warnings={summary['warnings']})",
    ]
    checks = payload["checks"]
    integrity = checks["integrity"]
    lines.append(f"Integrity: {integrity['status']}")
    if integrity.get("messages"):
        for message in integrity["messages"]:
            lines.append(f"  - {message}")
    tables = checks["tables"]
    if tables["status"] != "ok":
        lines.append(f"Tables: {tables['status']}")
        for table in tables.get("missing", []):
            lines.append(f"  - missing: {table}")
    indexes = checks["indexes"]
    if indexes["status"] != "ok":
        lines.append(f"Indexes: {indexes['status']}")
        for name in indexes.get("missing", []):
            lines.append(f"  - missing: {name}")
    pragmas = checks["pragmas"]
    if pragmas["status"] != "ok":
        lines.append("Pragmas:")
        for key in ("foreign_keys", "journal_mode", "synchronous"):
            item = pragmas[key]
            if item["status"] != "ok":
                lines.append(
                    f"  - {key}: expected {item['expected']} got {item['actual']}"
                )
    namespaces = checks["namespaces"]
    if namespaces["status"] != "ok":
        lines.append("Namespaces:")
        for entry in namespaces.get("post_retire_writes", []):
            lines.append(
                f"  - retired {entry['namespace']} has {entry['active_writes']} active writes"
            )
    retention = checks["retention"]
    if retention["status"] != "ok":
        lines.append("Retention:")
        for entry in retention.get("invalid_rules", []):
            lines.append(f"  - invalid: {entry['namespace']} ({entry['reason']})")
        for entry in retention.get("retired_rules", []):
            lines.append(f"  - retired namespace: {entry['namespace']}")
    tombstones = checks["tombstones"]
    if tombstones["status"] != "ok":
        lines.append("Tombstones:")
        for entry in tombstones.get("over_threshold", []):
            lines.append(
                f"  - {entry['namespace']}: tombstones={entry['tombstones']}"
            )
    orphans = checks["orphans"]
    if orphans["status"] != "ok":
        lines.append("Orphaned events:")
        lines.append(f"  - count: {orphans['orphaned_event_count']}")
    return "\n".join(lines)


def run_memory_doctor_check(args: argparse.Namespace) -> None:
    actor = "operator"
    db_path = Path(args.db_path)
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    result_payload: dict[str, object] = {
        "status": "error",
        "exit_code": 3,
        "summary": {"errors": 0, "warnings": 0},
        "checks": {},
    }
    errors: list[str] = []
    warnings: list[str] = []
    if not db_path.exists():
        errors.append("Database file does not exist.")
        result_payload["checks"] = {
            "integrity": {"status": "skipped", "messages": [], "truncated": False},
            "tables": {"status": "error", "missing": list(MEMORY_TABLE_NAMES)},
            "indexes": {"status": "skipped", "missing": []},
            "pragmas": {
                "status": "skipped",
                "foreign_keys": {
                    "status": "skipped",
                    "expected": EXPECTED_FOREIGN_KEYS,
                    "actual": None,
                },
                "journal_mode": {
                    "status": "skipped",
                    "expected": EXPECTED_JOURNAL_MODE,
                    "actual": None,
                },
                "synchronous": {
                    "status": "skipped",
                    "expected": EXPECTED_SYNCHRONOUS,
                    "actual": None,
                },
            },
            "namespaces": {
                "status": "skipped",
                "retired": [],
                "retired_truncated": False,
                "post_retire_writes": [],
                "post_retire_truncated": False,
            },
            "retention": {
                "status": "skipped",
                "invalid_rules": [],
                "invalid_truncated": False,
                "retired_rules": [],
                "retired_truncated": False,
                "duplicate_rules": [],
                "duplicate_truncated": False,
            },
            "tombstones": {
                "status": "skipped",
                "threshold": TOMBSTONE_WARN_THRESHOLD,
                "over_threshold": [],
                "truncated": False,
            },
            "orphans": {
                "status": "skipped",
                "orphaned_event_count": 0,
                "sample": [],
                "sample_truncated": False,
            },
        }
    else:
        connection = _open_readonly_connection(db_path)
        try:
            existing_tables = _collect_tables(connection)
            missing_tables = sorted(set(MEMORY_TABLE_NAMES) - set(existing_tables))
            if missing_tables:
                errors.append("Missing required tables.")
            table_status = "ok" if not missing_tables else "error"

            expected_index_names = [name for name, _ in MEMORY_INDEX_DEFINITIONS]
            existing_indexes = _collect_indexes(connection)
            missing_indexes = sorted(
                set(expected_index_names) - set(existing_indexes)
            )
            index_status = "ok" if not missing_indexes else "warning"
            if missing_indexes:
                warnings.append("Missing expected indexes.")

            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_messages = [row[0] for row in integrity_rows]
            integrity_status = "ok"
            if len(integrity_messages) != 1 or integrity_messages[0] != "ok":
                integrity_status = "error"
                errors.append("Integrity check failed.")
            integrity_messages, integrity_truncated = _cap_list(
                integrity_messages, MAX_DETAIL_ENTRIES
            )

            pragmas_status = "ok"
            pragma_details = {}
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if foreign_keys != EXPECTED_FOREIGN_KEYS:
                try:
                    connection.execute("PRAGMA foreign_keys = ON")
                    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
                except sqlite3.Error:
                    pass
            fk_status = "ok" if foreign_keys == EXPECTED_FOREIGN_KEYS else "warning"
            if fk_status != "ok":
                warnings.append("foreign_keys pragma drift.")
                pragmas_status = "warning"
            pragma_details["foreign_keys"] = {
                "status": fk_status,
                "expected": EXPECTED_FOREIGN_KEYS,
                "actual": foreign_keys,
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            jm_status = "ok" if journal_mode == EXPECTED_JOURNAL_MODE else "warning"
            if jm_status != "ok":
                warnings.append("journal_mode pragma drift.")
                pragmas_status = "warning"
            pragma_details["journal_mode"] = {
                "status": jm_status,
                "expected": EXPECTED_JOURNAL_MODE,
                "actual": journal_mode,
            }
            synchronous_value = connection.execute("PRAGMA synchronous").fetchone()[0]
            synchronous_label = _pragma_synchronous_label(synchronous_value)
            sync_status = "ok" if synchronous_label == EXPECTED_SYNCHRONOUS else "warning"
            if sync_status != "ok":
                warnings.append("synchronous pragma drift.")
                pragmas_status = "warning"
            pragma_details["synchronous"] = {
                "status": sync_status,
                "expected": EXPECTED_SYNCHRONOUS,
                "actual": synchronous_label,
            }

            namespace_status = "ok"
            retired_entries: list[dict[str, object]] = []
            post_retire: list[dict[str, object]] = []
            if "memory_namespaces" in existing_tables:
                retired_rows = connection.execute(
                    "SELECT namespace, retired_at FROM memory_namespaces "
                    "WHERE retired_at IS NOT NULL "
                    "ORDER BY namespace"
                ).fetchall()
                for row in retired_rows:
                    retired_entries.append(
                        {"namespace": row["namespace"], "retired_at": row["retired_at"]}
                    )
                if "memory_items" in existing_tables:
                    for row in retired_rows:
                        count = connection.execute(
                            """
                            SELECT COUNT(*) FROM memory_items
                            WHERE namespace = ? AND is_tombstoned = 0 AND updated_at > ?
                            """,
                            (row["namespace"], row["retired_at"]),
                        ).fetchone()[0]
                        if count:
                            namespace_status = "error"
                            errors.append("Writes after namespace retirement.")
                            post_retire.append(
                                {
                                    "namespace": row["namespace"],
                                    "active_writes": count,
                                }
                            )
            else:
                namespace_status = "skipped"
            retired_entries, retired_truncated = _cap_list(
                retired_entries, MAX_DETAIL_ENTRIES
            )
            post_retire, post_retire_truncated = _cap_list(
                post_retire, MAX_DETAIL_ENTRIES
            )

            retention_status = "ok"
            invalid_rules: list[dict[str, object]] = []
            retired_rules: list[dict[str, object]] = []
            duplicate_rules: list[dict[str, object]] = []
            if "memory_retention_rules" in existing_tables:
                rules = connection.execute(
                    """
                    SELECT namespace, max_items, ttl_seconds
                    FROM memory_retention_rules
                    ORDER BY namespace
                    """
                ).fetchall()
                seen_namespaces: dict[str, int] = {}
                for rule in rules:
                    namespace = rule["namespace"]
                    seen_namespaces[namespace] = seen_namespaces.get(namespace, 0) + 1
                    max_items = rule["max_items"]
                    ttl_seconds = rule["ttl_seconds"]
                    if max_items is not None and max_items < 0:
                        retention_status = "error"
                        errors.append("Invalid retention max_items.")
                        invalid_rules.append(
                            {
                                "namespace": namespace,
                                "reason": "max_items_negative",
                                "max_items": max_items,
                            }
                        )
                    if ttl_seconds is not None and ttl_seconds < 0:
                        retention_status = "error"
                        errors.append("Invalid retention ttl_seconds.")
                        invalid_rules.append(
                            {
                                "namespace": namespace,
                                "reason": "ttl_seconds_negative",
                                "ttl_seconds": ttl_seconds,
                            }
                        )
                for namespace, count in sorted(seen_namespaces.items()):
                    if count > 1:
                        retention_status = "error"
                        errors.append("Duplicate retention rules.")
                        duplicate_rules.append({"namespace": namespace, "count": count})
                if "memory_items" in existing_tables or "memory_namespaces" in existing_tables:
                    namespace_rows = connection.execute(
                        """
                        SELECT DISTINCT namespace FROM (
                            SELECT namespace FROM memory_items
                            UNION
                            SELECT namespace FROM memory_namespaces
                        )
                        """
                    ).fetchall()
                    known_namespaces = {row["namespace"] for row in namespace_rows}
                    for rule in rules:
                        namespace = rule["namespace"]
                        if namespace not in known_namespaces:
                            retention_status = "error"
                            errors.append("Retention rule namespace missing.")
                            invalid_rules.append(
                                {"namespace": namespace, "reason": "missing_namespace"}
                            )
                if "memory_namespaces" in existing_tables:
                    retired_lookup = {
                        row["namespace"]
                        for row in connection.execute(
                            "SELECT namespace FROM memory_namespaces WHERE retired_at IS NOT NULL"
                        ).fetchall()
                    }
                    for rule in rules:
                        namespace = rule["namespace"]
                        if namespace in retired_lookup:
                            if retention_status == "ok":
                                retention_status = "warning"
                            warnings.append("Retention rule for retired namespace.")
                            retired_rules.append({"namespace": namespace})
            else:
                retention_status = "skipped"
            invalid_rules, invalid_truncated = _cap_list(
                invalid_rules, MAX_DETAIL_ENTRIES
            )
            retired_rules, retired_truncated = _cap_list(
                retired_rules, MAX_DETAIL_ENTRIES
            )
            duplicate_rules, duplicate_truncated = _cap_list(
                duplicate_rules, MAX_DETAIL_ENTRIES
            )

            tombstone_status = "ok"
            over_threshold: list[dict[str, object]] = []
            if "memory_items" in existing_tables:
                rows = connection.execute(
                    """
                    SELECT namespace, SUM(is_tombstoned) as tombstones
                    FROM memory_items
                    GROUP BY namespace
                    ORDER BY namespace
                    """
                ).fetchall()
                for row in rows:
                    tombstones = row["tombstones"] or 0
                    if tombstones >= TOMBSTONE_WARN_THRESHOLD:
                        tombstone_status = "warning"
                        warnings.append("Tombstone bloat warning.")
                        over_threshold.append(
                            {
                                "namespace": row["namespace"],
                                "tombstones": tombstones,
                            }
                        )
            else:
                tombstone_status = "skipped"
            over_threshold, tombstone_truncated = _cap_list(
                over_threshold, MAX_DETAIL_ENTRIES
            )

            orphan_status = "ok"
            orphan_count = 0
            orphan_samples: list[dict[str, object]] = []
            if "memory_events" in existing_tables and "memory_items" in existing_tables:
                items = connection.execute(
                    "SELECT namespace, key FROM memory_items"
                ).fetchall()
                item_keys = {(row["namespace"], row["key"]) for row in items}
                event_rows = connection.execute(
                    """
                    SELECT operation, request_json
                    FROM memory_events
                    WHERE operation IN ('put', 'delete')
                    ORDER BY timestamp
                    """
                ).fetchall()
                for row in event_rows:
                    try:
                        payload = json.loads(row["request_json"])
                    except json.JSONDecodeError:
                        continue
                    namespace = payload.get("namespace")
                    key = payload.get("key")
                    if not namespace or not key:
                        continue
                    if (namespace, key) not in item_keys:
                        orphan_count += 1
                        if len(orphan_samples) < MAX_DETAIL_ENTRIES:
                            orphan_samples.append(
                                {
                                    "namespace": namespace,
                                    "key": key,
                                    "operation": row["operation"],
                                }
                            )
                orphan_samples = sorted(
                    orphan_samples, key=lambda entry: (entry["namespace"], entry["key"])
                )
                if orphan_count:
                    orphan_status = "warning"
                    warnings.append("Orphaned events detected.")
            else:
                orphan_status = "skipped"
            orphan_samples, orphan_truncated = _cap_list(
                orphan_samples, MAX_DETAIL_ENTRIES
            )

            result_payload["checks"] = {
                "integrity": {
                    "status": integrity_status,
                    "messages": integrity_messages,
                    "truncated": integrity_truncated,
                },
                "tables": {
                    "status": table_status,
                    "missing": missing_tables,
                },
                "indexes": {
                    "status": index_status,
                    "missing": missing_indexes,
                },
                "pragmas": {
                    "status": pragmas_status,
                    **pragma_details,
                },
                "namespaces": {
                    "status": namespace_status,
                    "retired": retired_entries,
                    "retired_truncated": retired_truncated,
                    "post_retire_writes": post_retire,
                    "post_retire_truncated": post_retire_truncated,
                },
                "retention": {
                    "status": retention_status,
                    "invalid_rules": invalid_rules,
                    "invalid_truncated": invalid_truncated,
                    "retired_rules": retired_rules,
                    "retired_truncated": retired_truncated,
                    "duplicate_rules": duplicate_rules,
                    "duplicate_truncated": duplicate_truncated,
                },
                "tombstones": {
                    "status": tombstone_status,
                    "threshold": TOMBSTONE_WARN_THRESHOLD,
                    "over_threshold": over_threshold,
                    "truncated": tombstone_truncated,
                },
                "orphans": {
                    "status": orphan_status,
                    "orphaned_event_count": orphan_count,
                    "sample": orphan_samples,
                    "sample_truncated": orphan_truncated,
                },
            }
        finally:
            connection.close()

    error_count = len(errors)
    warning_count = len(warnings)
    status = "clean"
    exit_code = 0
    if error_count:
        status = "error"
        exit_code = 3
    elif warning_count:
        status = "warning"
        exit_code = 2
    result_payload["status"] = status
    result_payload["exit_code"] = exit_code
    result_payload["summary"] = {"errors": error_count, "warnings": warning_count}

    if args.json:
        print(_render_check_json(result_payload))
    else:
        print(_render_check_human(result_payload))

    _append_audit_event(
        db_path=db_path,
        operation="memory.doctor.check",
        actor=actor,
        policy_hash=policy_hash,
        request={"command": "check", "json": bool(args.json)},
        result_meta={
            "status": status,
            "exit_code": exit_code,
            "summary": result_payload["summary"],
            "checks": result_payload["checks"],
        },
    )

    raise SystemExit(exit_code)


def _purge_tombstones_plan(
    connection: sqlite3.Connection,
    *,
    namespace: str,
    older_than_seconds: int,
    limit: int,
) -> tuple[list[tuple[str, str]], int, str]:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    cutoff = cutoff_dt.isoformat()
    rows = connection.execute(
        """
        SELECT id, key
        FROM memory_items
        WHERE namespace = ? AND is_tombstoned = 1 AND updated_at <= ?
        ORDER BY updated_at, key, id
        LIMIT ?
        """,
        (namespace, cutoff, limit),
    ).fetchall()
    candidates = [(row["id"], row["key"]) for row in rows]
    return candidates, len(candidates), cutoff


def _purge_tombstones_apply(
    connection: sqlite3.Connection,
    *,
    namespace: str,
    older_than_seconds: int,
    limit: int,
) -> tuple[int, str]:
    candidates, _, cutoff = _purge_tombstones_plan(
        connection,
        namespace=namespace,
        older_than_seconds=older_than_seconds,
        limit=limit,
    )
    if not candidates:
        return 0, cutoff
    ids = [row_id for row_id, _ in candidates]
    placeholders = ",".join("?" for _ in ids)
    connection.execute(
        f"DELETE FROM memory_items WHERE id IN ({placeholders}) AND is_tombstoned = 1",
        ids,
    )
    return len(ids), cutoff


def _collect_missing_indexes(connection: sqlite3.Connection) -> list[str]:
    expected_index_names = [name for name, _ in MEMORY_INDEX_DEFINITIONS]
    existing_indexes = _collect_indexes(connection)
    return sorted(set(expected_index_names) - set(existing_indexes))


def _ensure_repair_inputs(args: argparse.Namespace) -> None:
    if args.purge_tombstones:
        if not args.namespace:
            print("--namespace is required for --purge-tombstones", file=sys.stderr)
            raise SystemExit(2)
        if args.older_than_seconds is None:
            print("--older-than-seconds is required for --purge-tombstones", file=sys.stderr)
            raise SystemExit(2)


def _collect_repair_actions(args: argparse.Namespace) -> list[str]:
    actions: list[str] = []
    if args.vacuum or args.optimize:
        actions.append("vacuum")
    if args.reindex:
        actions.append("reindex")
    if args.rebuild_indexes:
        actions.append("rebuild_indexes")
    if args.enforce_foreign_keys:
        actions.append("enforce_foreign_keys")
    if args.purge_tombstones:
        actions.append("purge_tombstones")
    return actions


def _repair_action_policy(action: str, namespace: str) -> tuple[str, str]:
    action_map = {
        "vacuum": "memory.doctor.vacuum",
        "reindex": "memory.doctor.reindex",
        "rebuild_indexes": "memory.doctor.rebuild_indexes",
        "enforce_foreign_keys": "memory.doctor.enforce_foreign_keys",
        "purge_tombstones": "memory.doctor.purge_tombstones",
    }
    return action_map[action], namespace


def _confirm_repairs(
    *,
    decisions: list[DoctorDecision],
    yes: bool,
    non_interactive: bool,
) -> tuple[list[DoctorDecision], str]:
    decision_path = _decision_path(yes=yes, non_interactive=non_interactive)
    if not any(decision.confirmation_required for decision in decisions):
        return decisions, decision_path
    if yes:
        for decision in decisions:
            if decision.confirmation_required:
                decision.confirmation_provided = True
                decision.confirmation_mode = "yes-flag"
        return decisions, decision_path
    if non_interactive or not _is_interactive_tty():
        for decision in decisions:
            if decision.confirmation_required:
                decision.allowed = False
                decision.reason = "confirmation_required"
        return decisions, decision_path
    response = input("This repair requires confirmation. Proceed? [y/N]:")
    if response.strip().lower() not in {"y", "yes"}:
        for decision in decisions:
            if decision.confirmation_required:
                decision.allowed = False
                decision.reason = "confirmation_declined"
        return decisions, decision_path
    for decision in decisions:
        if decision.confirmation_required:
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    return decisions, decision_path


def _build_repair_plan(
    connection: sqlite3.Connection,
    *,
    actions: list[str],
    namespace: str | None,
    older_than_seconds: int | None,
    limit: int,
) -> RepairPlan:
    missing_indexes: list[str] = []
    purge_candidates: list[tuple[str, str]] = []
    purge_count = 0
    purge_cutoff = None
    foreign_keys_before = None
    foreign_keys_after = None
    if "rebuild_indexes" in actions:
        missing_indexes = _collect_missing_indexes(connection)
    if "purge_tombstones" in actions and namespace and older_than_seconds is not None:
        purge_candidates, purge_count, purge_cutoff = _purge_tombstones_plan(
            connection,
            namespace=namespace,
            older_than_seconds=older_than_seconds,
            limit=limit,
        )
    if "enforce_foreign_keys" in actions:
        foreign_keys_before = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return RepairPlan(
        actions=actions,
        missing_indexes=missing_indexes,
        purge_candidates=purge_candidates,
        purge_count=purge_count,
        purge_limit=limit,
        purge_namespace=namespace,
        purge_cutoff=purge_cutoff,
        foreign_keys_before=foreign_keys_before,
        foreign_keys_after=foreign_keys_after,
    )


def _render_repair_plan(plan: RepairPlan) -> str:
    lines = ["Memory doctor repair plan:"]
    for action in plan.actions:
        lines.append(f"- {action}")
    if plan.missing_indexes:
        lines.append("Missing indexes:")
        for name in plan.missing_indexes:
            lines.append(f"  - {name}")
    if plan.purge_namespace:
        lines.append(
            f"Purge tombstones: namespace={plan.purge_namespace} "
            f"count={plan.purge_count} limit={plan.purge_limit} "
            f"cutoff={plan.purge_cutoff}"
        )
        if plan.purge_candidates:
            candidates, truncated = _cap_list(plan.purge_candidates, MAX_DETAIL_ENTRIES)
            lines.append("Purge candidates:")
            for _, key in candidates:
                lines.append(f"  - {key}")
            if truncated:
                lines.append("  - ... truncated")
    if plan.foreign_keys_before is not None:
        lines.append(f"Foreign keys before: {plan.foreign_keys_before}")
    return "\n".join(lines)


def run_memory_doctor_repair(args: argparse.Namespace) -> None:
    actor = "operator"
    db_path = Path(args.db_path)
    if not db_path.exists():
        print("Database file does not exist.", file=sys.stderr)
        raise SystemExit(2)
    _ensure_repair_inputs(args)
    actions = _collect_repair_actions(args)
    if not actions:
        print("No repairs requested. Use repair flags to select actions.", file=sys.stderr)
        raise SystemExit(2)
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    decisions: list[DoctorDecision] = []
    for action in actions:
        namespace = args.namespace or "global"
        policy_action, policy_namespace = _repair_action_policy(action, namespace)
        decisions.append(
            _evaluate_policy(
                policy,
                action=policy_action,
                namespace=policy_namespace,
            )
        )
    decisions, decision_path = _confirm_repairs(
        decisions=decisions,
        yes=args.yes,
        non_interactive=args.non_interactive,
    )
    if not all(decision.allowed for decision in decisions):
        operation = "memory.doctor.repair.plan" if args.dry_run else "memory.doctor.repair.apply"
        _append_audit_event(
            db_path=db_path,
            operation=operation,
            actor=actor,
            policy_hash=policy_hash,
            request={
                "command": "repair",
                "actions": actions,
                "dry_run": args.dry_run,
                "decision_path": decision_path,
            },
            result_meta={
                "status": "denied",
                "decisions": [decision.__dict__ for decision in decisions],
            },
        )
        print("Repair denied by policy or confirmation.", file=sys.stderr)
        raise SystemExit(2)

    connection = _open_write_connection(db_path)
    try:
        plan = _build_repair_plan(
            connection,
            actions=actions,
            namespace=args.namespace,
            older_than_seconds=args.older_than_seconds,
            limit=args.limit,
        )
        if "enforce_foreign_keys" in actions:
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if foreign_keys != EXPECTED_FOREIGN_KEYS and not args.set_foreign_keys_on:
                operation = (
                    "memory.doctor.repair.plan"
                    if args.dry_run
                    else "memory.doctor.repair.apply"
                )
                _append_audit_event(
                    db_path=db_path,
                    operation=operation,
                    actor=actor,
                    policy_hash=policy_hash,
                    request={
                        "command": "repair",
                        "actions": actions,
                        "dry_run": args.dry_run,
                    },
                    result_meta={
                        "status": "failed",
                        "reason": "foreign_keys_off",
                    },
                )
                print(
                    "foreign_keys is OFF. Re-run with --set-foreign-keys-on to apply.",
                    file=sys.stderr,
                )
                raise SystemExit(2)

        if args.dry_run:
            print(_render_repair_plan(plan))
            _append_audit_event(
                db_path=db_path,
                operation="memory.doctor.repair.plan",
                actor=actor,
                policy_hash=policy_hash,
                request={
                    "command": "repair",
                    "actions": actions,
                    "dry_run": True,
                    "decision_path": decision_path,
                },
                result_meta={
                    "status": "planned",
                    "plan": plan.__dict__,
                    "decisions": [decision.__dict__ for decision in decisions],
                },
            )
            return

        applied_actions: list[str] = []
        applied_meta: dict[str, object] = {}
        if "vacuum" in actions:
            connection.execute("VACUUM")
            connection.execute("ANALYZE")
            applied_actions.append("vacuum")
        if "reindex" in actions:
            connection.execute("REINDEX")
            applied_actions.append("reindex")
        if "rebuild_indexes" in actions:
            for _, statement in MEMORY_INDEX_DEFINITIONS:
                connection.execute(statement)
            applied_actions.append("rebuild_indexes")
            applied_meta["rebuild_indexes_created"] = plan.missing_indexes
        if "enforce_foreign_keys" in actions:
            if args.set_foreign_keys_on:
                connection.execute("PRAGMA foreign_keys = ON")
                applied_actions.append("enforce_foreign_keys")
                applied_meta["foreign_keys_before"] = plan.foreign_keys_before
                applied_meta["foreign_keys_after"] = EXPECTED_FOREIGN_KEYS
        if "purge_tombstones" in actions and args.namespace:
            deleted, cutoff = _purge_tombstones_apply(
                connection,
                namespace=args.namespace,
                older_than_seconds=args.older_than_seconds or 0,
                limit=args.limit,
            )
            applied_actions.append("purge_tombstones")
            applied_meta["purge_tombstones"] = {
                "namespace": args.namespace,
                "deleted": deleted,
                "limit": args.limit,
                "cutoff": cutoff,
            }
        connection.commit()
        print("Repairs applied:")
        for action in applied_actions:
            print(f"- {action}")
        _append_audit_event(
            db_path=db_path,
            operation="memory.doctor.repair.apply",
            actor=actor,
            policy_hash=policy_hash,
            request={
                "command": "repair",
                "actions": actions,
                "dry_run": False,
                "decision_path": decision_path,
            },
            result_meta={
                "status": "applied",
                "applied_actions": applied_actions,
                "meta": applied_meta,
                "decisions": [decision.__dict__ for decision in decisions],
            },
        )
    finally:
        connection.close()
