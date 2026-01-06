"""Memory selection explain CLI."""
from __future__ import annotations

import argparse
import json
import sys

from gismo.core.state import StateStore
from gismo.memory.store import MemorySelectionTrace, list_selection_traces

DEFAULT_EXPLAIN_LIMIT = 200


def run_memory_explain(args: argparse.Namespace) -> None:
    run_id = args.run
    plan_id = args.plan
    if run_id and plan_id:
        print("ERROR: Use only one of --run or --plan.", file=sys.stderr)
        raise SystemExit(2)
    if not run_id and not plan_id:
        print("ERROR: Provide --run or --plan.", file=sys.stderr)
        raise SystemExit(2)
    if args.limit <= 0:
        print("ERROR: --limit must be > 0.", file=sys.stderr)
        raise SystemExit(2)
    state_store = StateStore(args.db_path)
    try:
        if run_id and state_store.get_run(run_id) is None:
            print(f"ERROR: Run not found: {run_id}", file=sys.stderr)
            raise SystemExit(2)
        if plan_id and state_store.get_event(plan_id) is None:
            print(f"ERROR: Plan not found: {plan_id}", file=sys.stderr)
            raise SystemExit(2)
    finally:
        state_store.close()
    traces = list_selection_traces(
        args.db_path,
        run_id=run_id,
        plan_id=plan_id,
        limit=args.limit,
    )
    included = [trace for trace in traces if trace.decision == "include"]
    excluded = [trace for trace in traces if trace.decision != "include"]
    if args.json:
        payload = _build_json_payload(
            run_id=run_id,
            plan_id=plan_id,
            limit=args.limit,
            included=included,
            excluded=excluded,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    _print_human(
        run_id=run_id,
        plan_id=plan_id,
        included=included,
        excluded=excluded,
    )


def _build_json_payload(
    *,
    run_id: str | None,
    plan_id: str | None,
    limit: int,
    included: list[MemorySelectionTrace],
    excluded: list[MemorySelectionTrace],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "plan_id": plan_id,
        "limit": limit,
        "counts": {
            "included": len(included),
            "excluded": len(excluded),
        },
        "included": [trace.to_dict() for trace in included],
        "excluded": [trace.to_dict() for trace in excluded],
    }


def _print_human(
    *,
    run_id: str | None,
    plan_id: str | None,
    included: list[MemorySelectionTrace],
    excluded: list[MemorySelectionTrace],
) -> None:
    title = "Memory selection explain"
    if run_id:
        title = f"{title} (run {run_id})"
    if plan_id:
        title = f"{title} (plan {plan_id})"
    print(title)
    _print_trace_group("Included", included)
    _print_trace_group("Excluded", excluded)


def _print_trace_group(label: str, traces: list[MemorySelectionTrace]) -> None:
    print(f"{label} ({len(traces)}):")
    if not traces:
        print("  - none")
        return
    for trace in traces:
        reason_text = _format_reasons(trace)
        print(
            f"  - {trace.namespace}:{trace.item_key} ({trace.kind}) "
            f"reasons={reason_text}"
        )


def _format_reasons(trace: MemorySelectionTrace) -> str:
    parts: list[str] = []
    for reason in trace.reasons:
        if reason.detail:
            parts.append(f"{reason.code}({reason.detail})")
        else:
            parts.append(reason.code)
    if not parts:
        return "-"
    return ", ".join(parts)
