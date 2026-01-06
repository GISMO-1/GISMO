"""Memory preview CLI handlers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.memory.injection import (
    MEMORY_INJECTION_BYTE_CAP,
    MEMORY_INJECTION_ITEM_CAP,
    build_memory_injection_trace,
    profile_filters_payload,
    profile_selection_filters,
    select_injection_items,
)
from gismo.memory.store import MemoryProfile, get_profile_by_selector, list_profile_items

READ_ACTION = "memory.read"


def run_memory_preview(args: argparse.Namespace) -> None:
    profile = _resolve_profile(args.db_path, args.memory_profile)
    namespace_filters = _normalize_list(args.namespace)
    policy, _ = _load_memory_policy(args.policy)
    tool_allowed = _check_tool_allowed(policy)

    def policy_checker(namespace: str) -> bool:
        if not tool_allowed:
            return False
        return policy.memory.is_allowed(READ_ACTION, namespace)

    items = list_profile_items(
        args.db_path,
        profile=profile,
        limit=MEMORY_INJECTION_ITEM_CAP,
    )
    selection_filters = profile_selection_filters(
        profile,
        namespace_filters=namespace_filters,
    )
    scoped_items = [
        item for item in items if selection_filters.matches_namespace(item.namespace)
    ]
    allowed_items = [item for item in scoped_items if policy_checker(item.namespace)]
    selection = select_injection_items(
        allowed_items,
        cap_items=MEMORY_INJECTION_ITEM_CAP,
        cap_bytes=MEMORY_INJECTION_BYTE_CAP,
    )
    effective_limit = min(
        MEMORY_INJECTION_ITEM_CAP,
        profile.max_items if profile.max_items is not None else MEMORY_INJECTION_ITEM_CAP,
    )
    trace = build_memory_injection_trace(
        args.db_path,
        selected_items=selection.items,
        source="--memory-profile",
        filters=selection_filters,
        cap_items=MEMORY_INJECTION_ITEM_CAP,
        cap_bytes=MEMORY_INJECTION_BYTE_CAP,
        profile=profile_filters_payload(profile, effective_limit),
        policy_checker=policy_checker,
    )
    if args.json:
        print(json.dumps(trace.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return
    _print_preview(trace)


def _print_preview(trace) -> None:
    print("Memory preview (profile)")
    print(f"Source: {trace.source}")
    eligibility = trace.counts.to_dict()
    print(
        "Selected: "
        f"{eligibility.get('selected_items')}/"
        f"{eligibility.get('filtered_items')} "
        f"(cap_items={eligibility.get('cap_items')} cap_bytes={eligibility.get('cap_bytes')})"
    )
    print(f"Injection hash: {trace.injection_hash}")
    if trace.denied_namespaces:
        denied = ", ".join(
            f"{entry['namespace']}={entry['count']}" for entry in trace.denied_namespaces
        )
        print(f"Denied namespaces: {denied}")
    print("Selected items:")
    if not trace.selected_items:
        print("  - none")
        return
    for item in trace.selected_items:
        print(
            "  - "
            f"{item.namespace}/{item.key} kind={item.kind} "
            f"confidence={item.confidence} updated_at={item.updated_at} "
            f"hash={item.item_hash}"
        )


def _resolve_profile(db_path: str, selector: str) -> MemoryProfile:
    profile = get_profile_by_selector(db_path, selector)
    if profile is None:
        print(f"Memory profile not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if profile.retired_at:
        print(f"Memory profile is retired: {profile.name}", file=sys.stderr)
        raise SystemExit(2)
    return profile


def _normalize_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    normalized: list[str] = []
    for item in values:
        if not item:
            continue
        normalized.extend(part.strip() for part in item.split(",") if part.strip())
    if not normalized:
        return None
    return sorted(set(normalized))


def _resolve_default_policy_path(
    policy_path: str | None,
    repo_root: Path,
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


def _check_tool_allowed(policy: PermissionPolicy) -> bool:
    try:
        policy.check_tool_allowed(READ_ACTION)
    except PermissionError:
        return False
    return True
