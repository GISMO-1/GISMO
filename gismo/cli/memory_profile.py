"""Memory profile CLI handlers."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.memory.store import (
    MemoryProfile,
    create_profile,
    get_profile_by_selector,
    list_profiles,
    list_retired_namespaces,
    policy_hash_for_path,
    record_event as memory_record_event,
    retire_profile,
)

ALLOWED_MEMORY_KINDS = {"fact", "preference", "constraint", "procedure", "note", "summary"}


@dataclass
class ProfileDecision:
    action: str
    allowed: bool
    confirmation_required: bool
    confirmation_provided: bool
    confirmation_mode: str | None
    reason: str | None


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


def _evaluate_policy(policy: PermissionPolicy, *, action: str, namespace: str) -> ProfileDecision:
    allowed = policy.memory.is_allowed(action, namespace)
    return ProfileDecision(
        action=action,
        allowed=allowed,
        confirmation_required=policy.memory.requires_confirmation(action, namespace),
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None if allowed else "policy_denied",
    )


def _policy_result_meta(decision: ProfileDecision) -> dict[str, object]:
    return {
        "policy_action": decision.action,
        "policy_decision": "allowed" if decision.allowed else "denied",
        "policy_reason": decision.reason,
        "confirmation": {
            "required": decision.confirmation_required,
            "provided": decision.confirmation_provided,
            "mode": decision.confirmation_mode,
        },
    }


def _normalize_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    normalized = []
    for item in values:
        if not item:
            continue
        normalized.extend(part.strip() for part in item.split(",") if part.strip())
    if not normalized:
        return None
    return sorted(set(normalized))


def _validate_kinds(kinds: list[str] | None) -> list[str] | None:
    if not kinds:
        return None
    invalid = sorted({kind for kind in kinds if kind not in ALLOWED_MEMORY_KINDS})
    if invalid:
        print(
            f"Invalid memory kind(s): {', '.join(invalid)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return kinds


def _serialize_profile(profile: MemoryProfile) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "description": profile.description,
        "include_namespaces": profile.include_namespaces,
        "exclude_namespaces": profile.exclude_namespaces,
        "include_kinds": profile.include_kinds,
        "exclude_kinds": profile.exclude_kinds,
        "max_items": profile.max_items,
        "created_at": profile.created_at,
        "retired_at": profile.retired_at,
    }


def _profile_warnings(profile: MemoryProfile, retired_namespaces: list[str]) -> list[str]:
    if not retired_namespaces:
        return []
    retired = set(retired_namespaces)
    referenced = []
    for namespace in profile.include_namespaces + profile.exclude_namespaces:
        if namespace in retired:
            referenced.append(namespace)
    if not referenced:
        return []
    referenced = sorted(set(referenced))
    return [f"Profile references retired namespace(s): {', '.join(referenced)}"]


def run_memory_profile_list(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    profiles = list_profiles(args.db_path)
    memory_record_event(
        args.db_path,
        operation="memory.profile.list",
        actor=actor,
        policy_hash=policy_hash,
        request={},
        result_meta={"count": len(profiles)},
    )
    if args.json:
        payload = [_serialize_profile(profile) for profile in profiles]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if not profiles:
        print("(no profiles)")
        return
    for profile in profiles:
        status = "retired" if profile.retired_at else "active"
        print(f"- {profile.name} ({profile.profile_id}) [{status}]")


def run_memory_profile_show(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    profile = get_profile_by_selector(args.db_path, args.selector)
    memory_record_event(
        args.db_path,
        operation="memory.profile.show",
        actor=actor,
        policy_hash=policy_hash,
        request={"selector": args.selector},
        result_meta={"found": profile is not None},
    )
    if profile is None:
        print(f"Memory profile not found: {args.selector}")
        raise SystemExit(2)
    warnings = _profile_warnings(profile, list_retired_namespaces(args.db_path))
    if args.json:
        payload = _serialize_profile(profile)
        payload["warnings"] = warnings
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"Profile: {profile.name}")
    print(f"ID: {profile.profile_id}")
    print(f"Description: {profile.description or '-'}")
    print(f"Status: {'retired' if profile.retired_at else 'active'}")
    print(f"Created: {profile.created_at}")
    if profile.retired_at:
        print(f"Retired: {profile.retired_at}")
    print(f"Include namespaces: {', '.join(profile.include_namespaces) or '-'}")
    print(f"Exclude namespaces: {', '.join(profile.exclude_namespaces) or '-'}")
    print(f"Include kinds: {', '.join(profile.include_kinds) or '-'}")
    print(f"Exclude kinds: {', '.join(profile.exclude_kinds) or '-'}")
    print(f"Max items: {profile.max_items if profile.max_items is not None else '-'}")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def run_memory_profile_create(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    include_namespaces = _normalize_list(args.include_namespace)
    exclude_namespaces = _normalize_list(args.exclude_namespace)
    include_kinds = _validate_kinds(_normalize_list(args.include_kind))
    exclude_kinds = _validate_kinds(_normalize_list(args.exclude_kind))
    max_items = args.max_items
    if max_items is not None and max_items < 1:
        print("max-items must be >= 1.", file=sys.stderr)
        raise SystemExit(2)
    action = "memory.profile.create"
    decision = _evaluate_policy(policy, action=action, namespace=args.name)
    if decision.allowed:
        decision.confirmation_required = True
    request = {
        "name": args.name,
        "description": args.description,
        "include_namespaces": include_namespaces,
        "exclude_namespaces": exclude_namespaces,
        "include_kinds": include_kinds,
        "exclude_kinds": exclude_kinds,
        "max_items": max_items,
    }
    if not decision.allowed:
        result_meta = _policy_result_meta(decision)
        result_meta["policy_path"] = resolved_policy_path
        memory_record_event(
            args.db_path,
            operation="memory.profile.create",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
        )
        print("Memory profile create blocked by policy.", file=sys.stderr)
        raise SystemExit(2)
    decision_path = _decision_path(yes=args.yes, non_interactive=args.non_interactive)
    if decision.confirmation_required:
        if args.yes:
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
        elif args.non_interactive or not _is_interactive_tty():
            denied = ProfileDecision(
                action=decision.action,
                allowed=False,
                confirmation_required=True,
                confirmation_provided=False,
                confirmation_mode=None,
                reason="confirmation_required",
            )
            result_meta = _policy_result_meta(denied)
            result_meta.update(
                {
                    "policy_path": resolved_policy_path,
                    "decision_path": decision_path,
                }
            )
            memory_record_event(
                args.db_path,
                operation="memory.profile.create",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
            )
            print(
                "Confirmation required for memory profile create. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(f"Create memory profile {args.name}? [y/N]:")
            if response.strip().lower() not in {"y", "yes"}:
                denied = ProfileDecision(
                    action=decision.action,
                    allowed=False,
                    confirmation_required=True,
                    confirmation_provided=False,
                    confirmation_mode=None,
                    reason="confirmation_declined",
                )
                result_meta = _policy_result_meta(denied)
                result_meta.update(
                    {
                        "policy_path": resolved_policy_path,
                        "decision_path": decision_path,
                    }
                )
                memory_record_event(
                    args.db_path,
                    operation="memory.profile.create",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=result_meta,
                )
                print("Confirmation declined; profile not created.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    try:
        profile = create_profile(
            args.db_path,
            name=args.name,
            description=args.description,
            include_namespaces=include_namespaces,
            exclude_namespaces=exclude_namespaces,
            include_kinds=include_kinds,
            exclude_kinds=exclude_kinds,
            max_items=max_items,
        )
    except ValueError as exc:
        result_meta = _policy_result_meta(decision)
        result_meta.update(
            {
                "policy_path": resolved_policy_path,
                "decision_path": decision_path,
                "error": str(exc),
            }
        )
        memory_record_event(
            args.db_path,
            operation="memory.profile.create",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
        )
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    result_meta = _policy_result_meta(decision)
    result_meta.update(
        {
            "policy_path": resolved_policy_path,
            "decision_path": decision_path,
            "profile_id": profile.profile_id,
            "created_at": profile.created_at,
            "retired_at": profile.retired_at,
        }
    )
    memory_record_event(
        args.db_path,
        operation="memory.profile.create",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )
    if args.json:
        print(json.dumps(_serialize_profile(profile), ensure_ascii=False, sort_keys=True))
        return
    print(f"Created memory profile: {profile.name} ({profile.profile_id})")


def run_memory_profile_retire(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_memory_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    profile = get_profile_by_selector(args.db_path, args.selector)
    if profile is None:
        print(f"Memory profile not found: {args.selector}")
        raise SystemExit(2)
    action = "memory.profile.retire"
    decision = _evaluate_policy(policy, action=action, namespace=profile.name)
    if decision.allowed:
        decision.confirmation_required = True
    request = {"profile_id": profile.profile_id, "name": profile.name}
    if not decision.allowed:
        result_meta = _policy_result_meta(decision)
        result_meta["policy_path"] = resolved_policy_path
        memory_record_event(
            args.db_path,
            operation="memory.profile.retire",
            actor=actor,
            policy_hash=policy_hash,
            request=request,
            result_meta=result_meta,
        )
        print("Memory profile retire blocked by policy.", file=sys.stderr)
        raise SystemExit(2)
    decision_path = _decision_path(yes=args.yes, non_interactive=args.non_interactive)
    if decision.confirmation_required:
        if args.yes:
            decision.confirmation_provided = True
            decision.confirmation_mode = "yes-flag"
        elif args.non_interactive or not _is_interactive_tty():
            denied = ProfileDecision(
                action=decision.action,
                allowed=False,
                confirmation_required=True,
                confirmation_provided=False,
                confirmation_mode=None,
                reason="confirmation_required",
            )
            result_meta = _policy_result_meta(denied)
            result_meta.update(
                {
                    "policy_path": resolved_policy_path,
                    "decision_path": decision_path,
                }
            )
            memory_record_event(
                args.db_path,
                operation="memory.profile.retire",
                actor=actor,
                policy_hash=policy_hash,
                request=request,
                result_meta=result_meta,
            )
            print(
                "Confirmation required for memory profile retire. Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        else:
            response = input(f"Retire memory profile {profile.name}? [y/N]:")
            if response.strip().lower() not in {"y", "yes"}:
                denied = ProfileDecision(
                    action=decision.action,
                    allowed=False,
                    confirmation_required=True,
                    confirmation_provided=False,
                    confirmation_mode=None,
                    reason="confirmation_declined",
                )
                result_meta = _policy_result_meta(denied)
                result_meta.update(
                    {
                        "policy_path": resolved_policy_path,
                        "decision_path": decision_path,
                    }
                )
                memory_record_event(
                    args.db_path,
                    operation="memory.profile.retire",
                    actor=actor,
                    policy_hash=policy_hash,
                    request=request,
                    result_meta=result_meta,
                )
                print("Confirmation declined; profile not retired.", file=sys.stderr)
                raise SystemExit(2)
            decision.confirmation_provided = True
            decision.confirmation_mode = "prompt"
    profile, changed = retire_profile(args.db_path, profile_id=profile.profile_id)
    result_meta = _policy_result_meta(decision)
    result_meta.update(
        {
            "policy_path": resolved_policy_path,
            "decision_path": decision_path,
            "profile_id": profile.profile_id,
            "retired_at": profile.retired_at,
            "changed": changed,
        }
    )
    memory_record_event(
        args.db_path,
        operation="memory.profile.retire",
        actor=actor,
        policy_hash=policy_hash,
        request=request,
        result_meta=result_meta,
    )
    if args.json:
        print(json.dumps(_serialize_profile(profile), ensure_ascii=False, sort_keys=True))
        return
    if changed:
        print(f"Retired memory profile: {profile.name} ({profile.profile_id})")
    else:
        print(f"Memory profile already retired: {profile.name} ({profile.profile_id})")
