"""Memory summarize CLI handlers: promote run outcomes into persistent memory."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.memory.store import (
    apply_retention_evictions as memory_apply_retention_evictions,
    get_namespace as memory_get_namespace,
    plan_retention_for_write as memory_plan_retention_for_write,
    policy_hash_for_path,
    put_item as memory_put_item,
    record_event as memory_record_event,
    record_retention_decision as memory_record_retention_decision,
)
from gismo.memory.summarize import RunSummaryItem, RunSummaryPlan, build_run_summary_plan

SUMMARIZE_ACTION = "memory.put"
SUMMARIZE_EVENT = "memory.summarize.run"


@dataclass
class SummarizeDecision:
    action: str
    allowed: bool
    confirmation_required: bool
    confirmation_provided: bool
    confirmation_mode: str | None
    reason: str | None


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resolve_default_policy_path(
    policy_path: str | None, repo_root: Path
) -> tuple[str | None, bool]:
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
) -> SummarizeDecision:
    try:
        policy.check_tool_allowed(action)
    except PermissionError:
        return SummarizeDecision(
            action=action,
            allowed=False,
            confirmation_required=False,
            confirmation_provided=False,
            confirmation_mode=None,
            reason="policy_denied",
        )
    allowed = policy.memory.is_allowed(action, namespace)
    return SummarizeDecision(
        action=action,
        allowed=allowed,
        confirmation_required=policy.memory.requires_confirmation(action, namespace),
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None if allowed else "policy_denied",
    )


def _serialize_plan(plan: RunSummaryPlan) -> dict[str, Any]:
    return {
        "run_id": plan.run_id,
        "namespace": plan.namespace,
        "generated_at": plan.generated_at,
        "item_count": len(plan.items),
        "items": [
            {
                "namespace": plan.namespace,
                "key": item.key,
                "kind": item.kind,
                "value": item.value,
                "tags": item.tags,
                "confidence": item.confidence,
                "source": item.source,
            }
            for item in plan.items
        ],
    }


def _print_plan(plan: RunSummaryPlan) -> None:
    print(
        f"Dry run: would write {len(plan.items)} memory item(s) "
        f"to namespace '{plan.namespace}'"
    )
    for item in plan.items:
        print(
            f"  - {plan.namespace}/{item.key} "
            f"kind={item.kind} confidence={item.confidence}"
        )


def run_memory_summarize_run(args: argparse.Namespace) -> None:
    """Handler for `gismo memory summarize run <RUN_ID>`."""
    actor = "operator"
    db_path = args.db_path
    run_id = args.run_id
    namespace = args.namespace
    dry_run = args.dry_run
    confidence = args.confidence
    include_outputs = args.include_outputs

    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    decision_path_label = _decision_path(yes=args.yes, non_interactive=args.non_interactive)

    with StateStore(db_path) as state_store:
        run = state_store.get_run(run_id)
        if run is None:
            print(f"Run not found: {run_id}", file=sys.stderr)
            raise SystemExit(2)
        tasks = list(state_store.list_tasks(run_id))

    plan = build_run_summary_plan(
        run=run,
        tasks=tasks,
        namespace=namespace,
        confidence=confidence,
        include_outputs=include_outputs,
    )

    if dry_run:
        memory_record_event(
            db_path,
            operation=SUMMARIZE_EVENT,
            actor=actor,
            policy_hash=policy_hash,
            request={
                "run_id": run_id,
                "namespace": namespace,
                "dry_run": True,
                "confidence": confidence,
                "include_outputs": include_outputs,
            },
            result_meta={
                "status": "dry_run",
                "item_count": len(plan.items),
                "decision_path": decision_path_label,
            },
            related_run_id=run_id,
        )
        if args.json:
            print(json.dumps(_serialize_plan(plan), ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_plan(plan)
        return

    decision = _evaluate_policy(policy, action=SUMMARIZE_ACTION, namespace=namespace)

    if not decision.allowed:
        memory_record_event(
            db_path,
            operation=SUMMARIZE_EVENT,
            actor=actor,
            policy_hash=policy_hash,
            request={
                "run_id": run_id,
                "namespace": namespace,
                "dry_run": False,
                "confidence": confidence,
                "include_outputs": include_outputs,
            },
            result_meta={
                "status": "denied",
                "reason": decision.reason,
                "policy_action": decision.action,
                "policy_path": resolved_policy_path,
            },
            related_run_id=run_id,
        )
        print("Memory summarize blocked by policy.", file=sys.stderr)
        raise SystemExit(2)

    namespace_detail = memory_get_namespace(db_path, namespace=namespace)
    if namespace_detail and namespace_detail.retired:
        memory_record_event(
            db_path,
            operation=SUMMARIZE_EVENT,
            actor=actor,
            policy_hash=policy_hash,
            request={
                "run_id": run_id,
                "namespace": namespace,
                "dry_run": False,
                "confidence": confidence,
                "include_outputs": include_outputs,
            },
            result_meta={
                "status": "denied",
                "reason": "namespace_retired",
                "namespace_retired_at": namespace_detail.retired_at,
            },
            related_run_id=run_id,
        )
        print(f"Namespace '{namespace}' is retired.", file=sys.stderr)
        raise SystemExit(2)

    if decision.confirmation_required:
        if args.yes:
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
        elif args.non_interactive or not _is_interactive_tty():
            memory_record_event(
                db_path,
                operation=SUMMARIZE_EVENT,
                actor=actor,
                policy_hash=policy_hash,
                request={
                    "run_id": run_id,
                    "namespace": namespace,
                    "dry_run": False,
                    "confidence": confidence,
                    "include_outputs": include_outputs,
                },
                result_meta={
                    "status": "denied",
                    "reason": "confirmation_required",
                    "decision_path": decision_path_label,
                },
                related_run_id=run_id,
            )
            print(
                "Confirmation required for memory summarize. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(
                f"Write {len(plan.items)} summary item(s) to namespace '{namespace}'? [y/N]: "
            )
            if response.strip().lower() not in {"y", "yes"}:
                memory_record_event(
                    db_path,
                    operation=SUMMARIZE_EVENT,
                    actor=actor,
                    policy_hash=policy_hash,
                    request={
                        "run_id": run_id,
                        "namespace": namespace,
                        "dry_run": False,
                        "confidence": confidence,
                        "include_outputs": include_outputs,
                    },
                    result_meta={
                        "status": "denied",
                        "reason": "confirmation_declined",
                        "decision_path": "interactive",
                    },
                    related_run_id=run_id,
                )
                print("Confirmation declined; summary not written.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"

    written: list[dict[str, Any]] = []
    for summary_item in plan.items:
        retention_plan = memory_plan_retention_for_write(
            db_path, namespace=namespace, key=summary_item.key
        )
        retention_event_id: str | None = None
        if retention_plan is not None:
            if retention_plan.shortfall > 0:
                memory_record_event(
                    db_path,
                    operation=SUMMARIZE_EVENT,
                    actor=actor,
                    policy_hash=policy_hash,
                    request={
                        "run_id": run_id,
                        "namespace": namespace,
                        "key": summary_item.key,
                        "dry_run": False,
                    },
                    result_meta={
                        "status": "denied",
                        "reason": "retention_shortfall",
                        "key": summary_item.key,
                    },
                    related_run_id=run_id,
                )
                print(
                    f"Memory summarize blocked: retention rule cannot be satisfied "
                    f"for '{summary_item.key}'.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            retention_event_id = memory_record_retention_decision(
                db_path,
                plan=retention_plan,
                namespace=namespace,
                key=summary_item.key,
                actor=actor,
                policy_hash=policy_hash,
                related_run_id=run_id,
            )
            if retention_plan.evictions:
                memory_apply_retention_evictions(
                    db_path,
                    plan=retention_plan,
                    actor=actor,
                    policy_hash=policy_hash,
                    retention_event_id=retention_event_id,
                    related_run_id=run_id,
                )

        item = memory_put_item(
            db_path,
            namespace=namespace,
            key=summary_item.key,
            kind=summary_item.kind,
            value=summary_item.value,
            tags=summary_item.tags,
            confidence=summary_item.confidence,
            source=summary_item.source,
            ttl_seconds=None,
            actor=actor,
            policy_hash=policy_hash,
            related_run_id=run_id,
            result_meta_extra={
                "summarize_run_id": run_id,
                "policy_action": SUMMARIZE_ACTION,
                "policy_decision": "allowed",
                "confirmation": {
                    "required": decision.confirmation_required,
                    "provided": decision.confirmation_provided,
                    "mode": decision.confirmation_mode,
                },
                "retention_event_id": retention_event_id,
            },
        )
        written.append({"namespace": namespace, "key": item.key, "kind": item.kind})

    memory_record_event(
        db_path,
        operation=SUMMARIZE_EVENT,
        actor=actor,
        policy_hash=policy_hash,
        request={
            "run_id": run_id,
            "namespace": namespace,
            "dry_run": False,
            "confidence": confidence,
            "include_outputs": include_outputs,
        },
        result_meta={
            "status": "applied",
            "item_count": len(written),
            "items": written,
            "decision_path": decision_path_label,
            "confirmation": {
                "required": decision.confirmation_required,
                "provided": decision.confirmation_provided,
                "mode": decision.confirmation_mode,
            },
            "policy_path": resolved_policy_path,
        },
        related_run_id=run_id,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "status": "applied",
                    "run_id": run_id,
                    "namespace": namespace,
                    "item_count": len(written),
                    "items": written,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    print(
        f"Summarized run {run_id}: "
        f"wrote {len(written)} memory item(s) to '{namespace}'"
    )
    for entry in written:
        print(f"  - {entry['namespace']}/{entry['key']}")
