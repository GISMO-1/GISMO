"""Tool receipt utilities for audit and replay."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gismo.core.permissions import PermissionPolicy
from gismo.core.state import StateStore


REDACTED_VALUE = "[REDACTED]"
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "password",
}


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            normalized_key = key.lower().replace("-", "_")
            if _is_sensitive_key(normalized_key):
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_payload(canonical_payload: str) -> str:
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def tool_kind_for_name(tool_name: str) -> str:
    mapping = {
        "run_shell": "shell",
        "read_file": "fs",
        "write_file": "fs",
        "list_dir": "fs",
        "echo": "builtin",
        "write_note": "state",
        "device_control": "device",
        "calendar_control": "calendar",
    }
    return mapping.get(tool_name, "tool")


def build_policy_snapshot(policy: PermissionPolicy, tool_name: str, *, allowed: bool) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "allowed": allowed,
        "allowed_tools": sorted(policy.allowed_tools),
    }


@dataclass(frozen=True)
class ToolReceiptReplayReport:
    run_id: str
    export_count: int
    db_count: int
    missing_in_export: list[str]
    missing_in_db: list[str]
    hash_mismatches: list[dict[str, str]]
    ordering_matches: bool


def replay_tool_receipts(
    state_store: StateStore,
    *,
    run_id: str,
    export_path: str | Path,
) -> ToolReceiptReplayReport:
    export_records = _load_tool_receipt_export(export_path, run_id=run_id)
    export_ids = [record["id"] for record in export_records]
    db_receipts = list(state_store.list_tool_receipts(run_id))
    db_ids = [receipt.id for receipt in db_receipts]
    missing_in_db = [receipt_id for receipt_id in export_ids if receipt_id not in db_ids]
    missing_in_export = [receipt_id for receipt_id in db_ids if receipt_id not in export_ids]
    hash_mismatches: list[dict[str, str]] = []
    export_lookup = {record["id"]: record for record in export_records}
    for receipt in db_receipts:
        exported = export_lookup.get(receipt.id)
        if not exported:
            continue
        if exported["request_sha256"] != receipt.request_sha256:
            hash_mismatches.append(
                {"id": receipt.id, "field": "request_sha256"}
            )
        if exported["response_sha256"] != receipt.response_sha256:
            hash_mismatches.append(
                {"id": receipt.id, "field": "response_sha256"}
            )
    ordering_matches = export_ids == db_ids
    return ToolReceiptReplayReport(
        run_id=run_id,
        export_count=len(export_records),
        db_count=len(db_receipts),
        missing_in_export=missing_in_export,
        missing_in_db=missing_in_db,
        hash_mismatches=hash_mismatches,
        ordering_matches=ordering_matches,
    )


def _is_sensitive_key(normalized_key: str) -> bool:
    if normalized_key in SENSITIVE_KEYS:
        return True
    if normalized_key.endswith("_token"):
        return True
    return False


def _load_tool_receipt_export(
    export_path: str | Path,
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    path = Path(export_path).expanduser()
    if not path.exists():
        raise ValueError(f"Export file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
            if record.get("record_type") != "tool_receipt":
                continue
            if record.get("run_id") != run_id:
                continue
            _validate_receipt_record(record, line_number)
            records.append(record)
    return records


def _validate_receipt_record(record: dict[str, Any], line_number: int) -> None:
    required_fields = [
        "id",
        "run_id",
        "tool_name",
        "tool_kind",
        "request_payload_json",
        "response_payload_json",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "request_sha256",
        "response_sha256",
    ]
    missing = [field for field in required_fields if field not in record]
    if missing:
        raise ValueError(f"Missing fields in tool_receipt at line {line_number}: {missing}")
    request_payload_json = record["request_payload_json"]
    response_payload_json = record["response_payload_json"]
    if not isinstance(request_payload_json, str) or not isinstance(response_payload_json, str):
        raise ValueError(f"Invalid payload JSON strings at line {line_number}.")
    if sha256_payload(request_payload_json) != record["request_sha256"]:
        raise ValueError(f"Request hash mismatch at line {line_number}.")
    if sha256_payload(response_payload_json) != record["response_sha256"]:
        raise ValueError(f"Response hash mismatch at line {line_number}.")
