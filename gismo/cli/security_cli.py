"""CLI handlers for security, quarantine, trust, and plugin inspection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gismo.core.plugin_signing import (
    PluginVerificationError,
    default_trust_store_path,
    describe_signers,
    inspect_manifest,
    load_manifest,
    load_trust_store,
    verify_manifest,
)
from gismo.core.execution import select_execution_events, summarize_execution_events
from gismo.core.trust_zones import describe_trust_zones
from gismo.core.state import QuarantineRecord, SecurityEvent, StateStore
from gismo.core.trust import (
    TRUST_LABEL_TRUSTED,
    TRUST_LABEL_VERIFIED,
    VERIFICATION_STATUS_UNVERIFIED,
    VERIFICATION_STATUS_VERIFIED,
    ensure_verification_status,
    normalize_trust_labels,
)
from gismo.memory.store import (
    MemoryItem,
    fetch_item_raw,
    item_execution_eligibility,
    item_planning_eligibility,
    list_items_for_snapshot,
)

SECURITY_REVIEW_POLICY_HASH = "security-review"


def run_security_events(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        if args.event_id:
            event = store.get_security_event(event_id=args.event_id)
            if event is None:
                print(f"Security event not found: {args.event_id}", file=sys.stderr)
                raise SystemExit(2)
            previous = store.get_security_event(seq=event.seq - 1) if event.seq > 1 else None
            next_event = store.get_security_event(seq=event.seq + 1)
            payload = _serialize_security_event(
                event,
                previous=previous,
                next_event=next_event,
            )
            if args.json:
                _print_json(payload)
                return
            _print_security_event_detail(payload)
            return

        events = store.list_security_events(
            limit=args.limit,
            event_type=args.event_type,
            related_run_id=args.run_id,
            related_task_id=args.task_id,
            related_plan_id=args.plan_id,
            related_approval_id=args.approval_id,
        )
    payload = {
        "filters": {
            "event_type": args.event_type,
            "related_run_id": args.run_id,
            "related_task_id": args.task_id,
            "related_plan_id": args.plan_id,
            "related_approval_id": args.approval_id,
            "limit": args.limit,
        },
        "events": [_serialize_security_event(event) for event in events],
    }
    if args.json:
        _print_json(payload)
        return
    _print_security_event_list(events)


def run_security_zones(args: argparse.Namespace) -> None:
    payload = describe_trust_zones()
    component_filter = (args.component or "").strip()
    if component_filter:
        payload["components"] = [
            component
            for component in payload["components"]
            if component["component"] == component_filter
        ]
    if args.json:
        _print_json(payload)
        return
    _print_security_zones(payload)


def run_security_execution(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        events = store.list_security_events(
            limit=max(args.recent * 12, 200),
            related_run_id=args.run_id,
            related_task_id=args.task_id,
            related_plan_id=args.plan_id,
        )
    executions = summarize_execution_events(
        events,
        limit=args.recent,
        mode=args.mode,
        zone=args.zone,
        component=args.component,
    )
    payload = {
        "filters": {
            "recent": args.recent,
            "mode": args.mode,
            "zone": args.zone,
            "component": args.component,
            "related_run_id": args.run_id,
            "related_task_id": args.task_id,
            "related_plan_id": args.plan_id,
        },
        "executions": [entry.to_dict() for entry in executions],
    }
    if args.json:
        _print_json(payload)
        return
    _print_execution_list(executions)


def run_security_execution_inspect(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        events = store.list_security_events(limit=5000)
    execution_id = _resolve_execution_id(events, args.selector)
    if execution_id is None:
        print(f"Execution record not found: {args.selector}", file=sys.stderr)
        raise SystemExit(2)
    selected_events = select_execution_events(events, execution_id=execution_id)
    executions = summarize_execution_events(selected_events, limit=1)
    if not executions:
        print(f"Execution record not found: {args.selector}", file=sys.stderr)
        raise SystemExit(2)
    payload = {
        "execution": executions[0].to_dict(),
        "events": [_serialize_security_event(event) for event in selected_events],
    }
    if args.json:
        _print_json(payload)
        return
    _print_execution_detail(payload)


def run_security_verify_chain(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        status = store.validate_security_event_chain()
        mismatch_event = (
            store.get_security_event(event_id=status.mismatch_event_id)
            if status.mismatch_event_id
            else None
        )
    payload = status.to_dict()
    payload["mismatch_event"] = (
        _serialize_security_event(mismatch_event)
        if mismatch_event is not None
        else None
    )
    if args.json:
        _print_json(payload)
    elif status.valid:
        print(f"Security event chain verified. Checked {status.checked} event(s).")
    else:
        print(
            f"Security event chain verification failed at seq {status.mismatch_seq} "
            f"({status.mismatch_event_id or 'unknown event'})."
        )
        if status.reason:
            print(f"Reason:   {status.reason}")
        if status.expected_hash:
            print(f"Expected: {status.expected_hash}")
        if status.actual_hash:
            print(f"Actual:   {status.actual_hash}")
    if not status.valid:
        raise SystemExit(2)


def run_quarantine_list(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        records = store.list_quarantine_records(
            status=args.status,
            verification_status=args.verification_status,
            source_kind=args.source_kind,
            origin_type=args.origin_type,
            limit=args.limit,
        )
    payload = {
        "filters": {
            "status": args.status,
            "verification_status": args.verification_status,
            "source_kind": args.source_kind,
            "origin_type": args.origin_type,
            "limit": args.limit,
        },
        "records": [_serialize_quarantine_record(record, include_content=False) for record in records],
    }
    if args.json:
        _print_json(payload)
        return
    if not records:
        print("No quarantine records found.")
        return
    _print_quarantine_list(records)


def run_quarantine_inspect(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        record = store.get_quarantine_record(args.record_id)
    if record is None:
        print(f"Quarantine record not found: {args.record_id}", file=sys.stderr)
        raise SystemExit(2)
    payload = _serialize_quarantine_record(record, include_content=True)
    if args.json:
        _print_json(payload)
        return
    _print_quarantine_detail(record)


def run_quarantine_promote(args: argparse.Namespace) -> None:
    labels = _parse_labels(args.labels)
    verification_status = _default_verification_status(labels, args.verification_status)
    with StateStore(args.db_path) as store:
        record = store.get_quarantine_record(args.record_id)
        if record is None:
            print(f"Quarantine record not found: {args.record_id}", file=sys.stderr)
            raise SystemExit(2)
        value = _resolve_quarantine_value(record, args)
        namespace = args.namespace or _metadata_default(record, "namespace")
        key = args.key or _metadata_default(record, "key")
        if not namespace or not key:
            print(
                "Quarantine promotion requires --namespace and --key when the record has no default memory target.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        kind = args.kind or _metadata_default(record, "kind") or _default_memory_kind(value)
        source = args.source or _metadata_default(record, "source") or f"quarantine:{record.source_kind}"
        try:
            item = store.promote_quarantine_record(
                record.id,
                namespace=namespace,
                key=key,
                kind=kind,
                value=value,
                source=source,
                actor="operator",
                trust_labels=labels,
                verification_status=verification_status,
                reason=args.reason,
                policy_hash=SECURITY_REVIEW_POLICY_HASH,
                related_run_id=record.related_run_id,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        updated = store.get_quarantine_record(record.id)
    payload = {
        "quarantine": _serialize_quarantine_record(updated or record, include_content=False),
        "memory_item": _serialize_memory_item(item),
    }
    if args.json:
        _print_json(payload)
        return
    print(f"Promoted quarantine record {record.id}.")
    print(f"Memory target: {item.namespace}/{item.key}")
    print(f"Trust labels:  {', '.join(item.trust_labels) if item.trust_labels else '-'}")
    print(f"Verification:  {item.verification_status}")


def run_quarantine_reject(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        try:
            record = store.reject_quarantine_record(
                args.record_id,
                actor="operator",
                reason=args.reason,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
    payload = _serialize_quarantine_record(record, include_content=False)
    if args.json:
        _print_json(payload)
        return
    print(f"Rejected quarantine record {record.id}.")
    print(f"Reason: {record.decision_reason}")


def run_trust_inspect_memory(args: argparse.Namespace) -> None:
    try:
        item = _resolve_memory_item(
            db_path=args.db_path,
            selector=args.selector,
            include_tombstoned=args.include_tombstoned,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if item is None:
        print(f"Memory item not found: {args.selector}", file=sys.stderr)
        raise SystemExit(2)
    payload = _serialize_memory_item(item)
    if args.json:
        _print_json(payload)
        return
    _print_memory_trust_detail(item)


def run_trust_transitions(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        events = store.list_security_events(
            limit=args.limit,
            event_type="trust_transition",
            related_run_id=args.run_id,
            related_task_id=args.task_id,
            related_plan_id=args.plan_id,
        )
    payload = {
        "filters": {
            "related_run_id": args.run_id,
            "related_task_id": args.task_id,
            "related_plan_id": args.plan_id,
            "limit": args.limit,
        },
        "events": [_serialize_security_event(event) for event in events],
    }
    if args.json:
        _print_json(payload)
        return
    if not events:
        print("No trust transitions found.")
        return
    for event in events:
        before = ", ".join(event.payload.get("labels_before") or []) or "-"
        after = ", ".join(event.payload.get("labels_after") or []) or "-"
        reason = event.payload.get("reason") or "-"
        print(
            f"{event.seq:06d}  {event.timestamp.isoformat()}  {event.resource}  "
            f"{before} -> {after}  reason={reason}"
        )


def run_plugins_signers(args: argparse.Namespace) -> None:
    trust_store, trust_store_path = _load_plugin_trust_store(args.trust_store, args.db_path)
    payload = {
        "trust_store_path": str(trust_store_path),
        "signers": describe_signers(trust_store),
    }
    if args.json:
        _print_json(payload)
        return
    print(f"Trust store: {trust_store_path}")
    if not payload["signers"]:
        print("No signers configured.")
        return
    for signer in payload["signers"]:
        note = signer["note"] or "-"
        status = "trusted" if signer["trusted"] else "untrusted"
        print(f"{signer['signer_id']}  {status:<9} note={note}")


def run_plugins_verify(args: argparse.Namespace) -> None:
    trust_store, trust_store_path = _load_plugin_trust_store(args.trust_store, args.db_path)
    manifest_paths = _collect_manifest_paths(args.manifest, args.manifest_dir)
    if not manifest_paths:
        print("No plugin or adapter manifests found.")
        return
    reports: list[dict[str, Any]] = []
    failures = 0
    for manifest_path in manifest_paths:
        try:
            manifest = load_manifest(manifest_path)
            report = inspect_manifest(manifest, trust_store=trust_store)
            try:
                verify_manifest(
                    manifest,
                    trust_store=trust_store,
                    db_path=args.db_path,
                    actor="operator",
                )
            except PluginVerificationError:
                pass
            reports.append(
                {
                    "manifest_path": str(manifest_path),
                    **report.to_dict(),
                }
            )
            if not report.verified:
                failures += 1
        except (OSError, json.JSONDecodeError, PluginVerificationError, ValueError) as exc:
            failures += 1
            _record_plugin_manifest_failure(
                db_path=args.db_path,
                manifest_path=manifest_path,
                reason=str(exc),
            )
            reports.append(
                {
                    "manifest_path": str(manifest_path),
                    "plugin_id": None,
                    "version": None,
                    "entrypoint": None,
                    "signer_id": None,
                    "signed": False,
                    "trusted_signer": False,
                    "tampered": None,
                    "verified": False,
                    "reason": str(exc),
                    "manifest_sha256": None,
                }
            )
    payload = {
        "trust_store_path": str(trust_store_path),
        "reports": reports,
        "summary": {
            "manifests": len(reports),
            "verified": sum(1 for report in reports if report["verified"]),
            "failed": sum(1 for report in reports if not report["verified"]),
        },
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Trust store: {trust_store_path}")
        for report in reports:
            status = "OK" if report["verified"] else "FAIL"
            signer = report["signer_id"] or "-"
            signed = "yes" if report["signed"] else "no"
            trusted_signer = "yes" if report["trusted_signer"] else "no"
            tampered = "-"
            if report["tampered"] is True:
                tampered = "yes"
            elif report["tampered"] is False:
                tampered = "no"
            print(
                f"{status:<4} {report['manifest_path']}  "
                f"plugin={report['plugin_id'] or '-'} signer={signer} "
                f"signed={signed} trusted-signer={trusted_signer} tampered={tampered}"
            )
            if report["reason"]:
                print(f"     reason={report['reason']}")
    if failures:
        raise SystemExit(2)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _parse_labels(values: list[str]) -> list[str]:
    labels: list[str] = []
    for value in values:
        for item in str(value).split(","):
            text = item.strip()
            if text:
                labels.append(text)
    normalized = normalize_trust_labels(labels)
    if not normalized:
        print("At least one trust label is required.", file=sys.stderr)
        raise SystemExit(2)
    return normalized


def _default_verification_status(labels: list[str], explicit: str | None) -> str:
    if explicit:
        return ensure_verification_status(explicit)
    if TRUST_LABEL_TRUSTED in labels or TRUST_LABEL_VERIFIED in labels:
        return VERIFICATION_STATUS_VERIFIED
    return VERIFICATION_STATUS_UNVERIFIED


def _serialize_security_event(
    event: SecurityEvent,
    *,
    previous: SecurityEvent | None = None,
    next_event: SecurityEvent | None = None,
) -> dict[str, Any]:
    payload = event.to_dict()
    if previous is not None or next_event is not None:
        payload["chain"] = {
            "previous": (
                {
                    "seq": previous.seq,
                    "id": previous.id,
                    "event_hash": previous.event_hash,
                }
                if previous is not None
                else None
            ),
            "current": {
                "seq": event.seq,
                "id": event.id,
                "prev_event_id": event.prev_event_id,
                "prev_hash": event.prev_hash,
                "event_hash": event.event_hash,
            },
            "next": (
                {
                    "seq": next_event.seq,
                    "id": next_event.id,
                    "prev_event_id": next_event.prev_event_id,
                    "prev_hash": next_event.prev_hash,
                }
                if next_event is not None
                else None
            ),
        }
    return payload


def _print_security_event_list(events: list[SecurityEvent]) -> None:
    if not events:
        print("No security events found.")
        return
    for event in events:
        run_id = event.related_run_id or "-"
        task_id = event.related_task_id or "-"
        plan_id = event.related_plan_id or "-"
        print(
            f"{event.seq:06d}  {event.timestamp.isoformat()}  {event.event_type:<26} "
            f"{event.actor:<12} {event.resource}  run={run_id} task={task_id} plan={plan_id}"
        )


def _print_security_event_detail(payload: dict[str, Any]) -> None:
    print(f"Seq:       {payload['seq']}")
    print(f"ID:        {payload['id']}")
    print(f"Time:      {payload['timestamp']}")
    print(f"Type:      {payload['event_type']}")
    print(f"Actor:     {payload['actor']}")
    print(f"Action:    {payload['action']}")
    print(f"Resource:  {payload['resource']}")
    print(f"Run:       {payload.get('related_run_id') or '-'}")
    print(f"Task:      {payload.get('related_task_id') or '-'}")
    print(f"Plan:      {payload.get('related_plan_id') or '-'}")
    print(f"Approval:  {payload.get('related_approval_id') or '-'}")
    if "chain" in payload:
        chain = payload["chain"]
        previous = chain.get("previous") or {}
        next_event = chain.get("next") or {}
        current = chain.get("current") or {}
        print(f"Previous:  {previous.get('id') or '-'}")
        print(f"Prev hash: {current.get('prev_hash') or '-'}")
        print(f"Hash:      {current.get('event_hash') or '-'}")
        print(f"Next:      {next_event.get('id') or '-'}")
    print("Payload:")
    print(json.dumps(payload.get("payload") or {}, ensure_ascii=False, sort_keys=True, indent=2))


def _print_execution_list(executions: list[Any]) -> None:
    if not executions:
        print("No execution records found.")
        return
    for execution in executions:
        print(
            f"{execution.execution_id[:12]}  {execution.timestamp}  "
            f"{execution.component:<18} {execution.zone:<16} {execution.mode:<20} "
            f"{execution.result:<10} run={execution.related_run_id or '-'} task={execution.related_task_id or '-'}"
        )


def _print_execution_detail(payload: dict[str, Any]) -> None:
    execution = payload["execution"]
    print(f"Execution:  {execution['execution_id']}")
    print(f"Time:       {execution['timestamp']}")
    print(f"Component:  {execution['component']}")
    print(f"Zone:       {execution['zone']}")
    print(f"Mode:       {execution['mode']}")
    print(f"Action:     {execution['action']}")
    print(f"Resource:   {execution['resource']}")
    print(f"Result:     {execution['result']}")
    print(f"Run:        {execution.get('related_run_id') or '-'}")
    print(f"Task:       {execution.get('related_task_id') or '-'}")
    print(f"Plan:       {execution.get('related_plan_id') or '-'}")
    if execution.get("sandbox_profile"):
        print("Sandbox:")
        print(json.dumps(execution["sandbox_profile"], ensure_ascii=False, sort_keys=True, indent=2))
    print("Events:")
    for event in payload["events"]:
        print(
            f"  {event['seq']:06d} {event['timestamp']} {event['event_type']} "
            f"{event['payload'].get('result') or '-'}"
        )


def _print_security_zones(payload: dict[str, Any]) -> None:
    zones = payload.get("zones") or []
    components = payload.get("components") or []
    print("Trust zones:")
    for zone in zones:
        print(
            f"- {zone['zone']}  boundary={zone['boundary_kind']}  "
            f"{zone['semantics']}"
        )
        print(f"  note={zone['notes']}")
    print("Components:")
    if not components:
        print("  No components matched the requested filter.")
        return
    for component in components:
        print(
            f"- {component['component']}  zone={component['zone']}  "
            f"mode={component['default_execution_mode']}  "
            f"state={component['state_access']}  boundary={component['boundary_kind']}"
        )
        print(f"  note={component['notes']}")


def _resolve_execution_id(events: list[SecurityEvent], selector: str) -> str | None:
    text = (selector or "").strip()
    if not text:
        return None
    executions = summarize_execution_events(events, limit=max(len(events), 1))
    exact = [entry.execution_id for entry in executions if entry.execution_id == text]
    if exact:
        return exact[0]
    prefix = [entry.execution_id for entry in executions if entry.execution_id.startswith(text)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def _serialize_quarantine_record(record: QuarantineRecord, *, include_content: bool) -> dict[str, Any]:
    payload = {
        "id": record.id,
        "created_at": record.created_at.isoformat(),
        "source_kind": record.source_kind,
        "source_ref": record.source_ref,
        "origin_type": record.origin_type,
        "content_sha256": record.content_sha256,
        "verification_status": record.verification_status,
        "trust_labels": list(record.trust_labels),
        "provenance": dict(record.provenance_json),
        "metadata": dict(record.metadata_json),
        "status": record.status,
        "decision_reason": record.decision_reason,
        "decision_at": record.decision_at.isoformat() if record.decision_at else None,
        "memory_namespace": record.memory_namespace,
        "memory_key": record.memory_key,
        "related_run_id": record.related_run_id,
        "related_task_id": record.related_task_id,
        "related_plan_id": record.related_plan_id,
        "related_event_id": record.related_event_id,
        "content_present": record.content is not None,
    }
    if include_content:
        payload["content"] = record.content
    return payload


def _print_quarantine_list(records: list[QuarantineRecord]) -> None:
    for record in records:
        labels = ",".join(record.trust_labels) if record.trust_labels else "-"
        source = record.source_ref or record.source_kind
        print(
            f"{record.id[:12]}  {record.status:<10} {record.origin_type:<16} "
            f"{record.verification_status:<10} {labels:<30} {record.created_at.isoformat()}  {source}"
        )


def _print_quarantine_detail(record: QuarantineRecord) -> None:
    print(f"ID:          {record.id}")
    print(f"Created:     {record.created_at.isoformat()}")
    print(f"Status:      {record.status}")
    print(f"Source kind: {record.source_kind}")
    print(f"Source ref:  {record.source_ref or '-'}")
    print(f"Origin:      {record.origin_type}")
    print(f"SHA256:      {record.content_sha256 or '-'}")
    print(f"Verification:{' '}{record.verification_status}")
    print(f"Trust labels:{' '}{', '.join(record.trust_labels) if record.trust_labels else '-'}")
    print(f"Memory link: {(record.memory_namespace or '-')}/{record.memory_key or '-'}")
    print(f"Run:         {record.related_run_id or '-'}")
    print(f"Task:        {record.related_task_id or '-'}")
    print(f"Plan:        {record.related_plan_id or '-'}")
    print(f"Event:       {record.related_event_id or '-'}")
    if record.decision_reason:
        print(f"Decision:    {record.decision_reason}")
    print("Provenance:")
    print(json.dumps(record.provenance_json, ensure_ascii=False, sort_keys=True, indent=2))
    print("Metadata:")
    print(json.dumps(record.metadata_json, ensure_ascii=False, sort_keys=True, indent=2))
    print("Content:")
    if record.content is None:
        print("  -")
        return
    preview = json.dumps(record.content, ensure_ascii=False, sort_keys=True, indent=2)
    if len(preview) > 800:
        preview = preview[:800] + "\n..."
    print(preview)


def _serialize_memory_item(item: MemoryItem) -> dict[str, Any]:
    planning = item_planning_eligibility(item)
    execution = item_execution_eligibility(item)
    return {
        "id": item.id,
        "namespace": item.namespace,
        "key": item.key,
        "kind": item.kind,
        "value": item.value,
        "tags": list(item.tags),
        "confidence": item.confidence,
        "source": item.source,
        "source_type": item.source_type,
        "verification_status": item.verification_status,
        "trust_labels": list(item.trust_labels),
        "provenance": dict(item.provenance_json),
        "ttl_seconds": item.ttl_seconds,
        "is_tombstoned": item.is_tombstoned,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "planning_eligibility": planning.to_dict(),
        "execution_eligibility": execution.to_dict(),
    }


def _print_memory_trust_detail(item: MemoryItem) -> None:
    payload = _serialize_memory_item(item)
    print(f"Memory:      {item.namespace}/{item.key}")
    print(f"Kind:        {item.kind}")
    print(f"Source:      {item.source}")
    print(f"Source type: {item.source_type}")
    print(f"Verification:{' '}{item.verification_status}")
    print(f"Trust labels:{' '}{', '.join(item.trust_labels) if item.trust_labels else '-'}")
    print(f"Tombstoned:  {'yes' if item.is_tombstoned else 'no'}")
    print(
        "Planning:    "
        f"{'eligible' if payload['planning_eligibility']['eligible'] else 'not eligible'} "
        f"({', '.join(payload['planning_eligibility']['reasons']) or 'ok'})"
    )
    print(
        "Execution:   "
        f"{'eligible' if payload['execution_eligibility']['eligible'] else 'not eligible'} "
        f"({', '.join(payload['execution_eligibility']['reasons']) or 'ok'})"
    )
    print("Provenance:")
    print(json.dumps(item.provenance_json, ensure_ascii=False, sort_keys=True, indent=2))
    print("Value:")
    print(json.dumps(item.value, ensure_ascii=False, sort_keys=True, indent=2))


def _resolve_memory_item(
    *,
    db_path: str,
    selector: str,
    include_tombstoned: bool,
) -> MemoryItem | None:
    if "/" in selector:
        namespace, key = selector.split("/", 1)
        item = fetch_item_raw(db_path, namespace=namespace, key=key)
        if item is None:
            return None
        if item.is_tombstoned and not include_tombstoned:
            raise ValueError(
                f"Memory item is tombstoned: {namespace}/{key}. Re-run with --include-tombstoned."
            )
        return item
    matches = [
        item
        for item in list_items_for_snapshot(db_path, namespace=None, namespace_prefix=None)
        if item.key == selector and (include_tombstoned or not item.is_tombstoned)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        options = ", ".join(f"{item.namespace}/{item.key}" for item in matches[:5])
        raise ValueError(f"Memory selector is ambiguous: {selector}. Matches: {options}")
    return matches[0]


def _resolve_quarantine_value(record: QuarantineRecord, args: argparse.Namespace) -> Any:
    if args.value is not None and args.value_text is not None:
        print("Provide only one of --value or --value-text.", file=sys.stderr)
        raise SystemExit(2)
    if args.value_text is not None:
        return args.value_text
    if args.value is not None:
        try:
            return json.loads(args.value)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON for --value: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    if record.content is None:
        print(
            "Quarantine record does not contain stored content. Provide --value or --value-text.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return record.content


def _default_memory_kind(value: Any) -> str:
    return "note" if isinstance(value, str) else "fact"


def _metadata_default(record: QuarantineRecord, field_name: str) -> str | None:
    target = record.metadata_json.get("memory_target")
    if isinstance(target, dict):
        value = target.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = record.metadata_json.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _load_plugin_trust_store(path_value: str | None, db_path: str) -> tuple[object, Path]:
    trust_store_path = Path(path_value) if path_value else default_trust_store_path(db_path)
    if not trust_store_path.exists():
        print(f"Plugin trust store not found: {trust_store_path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return load_trust_store(trust_store_path), trust_store_path
    except (OSError, json.JSONDecodeError, PluginVerificationError, ValueError) as exc:
        print(f"Failed to load plugin trust store: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _collect_manifest_paths(
    manifests: list[str] | None,
    manifest_dirs: list[str] | None,
) -> list[Path]:
    paths: list[Path] = []
    for entry in manifests or []:
        path = Path(entry)
        if path.is_file():
            paths.append(path)
    for entry in manifest_dirs or []:
        directory = Path(entry)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_file():
                paths.append(path)
    if paths:
        return _dedupe_paths(paths)
    default_dirs = [
        Path("plugins"),
        Path("adapters"),
        Path(".gismo") / "plugins",
        Path(".gismo") / "adapters",
    ]
    discovered: list[Path] = []
    for directory in default_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_file():
                discovered.append(path)
    return _dedupe_paths(discovered)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def _record_plugin_manifest_failure(
    *,
    db_path: str,
    manifest_path: Path,
    reason: str,
) -> None:
    with StateStore(db_path) as store:
        store.record_security_event(
            event_type="plugin_signature_rejected",
            actor="operator",
            action="verify",
            resource=f"plugin_manifest:{manifest_path}",
            payload={"reason": reason, "manifest_path": str(manifest_path)},
        )
