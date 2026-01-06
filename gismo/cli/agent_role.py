"""Agent role CLI handlers."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from gismo.core.permissions import PermissionPolicy, load_policy
from gismo.core.state import StateStore
from gismo.memory.store import (
    get_profile_by_selector as memory_get_profile_by_selector,
    policy_hash_for_path,
)


@dataclass
class RoleDecision:
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


def _load_agent_policy(policy_path: str | None) -> tuple[PermissionPolicy, str | None]:
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


def _evaluate_policy(policy: PermissionPolicy, *, action: str) -> RoleDecision:
    try:
        policy.check_tool_allowed(action)
    except PermissionError:
        return RoleDecision(
            action=action,
            allowed=False,
            confirmation_required=False,
            confirmation_provided=False,
            confirmation_mode=None,
            reason="policy_denied",
        )
    return RoleDecision(
        action=action,
        allowed=True,
        confirmation_required=True,
        confirmation_provided=False,
        confirmation_mode=None,
        reason=None,
    )


def _policy_result_meta(decision: RoleDecision) -> dict[str, object]:
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


def _serialize_role(
    role,
    *,
    memory_profile_name: str | None = None,
    memory_profile_status: str | None = None,
) -> dict[str, object]:
    payload = {
        "role_id": role.role_id,
        "name": role.name,
        "description": role.description,
        "memory_profile_id": role.memory_profile_id,
        "created_at": role.created_at.isoformat(),
        "retired_at": role.retired_at.isoformat() if role.retired_at else None,
    }
    if memory_profile_name is not None:
        payload["memory_profile_name"] = memory_profile_name
    if memory_profile_status is not None:
        payload["memory_profile_status"] = memory_profile_status
    return payload


def _resolve_memory_profile(db_path: str, selector: str) -> tuple[str, str]:
    profile = memory_get_profile_by_selector(db_path, selector)
    if profile is None:
        print(f"Memory profile not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if profile.retired_at:
        print(f"Memory profile is retired: {profile.name}", file=sys.stderr)
        raise SystemExit(2)
    return profile.profile_id, profile.name


def _memory_profile_detail(db_path: str, profile_id: str | None) -> tuple[str | None, str | None]:
    if not profile_id:
        return None, None
    profile = memory_get_profile_by_selector(db_path, profile_id)
    if profile is None:
        return None, "missing"
    if profile.retired_at:
        return profile.name, "retired"
    return profile.name, "active"


def _record_event(
    *,
    state_store: StateStore,
    actor: str,
    action: str,
    request: dict[str, object],
    result_meta: dict[str, object],
) -> None:
    state_store.record_event(
        actor=actor,
        event_type="agent_role",
        message=f"Agent role {action}.",
        json_payload={
            "operation": action,
            "request": request,
            "result_meta": result_meta,
        },
    )


def run_agent_role_list(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_agent_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    state_store = StateStore(args.db_path)
    try:
        roles = state_store.list_agent_roles(include_retired=not args.active_only)
        result_meta = {
            "count": len(roles),
            "policy_path": resolved_policy_path,
            "policy_hash": policy_hash,
        }
        _record_event(
            state_store=state_store,
            actor=actor,
            action="agent.role.list",
            request={"active_only": args.active_only},
            result_meta=result_meta,
        )
        if args.json:
            payload = []
            for role in roles:
                name, status = _memory_profile_detail(args.db_path, role.memory_profile_id)
                payload.append(
                    _serialize_role(
                        role,
                        memory_profile_name=name,
                        memory_profile_status=status,
                    )
                )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        if not roles:
            print("(no roles)")
            return
        for role in roles:
            status = "retired" if role.retired_at else "active"
            profile_name, profile_status = _memory_profile_detail(
                args.db_path,
                role.memory_profile_id,
            )
            profile_text = "-"
            if role.memory_profile_id:
                profile_text = f"{profile_name or role.memory_profile_id}"
                if profile_status == "retired":
                    profile_text += " [retired]"
                elif profile_status == "missing":
                    profile_text += " [missing]"
            print(f"- {role.name} ({role.role_id}) [{status}] profile={profile_text}")
    finally:
        state_store.close()


def run_agent_role_show(args: argparse.Namespace) -> None:
    actor = "operator"
    _, resolved_policy_path = _load_agent_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    state_store = StateStore(args.db_path)
    try:
        role = state_store.get_agent_role_by_selector(args.selector)
        result_meta = {
            "found": role is not None,
            "policy_path": resolved_policy_path,
            "policy_hash": policy_hash,
        }
        _record_event(
            state_store=state_store,
            actor=actor,
            action="agent.role.show",
            request={"selector": args.selector},
            result_meta=result_meta,
        )
        if role is None:
            print(f"Agent role not found: {args.selector}")
            raise SystemExit(2)
        profile_name, profile_status = _memory_profile_detail(
            args.db_path,
            role.memory_profile_id,
        )
        if args.json:
            payload = _serialize_role(
                role,
                memory_profile_name=profile_name,
                memory_profile_status=profile_status,
            )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        print(f"Role: {role.name}")
        print(f"ID: {role.role_id}")
        print(f"Description: {role.description or '-'}")
        print(f"Status: {'retired' if role.retired_at else 'active'}")
        print(f"Created: {role.created_at.isoformat()}")
        if role.retired_at:
            print(f"Retired: {role.retired_at.isoformat()}")
        if role.memory_profile_id:
            profile_value = profile_name or role.memory_profile_id
            if profile_status == "retired":
                profile_value = f"{profile_value} (retired)"
            elif profile_status == "missing":
                profile_value = f"{profile_value} (missing)"
            print(f"Memory profile: {profile_value}")
        else:
            print("Memory profile: -")
    finally:
        state_store.close()


def run_agent_role_create(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_agent_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    memory_profile_id = None
    memory_profile_name = None
    if args.memory_profile:
        memory_profile_id, memory_profile_name = _resolve_memory_profile(
            args.db_path,
            args.memory_profile,
        )
    action = "agent.role.create"
    decision = _evaluate_policy(policy, action=action)
    request = {
        "name": args.name,
        "description": args.description,
        "memory_profile_selector": args.memory_profile,
        "memory_profile_id": memory_profile_id,
        "memory_profile_name": memory_profile_name,
    }
    state_store = StateStore(args.db_path)
    try:
        if not decision.allowed:
            result_meta = _policy_result_meta(decision)
            result_meta.update(
                {
                    "policy_path": resolved_policy_path,
                    "policy_hash": policy_hash,
                }
            )
            _record_event(
                state_store=state_store,
                actor=actor,
                action=action,
                request=request,
                result_meta=result_meta,
            )
            print("Agent role create blocked by policy.", file=sys.stderr)
            raise SystemExit(2)
        decision_path = _decision_path(yes=args.yes, non_interactive=args.non_interactive)
        if decision.confirmation_required:
            if args.yes:
                decision.confirmation_provided = True
                decision.confirmation_mode = "yes-flag"
            elif args.non_interactive or not _is_interactive_tty():
                denied = RoleDecision(
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
                        "policy_hash": policy_hash,
                        "decision_path": decision_path,
                    }
                )
                _record_event(
                    state_store=state_store,
                    actor=actor,
                    action=action,
                    request=request,
                    result_meta=result_meta,
                )
                print(
                    "Confirmation required for agent role create. Re-run with --yes to proceed.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            else:
                response = input(f"Create agent role {args.name}? [y/N]:")
                if response.strip().lower() not in {"y", "yes"}:
                    denied = RoleDecision(
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
                            "policy_hash": policy_hash,
                            "decision_path": decision_path,
                        }
                    )
                    _record_event(
                        state_store=state_store,
                        actor=actor,
                        action=action,
                        request=request,
                        result_meta=result_meta,
                    )
                    print("Confirmation declined; role not created.", file=sys.stderr)
                    raise SystemExit(2)
                decision.confirmation_provided = True
                decision.confirmation_mode = "prompt"
        try:
            role = state_store.create_agent_role(
                name=args.name,
                description=args.description,
                memory_profile_id=memory_profile_id,
            )
        except ValueError as exc:
            result_meta = _policy_result_meta(decision)
            result_meta.update(
                {
                    "policy_path": resolved_policy_path,
                    "policy_hash": policy_hash,
                    "decision_path": decision_path,
                    "error": str(exc),
                }
            )
            _record_event(
                state_store=state_store,
                actor=actor,
                action=action,
                request=request,
                result_meta=result_meta,
            )
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        result_meta = _policy_result_meta(decision)
        result_meta.update(
            {
                "policy_path": resolved_policy_path,
                "policy_hash": policy_hash,
                "decision_path": decision_path,
                "role_id": role.role_id,
                "created_at": role.created_at.isoformat(),
                "retired_at": role.retired_at.isoformat() if role.retired_at else None,
            }
        )
        _record_event(
            state_store=state_store,
            actor=actor,
            action=action,
            request=request,
            result_meta=result_meta,
        )
        if args.json:
            profile_name, profile_status = _memory_profile_detail(
                args.db_path,
                role.memory_profile_id,
            )
            print(
                json.dumps(
                    _serialize_role(
                        role,
                        memory_profile_name=profile_name,
                        memory_profile_status=profile_status,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return
        print(f"Created agent role: {role.name} ({role.role_id})")
    finally:
        state_store.close()


def run_agent_role_retire(args: argparse.Namespace) -> None:
    actor = "operator"
    policy, resolved_policy_path = _load_agent_policy(args.policy)
    policy_hash = _policy_hash(resolved_policy_path)
    action = "agent.role.retire"
    decision = _evaluate_policy(policy, action=action)
    state_store = StateStore(args.db_path)
    try:
        role = state_store.get_agent_role_by_selector(args.selector)
        if role is None:
            print(f"Agent role not found: {args.selector}")
            raise SystemExit(2)
        request = {"role_id": role.role_id, "name": role.name}
        if not decision.allowed:
            result_meta = _policy_result_meta(decision)
            result_meta.update(
                {
                    "policy_path": resolved_policy_path,
                    "policy_hash": policy_hash,
                }
            )
            _record_event(
                state_store=state_store,
                actor=actor,
                action=action,
                request=request,
                result_meta=result_meta,
            )
            print("Agent role retire blocked by policy.", file=sys.stderr)
            raise SystemExit(2)
        decision_path = _decision_path(yes=args.yes, non_interactive=args.non_interactive)
        if decision.confirmation_required:
            if args.yes:
                decision.confirmation_provided = True
                decision.confirmation_mode = "yes-flag"
            elif args.non_interactive or not _is_interactive_tty():
                denied = RoleDecision(
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
                        "policy_hash": policy_hash,
                        "decision_path": decision_path,
                    }
                )
                _record_event(
                    state_store=state_store,
                    actor=actor,
                    action=action,
                    request=request,
                    result_meta=result_meta,
                )
                print(
                    "Confirmation required for agent role retire. Re-run with --yes to proceed.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            else:
                response = input(f"Retire agent role {role.name}? [y/N]:")
                if response.strip().lower() not in {"y", "yes"}:
                    denied = RoleDecision(
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
                            "policy_hash": policy_hash,
                            "decision_path": decision_path,
                        }
                    )
                    _record_event(
                        state_store=state_store,
                        actor=actor,
                        action=action,
                        request=request,
                        result_meta=result_meta,
                    )
                    print("Confirmation declined; role not retired.", file=sys.stderr)
                    raise SystemExit(2)
                decision.confirmation_provided = True
                decision.confirmation_mode = "prompt"
        role, changed = state_store.retire_agent_role(role_id=role.role_id)
        result_meta = _policy_result_meta(decision)
        result_meta.update(
            {
                "policy_path": resolved_policy_path,
                "policy_hash": policy_hash,
                "decision_path": decision_path,
                "role_id": role.role_id,
                "retired_at": role.retired_at.isoformat() if role.retired_at else None,
                "changed": changed,
            }
        )
        _record_event(
            state_store=state_store,
            actor=actor,
            action=action,
            request=request,
            result_meta=result_meta,
        )
        if args.json:
            profile_name, profile_status = _memory_profile_detail(
                args.db_path,
                role.memory_profile_id,
            )
            print(
                json.dumps(
                    _serialize_role(
                        role,
                        memory_profile_name=profile_name,
                        memory_profile_status=profile_status,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return
        if changed:
            print(f"Retired agent role: {role.name} ({role.role_id})")
        else:
            print(f"Agent role already retired: {role.name} ({role.role_id})")
    finally:
        state_store.close()
