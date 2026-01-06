"""Agent session CLI handlers."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import sys
from typing import Callable
from uuid import uuid4

from gismo.core.models import AgentSession, AgentSessionStatus, EVENT_TYPE_LLM_PLAN, QueueStatus
from gismo.core.state import StateStore
from gismo.memory.store import get_profile_by_selector as memory_get_profile_by_selector


@dataclass(frozen=True)
class SessionRoleContext:
    role_id: str
    role_name: str
    memory_profile_id: str | None


@dataclass(frozen=True)
class AgentSessionDependencies:
    request_llm_plan: Callable[..., tuple[dict, object, StateStore, dict[str, object]]]
    build_memory_injection: Callable[..., object]
    record_memory_profile_use: Callable[..., None]
    confirm_agent_assessment: Callable[..., None]
    enqueue_plan_actions: Callable[..., tuple[list[str], list[str]]]
    drain_queue_items: Callable[..., list[QueueStatus]]
    queue_status_summary: Callable[..., tuple[str, QueueStatus | None]]
    apply_agent_role_payload: Callable[..., None]
    link_selection_traces_to_run: Callable[..., None]
    memory_decision_path: Callable[..., str]


def run_agent_session_start(args: argparse.Namespace) -> None:
    role_context = _resolve_role_context(args.db_path, args.role) if args.role else None
    profile_id = role_context.memory_profile_id if role_context else None
    profile_name = None
    if profile_id:
        profile_name = _resolve_profile_name(args.db_path, profile_id)
    state_store = StateStore(args.db_path)
    try:
        session = state_store.create_agent_session(
            goal=args.goal,
            role_id=role_context.role_id if role_context else None,
            role_name=role_context.role_name if role_context else None,
            profile_id=profile_id,
            profile_name=profile_name,
            max_steps=args.max_steps,
            notes=None,
        )
        _record_session_event(
            state_store=state_store,
            actor="operator",
            operation="start",
            session=session,
            request={
                "goal": session.goal,
                "role": args.role,
                "max_steps": session.max_steps,
            },
            result_meta={"status": "success"},
        )
    finally:
        state_store.close()
    if args.json:
        print(json.dumps(_session_payload(session), ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(f"Created agent session: {session.session_id}")


def run_agent_session_show(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    try:
        session = state_store.get_agent_session(args.session_id)
        if session is None:
            print(f"Agent session not found: {args.session_id}", file=sys.stderr)
            raise SystemExit(2)
    finally:
        state_store.close()
    if args.json:
        print(json.dumps(_session_payload(session), ensure_ascii=False, sort_keys=True, indent=2))
        return
    _print_session_detail(session)


def run_agent_session_list(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    try:
        sessions = state_store.list_agent_sessions()
    finally:
        state_store.close()
    if args.json:
        payload = {
            "schema_version": 1,
            "sessions": [_serialize_session(session) for session in sessions],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if not sessions:
        print("(no sessions)")
        return
    for session in sessions:
        print(_format_session_line(session))


def run_agent_session_pause(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    try:
        session = _require_session(state_store, args.session_id)
        if session.status == AgentSessionStatus.PAUSED:
            print(f"Session already paused: {session.session_id}")
            return
        if session.status in {
            AgentSessionStatus.COMPLETED,
            AgentSessionStatus.CANCELED,
            AgentSessionStatus.FAILED,
        }:
            print(f"Session is {session.status.value}; cannot pause.", file=sys.stderr)
            raise SystemExit(2)
        _confirm_operator_action("Pause agent session?", yes=args.yes, non_interactive=args.non_interactive)
        session = state_store.update_agent_session(
            replace(session, status=AgentSessionStatus.PAUSED)
        )
        _record_session_event(
            state_store=state_store,
            actor="operator",
            operation="pause",
            session=session,
            request={"session_id": session.session_id},
            result_meta={"status": "success"},
        )
    finally:
        state_store.close()
    print(f"Paused agent session: {session.session_id}")


def run_agent_session_cancel(args: argparse.Namespace) -> None:
    state_store = StateStore(args.db_path)
    try:
        session = _require_session(state_store, args.session_id)
        if session.status == AgentSessionStatus.CANCELED:
            print(f"Session already canceled: {session.session_id}")
            return
        if session.status == AgentSessionStatus.COMPLETED:
            print("Session already completed; cannot cancel.", file=sys.stderr)
            raise SystemExit(2)
        _confirm_operator_action("Cancel agent session?", yes=args.yes, non_interactive=args.non_interactive)
        session = state_store.update_agent_session(
            replace(session, status=AgentSessionStatus.CANCELED)
        )
        _record_session_event(
            state_store=state_store,
            actor="operator",
            operation="cancel",
            session=session,
            request={"session_id": session.session_id},
            result_meta={"status": "success"},
        )
    finally:
        state_store.close()
    print(f"Canceled agent session: {session.session_id}")


def run_agent_session_resume(args: argparse.Namespace, deps: AgentSessionDependencies) -> None:
    initial_store = StateStore(args.db_path)
    try:
        session = _require_session(initial_store, args.session_id)
        _ensure_session_resumable(session)
        _validate_profile_snapshot(args.db_path, session)
    finally:
        initial_store.close()

    plan_event_id = str(uuid4())
    memory_injection = None
    if session.profile_id or session.profile_name:
        memory_injection = deps.build_memory_injection(
            args.db_path,
            profile_selector=session.profile_id or session.profile_name,
            plan_id=plan_event_id,
        )
    role_context = None
    if session.role_id and session.role_name:
        role_context = SessionRoleContext(
            role_id=session.role_id,
            role_name=session.role_name,
            memory_profile_id=session.profile_id,
        )

    plan, assessment, state_store, payload = deps.request_llm_plan(
        args.db_path,
        session.goal,
        model=None,
        host=None,
        timeout_s=None,
        enqueue=not args.dry_run,
        dry_run=args.dry_run,
        max_actions=10,
        explain=False,
        debug=False,
        actor="agent_session",
        memory_injection=memory_injection,
        role_context=role_context,
        assessment_policy_path=args.policy,
        record_event=False,
    )
    try:
        deps.record_memory_profile_use(
            db_path=args.db_path,
            memory_injection=memory_injection,
            actor="agent_session",
            related_event_id=plan_event_id,
        )
        next_step = session.step_count + 1
        _apply_session_payload(payload, session, step_count=next_step)
        payload.update(
            {
                "apply_memory_suggestions_requested": False,
                "apply_memory_suggestions_result": {
                    "applied": 0,
                    "skipped": 0,
                    "denied": 0,
                },
                "apply_memory_suggestions_applied": [],
                "apply_memory_policy_path": None,
                "apply_memory_yes": args.yes,
                "apply_memory_non_interactive": args.non_interactive,
                "apply_memory_decision_path": deps.memory_decision_path(
                    yes=args.yes,
                    non_interactive=args.non_interactive,
                ),
            }
        )
        state_store.record_event(
            actor="agent_session",
            event_type=EVENT_TYPE_LLM_PLAN,
            message="LLM plan generated.",
            json_payload=payload,
            event_id=plan_event_id,
        )

        actions = plan.get("actions", [])
        updated_session = replace(
            session,
            last_plan_event_id=plan_event_id,
            step_count=next_step,
            status=AgentSessionStatus.ACTIVE,
        )

        if args.dry_run:
            updated_session = _finalize_session_status(updated_session, actions)
            updated_session = state_store.update_agent_session(updated_session)
            _record_session_event(
                state_store=state_store,
                actor="agent_session",
                operation="resume",
                session=updated_session,
                request={"dry_run": True},
                result_meta={
                    "status": updated_session.status.value,
                    "plan_event_id": plan_event_id,
                    "run_id": None,
                    "queue_status": "dry-run",
                },
            )
            print(f"Dry-run completed for session {session.session_id}")
            return

        if actions and args.non_interactive:
            updated_session = state_store.update_agent_session(
                replace(updated_session, status=AgentSessionStatus.PAUSED)
            )
            _record_session_event(
                state_store=state_store,
                actor="agent_session",
                operation="resume",
                session=updated_session,
                request={"non_interactive": True},
                result_meta={
                    "status": "blocked",
                    "plan_event_id": plan_event_id,
                    "run_id": None,
                    "queue_status": "blocked",
                    "reason": "non_interactive",
                },
            )
            print(
                "Refusing to enqueue in non-interactive mode. Use --yes to override.",
                file=sys.stderr,
            )
            raise SystemExit(2)

        try:
            deps.confirm_agent_assessment(assessment, actions, yes=args.yes)
        except SystemExit:
            updated_session = state_store.update_agent_session(
                replace(updated_session, status=AgentSessionStatus.PAUSED)
            )
            _record_session_event(
                state_store=state_store,
                actor="agent_session",
                operation="resume",
                session=updated_session,
                request={"confirmation": "declined"},
                result_meta={
                    "status": "blocked",
                    "plan_event_id": plan_event_id,
                    "run_id": None,
                    "queue_status": "blocked",
                    "reason": "confirmation_declined",
                },
            )
            raise

        if not actions:
            updated_session = _finalize_session_status(updated_session, actions)
            updated_session = state_store.update_agent_session(updated_session)
            _record_session_event(
                state_store=state_store,
                actor="agent_session",
                operation="resume",
                session=updated_session,
                request={},
                result_meta={
                    "status": updated_session.status.value,
                    "plan_event_id": plan_event_id,
                    "run_id": None,
                    "queue_status": "no-actions",
                },
            )
            print(f"No actions to enqueue for session {session.session_id}")
            return

        run_metadata: dict[str, object] = {
            "goal": session.goal,
            "source": "agent_session",
            "plan_event_id": plan_event_id,
        }
        if role_context:
            deps.apply_agent_role_payload(run_metadata, role_context)
        _apply_session_payload(run_metadata, session, step_count=next_step)
        run = state_store.create_run(
            label="agent-session",
            metadata=run_metadata,
        )
        deps.link_selection_traces_to_run(
            args.db_path,
            plan_id=plan_event_id,
            run_id=run.id,
        )

        enqueued_ids, skipped = deps.enqueue_plan_actions(state_store, plan, run_id=run.id)
        if skipped:
            print("Enqueue notes:")
            for note in skipped:
                print(f"- {note}")
        if not enqueued_ids:
            updated_session = _finalize_session_status(updated_session, actions)
            updated_session = state_store.update_agent_session(updated_session)
            _record_session_event(
                state_store=state_store,
                actor="agent_session",
                operation="resume",
                session=updated_session,
                request={},
                result_meta={
                    "status": updated_session.status.value,
                    "plan_event_id": plan_event_id,
                    "run_id": run.id,
                    "queue_status": "no-actions",
                },
            )
            print("No enqueue actions were generated.")
            return

        statuses = deps.drain_queue_items(args.db_path, args.policy, enqueued_ids)
        status_label, _ = deps.queue_status_summary(statuses)
        if status_label == "succeeded":
            updated_session = replace(updated_session, last_run_id=run.id)
            updated_session = _finalize_session_status(updated_session, actions)
        elif status_label == "failed":
            updated_session = replace(
                updated_session,
                last_run_id=run.id,
                status=AgentSessionStatus.FAILED,
            )
        else:
            updated_session = replace(
                updated_session,
                last_run_id=run.id,
                status=AgentSessionStatus.PAUSED,
            )
        updated_session = state_store.update_agent_session(updated_session)
        _record_session_event(
            state_store=state_store,
            actor="agent_session",
            operation="resume",
            session=updated_session,
            request={},
            result_meta={
                "status": updated_session.status.value,
                "plan_event_id": plan_event_id,
                "run_id": run.id,
                "queue_status": status_label,
            },
        )
        print(
            "Session resume completed: "
            f"status={updated_session.status.value} "
            f"run_id={run.id} queue_status={status_label}"
        )
    finally:
        state_store.close()


def _apply_session_payload(
    payload: dict[str, object],
    session: AgentSession,
    *,
    step_count: int,
) -> None:
    payload["agent_session"] = {
        "session_id": session.session_id,
        "role_id": session.role_id,
        "role_name": session.role_name,
        "profile_id": session.profile_id,
        "profile_name": session.profile_name,
        "goal": session.goal,
        "step_count": step_count,
        "max_steps": session.max_steps,
        "status": session.status.value,
    }


def _finalize_session_status(
    session: AgentSession,
    actions: list[dict[str, object]],
) -> AgentSession:
    status = session.status
    if not actions:
        status = AgentSessionStatus.COMPLETED
    if session.step_count >= session.max_steps:
        status = AgentSessionStatus.COMPLETED
    return replace(session, status=status)


def _record_session_event(
    *,
    state_store: StateStore,
    actor: str,
    operation: str,
    session: AgentSession,
    request: dict[str, object],
    result_meta: dict[str, object],
) -> None:
    payload = {
        "operation": operation,
        "session_id": session.session_id,
        "goal": session.goal,
        "role_name": session.role_name,
        "profile_name": session.profile_name,
        "step_count": session.step_count,
        "max_steps": session.max_steps,
        "request": request,
        "result_meta": result_meta,
    }
    state_store.record_event(
        actor=actor,
        event_type="agent_session",
        message=f"Agent session {operation}.",
        json_payload=payload,
    )


def _ensure_session_resumable(session: AgentSession) -> None:
    if session.status in {
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.CANCELED,
        AgentSessionStatus.FAILED,
    }:
        print(f"Session is {session.status.value}; cannot resume.", file=sys.stderr)
        raise SystemExit(2)
    if session.step_count >= session.max_steps:
        print("Session max steps reached; cannot resume.", file=sys.stderr)
        raise SystemExit(2)


def _validate_profile_snapshot(db_path: str, session: AgentSession) -> None:
    selector = session.profile_id or session.profile_name
    if not selector:
        return
    profile = memory_get_profile_by_selector(db_path, selector)
    if profile is None:
        print(f"Session profile missing: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if profile.retired_at:
        print(f"Session profile is retired: {profile.name}", file=sys.stderr)
        raise SystemExit(2)


def _resolve_profile_name(db_path: str, selector: str) -> str:
    profile = memory_get_profile_by_selector(db_path, selector)
    if profile is None:
        print(f"Memory profile not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if profile.retired_at:
        print(f"Memory profile is retired: {profile.name}", file=sys.stderr)
        raise SystemExit(2)
    return profile.name


def _resolve_role_context(db_path: str, selector: str) -> SessionRoleContext:
    state_store = StateStore(db_path)
    try:
        role = state_store.get_agent_role_by_selector(selector)
    finally:
        state_store.close()
    if role is None:
        print(f"Agent role not found: {selector}", file=sys.stderr)
        raise SystemExit(2)
    if role.retired_at:
        print(f"Agent role is retired: {role.name}", file=sys.stderr)
        raise SystemExit(2)
    if role.memory_profile_id:
        _resolve_profile_name(db_path, role.memory_profile_id)
    return SessionRoleContext(
        role_id=role.role_id,
        role_name=role.name,
        memory_profile_id=role.memory_profile_id,
    )


def _confirm_operator_action(prompt: str, *, yes: bool, non_interactive: bool) -> None:
    if yes:
        return
    if non_interactive or not _is_interactive_tty():
        print("Confirmation required. Use --yes in non-interactive mode.", file=sys.stderr)
        raise SystemExit(2)
    response = input(f"{prompt} [y/N]:")
    if response.strip().lower() not in {"y", "yes"}:
        print("Confirmation declined.", file=sys.stderr)
        raise SystemExit(2)


def _require_session(state_store: StateStore, session_id: str) -> AgentSession:
    session = state_store.get_agent_session(session_id)
    if session is None:
        print(f"Agent session not found: {session_id}", file=sys.stderr)
        raise SystemExit(2)
    return session


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _session_payload(session: AgentSession) -> dict[str, object]:
    return {
        "schema_version": 1,
        "session": _serialize_session(session),
    }


def _serialize_session(session: AgentSession) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "role_id": session.role_id,
        "role_name": session.role_name,
        "profile_id": session.profile_id,
        "profile_name": session.profile_name,
        "goal": session.goal,
        "status": session.status.value,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "last_plan_event_id": session.last_plan_event_id,
        "last_run_id": session.last_run_id,
        "step_count": session.step_count,
        "max_steps": session.max_steps,
        "notes": session.notes,
    }


def _format_session_line(session: AgentSession) -> str:
    role = session.role_name or "-"
    profile = session.profile_name or "-"
    goal = _truncate(session.goal, 60)
    return (
        f"- {session.session_id} status={session.status.value} "
        f"steps={session.step_count}/{session.max_steps} "
        f"role={role} profile={profile} goal={goal}"
    )


def _print_session_detail(session: AgentSession) -> None:
    print("=== GISMO Agent Session ===")
    print(f"Session ID:    {session.session_id}")
    print(f"Status:        {session.status.value}")
    print(f"Goal:          {session.goal}")
    print(f"Role:          {session.role_name or '-'}")
    print(f"Profile:       {session.profile_name or '-'}")
    print(f"Steps:         {session.step_count}/{session.max_steps}")
    print(f"Last plan:     {session.last_plan_event_id or '-'}")
    print(f"Last run:      {session.last_run_id or '-'}")
    print(f"Created:       {session.created_at.isoformat()}")
    print(f"Updated:       {session.updated_at.isoformat()}")
    if session.notes:
        print(f"Notes:         {session.notes}")


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 1)] + "…"
