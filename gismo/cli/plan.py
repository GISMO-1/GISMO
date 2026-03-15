"""CLI handlers for `gismo plan` subcommands."""
from __future__ import annotations

import argparse
import json
import sys

from gismo.core.models import PlanStatus
from gismo.core.state import StateStore


# ── helpers ────────────────────────────────────────────────────────────────

def _resolve_one(store: StateStore, id_or_prefix: str) -> str:
    """Resolve prefix → single plan ID, or exit with error."""
    matches = store.resolve_pending_plan_id(id_or_prefix)
    if not matches:
        print(f"Plan not found: {id_or_prefix!r}", file=sys.stderr)
        raise SystemExit(2)
    if len(matches) > 1:
        short = [m[:8] for m in matches[:5]]
        print(
            f"Ambiguous prefix {id_or_prefix!r} matches {len(matches)} plans: "
            + ", ".join(short),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return matches[0]


def _risk_label(level: str) -> str:
    colours = {"LOW": "\033[32mLOW\033[0m", "MEDIUM": "\033[33mMEDIUM\033[0m", "HIGH": "\033[31mHIGH\033[0m"}
    return colours.get(level, level)


def _print_plan_summary(plan: "object") -> None:
    from gismo.core.models import PendingPlan
    assert isinstance(plan, PendingPlan)
    print(f"Plan:     {plan.id}")
    print(f"Status:   {plan.status.value}")
    print(f"Risk:     {_risk_label(plan.risk_level)}")
    print(f"Intent:   {plan.intent}")
    print(f"Prompt:   {plan.user_text[:80]}")
    print(f"Actor:    {plan.actor}")
    print(f"Created:  {plan.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    actions = plan.plan_json.get("actions", [])
    print(f"Actions:  {len(actions)}")
    for i, a in enumerate(actions):
        t = a.get("type", "?")
        cmd = a.get("command", "")
        why = a.get("why", "")
        print(f"  [{i}] {t}: {cmd}")
        if why:
            print(f"       why: {why}")
    rationale = plan.risk_json.get("rationale", [])
    if rationale:
        print("Rationale:")
        for r in rationale:
            print(f"  - {r}")
    if plan.rejection_reason:
        print(f"Rejected: {plan.rejection_reason}")


# ── handlers ───────────────────────────────────────────────────────────────

def handle_plan_list(args: argparse.Namespace) -> None:
    status_filter = None
    if args.status:
        try:
            status_filter = PlanStatus(args.status.upper())
        except ValueError:
            print(f"Unknown status {args.status!r}. Use PENDING, APPROVED, or REJECTED.", file=sys.stderr)
            raise SystemExit(2)

    with StateStore(args.db_path) as store:
        plans = store.list_pending_plans(status=status_filter, limit=args.limit)

    if args.json:
        output = [
            {
                "id": p.id,
                "status": p.status.value,
                "risk_level": p.risk_level,
                "intent": p.intent,
                "user_text": p.user_text,
                "actor": p.actor,
                "created_at": p.created_at.isoformat(),
                "action_count": len(p.plan_json.get("actions", [])),
            }
            for p in plans
        ]
        print(json.dumps(output, ensure_ascii=False))
        return

    if not plans:
        print("No plans found.")
        return

    for p in plans:
        actions = p.plan_json.get("actions", [])
        flags = p.risk_json.get("risk_flags", [])
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        status_disp = p.status.value.lower()
        print(
            f"{p.id[:8]}  {status_disp:<10} {p.risk_level:<8}{flag_str}"
            f"  {p.intent[:40]:<40}  actions={len(actions)}"
        )


def handle_plan_show(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        plan_id = _resolve_one(store, args.id)
        plan = store.get_pending_plan(plan_id)

    if plan is None:
        print(f"Plan not found: {args.id}", file=sys.stderr)
        raise SystemExit(2)

    if args.json:
        print(json.dumps({
            "id": plan.id,
            "status": plan.status.value,
            "risk_level": plan.risk_level,
            "risk": plan.risk_json,
            "intent": plan.intent,
            "user_text": plan.user_text,
            "actor": plan.actor,
            "created_at": plan.created_at.isoformat(),
            "updated_at": plan.updated_at.isoformat(),
            "plan": plan.plan_json,
            "explain": plan.explain_json,
            "rejection_reason": plan.rejection_reason,
            "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
            "rejected_at": plan.rejected_at.isoformat() if plan.rejected_at else None,
        }, ensure_ascii=False, indent=2))
        return

    _print_plan_summary(plan)


def handle_plan_approve(args: argparse.Namespace) -> None:
    from gismo.core.plan_store import enqueue_plan_actions

    with StateStore(args.db_path) as store:
        plan_id = _resolve_one(store, args.id)
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            print(f"Plan not found: {args.id}", file=sys.stderr)
            raise SystemExit(2)
        if plan.status != PlanStatus.PENDING:
            print(f"Plan {plan_id[:8]} is already {plan.status.value.lower()}.", file=sys.stderr)
            raise SystemExit(2)

        _print_plan_summary(plan)
        print()

        if not args.yes:
            try:
                resp = input(f"Approve plan {plan_id[:8]} and enqueue actions? [y/N]: ")
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.", file=sys.stderr)
                raise SystemExit(2)
            if resp.strip().lower() not in {"y", "yes"}:
                print("Not approved.", file=sys.stderr)
                raise SystemExit(2)

        enqueued_ids, skipped = enqueue_plan_actions(store, plan.plan_json)
        store.approve_pending_plan(plan_id)

    if skipped:
        print("Enqueue notes:")
        for note in skipped:
            print(f"  - {note}")
    if enqueued_ids:
        print(f"Approved. Enqueued {len(enqueued_ids)} item(s):")
        for item_id in enqueued_ids:
            print(f"  {item_id}")
    else:
        print("Approved. No items to enqueue.")


def handle_plan_reject(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        plan_id = _resolve_one(store, args.id)
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            print(f"Plan not found: {args.id}", file=sys.stderr)
            raise SystemExit(2)
        if plan.status != PlanStatus.PENDING:
            print(f"Plan {plan_id[:8]} is already {plan.status.value.lower()}.", file=sys.stderr)
            raise SystemExit(2)

        if not args.yes:
            try:
                resp = input(f"Reject plan {plan_id[:8]}? [y/N]: ")
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.", file=sys.stderr)
                raise SystemExit(2)
            if resp.strip().lower() not in {"y", "yes"}:
                print("Cancelled.", file=sys.stderr)
                raise SystemExit(2)

        store.reject_pending_plan(plan_id, reason=args.reason)

    print(f"Plan {plan_id[:8]} rejected.")


def handle_plan_edit(args: argparse.Namespace) -> None:
    with StateStore(args.db_path) as store:
        plan_id = _resolve_one(store, args.id)
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            print(f"Plan not found: {args.id}", file=sys.stderr)
            raise SystemExit(2)
        if plan.status != PlanStatus.PENDING:
            print(f"Plan {plan_id[:8]} is {plan.status.value.lower()} and cannot be edited.", file=sys.stderr)
            raise SystemExit(2)

        new_plan = dict(plan.plan_json)
        actions = list(new_plan.get("actions", []))

        # 1-based action index from user
        idx = args.action - 1
        if idx < 0 or idx >= len(actions):
            print(
                f"Action index {args.action} out of range (plan has {len(actions)} action(s), 1-based).",
                file=sys.stderr,
            )
            raise SystemExit(2)

        if args.remove:
            removed = actions.pop(idx)
            print(f"Removed action {args.action}: {removed.get('command', '')}")
        elif args.cmd:
            from gismo.cli.operator import parse_command
            try:
                parse_command(args.cmd)
            except ValueError as exc:
                print(f"Invalid command: {exc}", file=sys.stderr)
                raise SystemExit(2)
            old_cmd = actions[idx].get("command", "")
            actions[idx] = dict(actions[idx])
            actions[idx]["command"] = args.cmd
            print(f"Action {args.action} updated: {old_cmd!r} → {args.cmd!r}")
        else:
            print("Specify --cmd NEW_COMMAND or --remove.", file=sys.stderr)
            raise SystemExit(2)

        new_plan["actions"] = actions
        store.update_pending_plan_json(plan_id, new_plan)

    print(f"Plan {plan_id[:8]} saved.")
