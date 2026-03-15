# Handoff — GISMO

This document is for maintainers and future contributors. It describes what GISMO is, how it is structured, what is stable, what is intentionally constrained, and what the next engineering work should focus on.

-------------------------------------------------------------------------------

PROJECT IDENTITY

GISMO (General Intelligent System for Multiflow Operations)

A local-first, operator-grade orchestration core that plans, schedules, executes, audits, and recovers actions on a user’s machine using a controlled local LLM.

Not a chatbot.
Not a toy.
Not cloud-dependent.

Core ethos:
- Deterministic state
- Policy before power
- Explicit > implicit
- No magic, no silent failures
- CLI must be self-diagnosing
- Windows behavior is the source of truth

-------------------------------------------------------------------------------

CURRENT SYSTEM STATUS

Overall: Foundation, planner, guardrails, and memory complete. Phase 4 interactive features substantially done.

Completed:
- Phase 0 (Foundation): DONE
- Phase 1 (Local LLM Planner): DONE
- Phase 2 (Control & Guardrails): DONE
- Phase 3 (Memory & Context): DONE
- Phase 4 (Interactive GISMO): IN PROGRESS — TUI, web UI, TTS, and plan approval complete

-------------------------------------------------------------------------------

WHAT IS “DONE” (NON-NEGOTIABLE BASELINE)

Core persistence + execution:
- SQLite state store (.gismo/state.db by default)
- Durable queue and daemon execution loop
- Deterministic, restart-safe orchestration
- Retained FAILED items for auditability (intentional)

CLI and operator UX:
- Canonical invocation: gismo ... (fallback: python -m gismo.cli.main ...)
- CLI entrypoint supports: run, enqueue, daemon, export, runs introspection, queue introspection
- Queue introspection complete:
  - queue stats
  - queue list
  - queue show ID_OR_PREFIX
  - short-id prefix resolution with ambiguity detection
- Queue item IDs are distinct from run IDs; runs are inspected via runs show or export.

Policy & safety:
- Policy enforcement layer is active and audited
- Policies supported: readonly, dev-safe
- shell: is blocked unless allowlisted
- No blanket shell=True behavior without guardrails

Observability:
- JSONL audit exports per run (and related task/tool granularity)
- Export defaults are DB-anchored (not dependent on CWD)
- Extensive tests
- scripts/verify.py is the single validation entrypoint

Windows-first:
- Windows-native paths and behavior
- No Unix assumptions
- Verified operation on Windows and Codespaces

-------------------------------------------------------------------------------

HIGH-LEVEL ARCHITECTURE (MENTAL MODEL)

GISMO is a state-driven orchestration system.

- State is authoritative (SQLite).
- Everything that happens is written to state and/or audit logs.
- The daemon does not “invent” work: it pulls queue items from state and executes them.
- The planner does not “execute”: it produces enqueue-only plans that the core validates, logs, and enqueues.
- Policy is the safety boundary: tools must consult policy before doing anything with side effects.

-------------------------------------------------------------------------------

REPO LAYOUT (INTENT)

gismo/
  cli/
    main.py              CLI entrypoint and argparse wiring
    plan.py              plan approval CLI (list/show/approve/reject/edit)
    tts_cli.py           TTS CLI (voices list/set/download, speak)
    ...                  command groups (queue, export, supervise, ipc, etc.)
  core/
    state.py             SQLite StateStore + schema and persistence
    orchestrator.py      run execution + tool dispatch
    daemon.py            durable execution loop
    export.py            export helpers and defaults
    paths.py             canonical path resolution, DB-anchored helpers
    plan_store.py        shared enqueue_plan_actions() helper
    models.py            dataclasses + enums incl. PendingPlan/PlanStatus
    ...
  tools/
    ...                  tool implementations (echo/note/shell/etc.)
  llm/
    ...                  local planner integration (Ollama client, prompts, normalization)
  tts/
    voices.py            voice registry, model cache (~/.cache/gismo/tts/)
    engine.py            synthesis (piper-tts), preprocessing, playback
    prefs.py             memory-backed voice preference (gismo:settings/tts.voice)
  web/
    api.py               pure data layer (no HTTP); all JSON-serialisable functions
    server.py            stdlib HTTP router (zero external deps)
    templates.py         single-file embedded HTML/CSS/JS dashboard

policy/
  dev-safe.json
  dev-operator.json
  readonly.json

docs/
  OPERATOR.md            operator guide

tests/
  test_*                 pytest suite; tests are the contract

scripts/
  verify.py              run all checks (the gatekeeper)

-------------------------------------------------------------------------------

AUTHORITY MODEL (SAFETY BOUNDARY)

Human operator:
- Defines policy (what is allowed)
- Provides intent/goals (ask/agent)
- Starts/stops daemon/supervisor

Planner (LLM):
- Proposes plan only (enqueue-only)
- Must output strict plan schema
- Actions are bounded; normalization/coercion applied
- Cannot bypass policy or execute directly

Core orchestrator + tools:
- Executes only what is enqueued
- Validates inputs
- Enforces policy at runtime
- Audits everything

No component is allowed to do work outside of this chain.

-------------------------------------------------------------------------------

INTENTIONAL LIMITATIONS (NOT BUGS)

- shell: commands are blocked unless explicitly allowlisted by policy
- FAILED queue items are retained for auditability
- No remote interface unless explicitly enabled/installed (CLI-first)
- No policy expansion without explicit reason and tests
- No feature work that undermines determinism, auditability, or Windows correctness

-------------------------------------------------------------------------------

RECENT NOTABLE CHANGES (PHASE 4)

Phase 4 delivered four major capabilities, all behind the standard CLI and web surfaces:

1. Terminal dashboard (TUI)
   - `gismo tui`: live curses dashboard with queue, runs, daemon status; 3s auto-refresh.
   - Pure stdlib, no external TUI library.

2. Local web dashboard
   - `gismo web [--port N] [--no-browser]`: opens browser to 127.0.0.1:7800.
   - Tabs: Queue (cancel/purge), Runs (task/tool detail), Memory (namespaces + items), Plans, Settings (TTS).
   - Daemon sidebar: live status, pause/resume controls.
   - Zero external HTTP dependencies: stdlib http.server only.
   - Single-file embedded HTML/JS/CSS in gismo/web/templates.py.
   - REST endpoints: GET/POST/PATCH in gismo/web/server.py; pure data layer in gismo/web/api.py.

3. TTS voice support
   - `gismo tts voices list/set/download`, `gismo tts speak`.
   - Backend: piper-tts; models download on first use to ~/.cache/gismo/tts/.
   - 5 voices: en_GB-northern_english_male-medium (default), en_GB-alan-medium,
     en_US-lessac-medium, en_US-ryan-high, en_US-amy-medium.
   - Preprocessing: GISMO → GHIZMO (word-boundary regex) for hard-G pronunciation.
   - Voice preference stored in memory (namespace gismo:settings, key tts.voice).
   - Voice selection and test playback available in web Settings tab.

4. Interactive plan approval
   - `gismo ask --defer`: saves LLM plan as a PendingPlan (status=PENDING) instead of enqueueing.
   - `gismo plan list/show/approve/reject/edit`: full CLI lifecycle.
     - approve: enqueues plan actions via shared enqueue_plan_actions(), marks APPROVED.
     - reject: records optional reason, marks REJECTED.
     - edit: per-action command replacement or removal (PENDING only, 1-based index).
   - Web UI Plans tab: table view + inline editing (editable action inputs, remove buttons).
   - Data model: PendingPlan dataclass + PlanStatus enum in models.py; pending_plans SQLite table
     in state.py with full CRUD + prefix resolution.
   - Shared enqueue logic in core/plan_store.py (avoids circular imports between web and CLI).
   - 27 tests: TestPendingPlanStateStore, TestEnqueuePlanActions, TestWebApiPlans.

Operator smoke scripts + handle guardrail (pre-Phase 4):
- Windows-friendly smoke scripts: operator_smoke.ps1 and e2e_smoke.ps1.
- CLI subprocess regression test: daemon --once with DB handle release assertion.

Next steps:
- Always-on local service behavior (Phase 4 remainder).
- Run operator_smoke.ps1 and e2e_smoke.ps1 on Windows after CLI changes.
- Validate ask/agent subprocess flows on Windows for handle hygiene at higher concurrency.

Tests run:
- python -m pytest (330 passed, 9 pre-existing Windows tempfile teardown failures unrelated to Phase 4)

-------------------------------------------------------------------------------

LEASHED AGENT LOOP (CONTROLLED AUTONOMY)

Agent behavior:
- The `agent` CLI turns a goal into a plan, enqueues it, and executes via the daemon.
- Confirmation gates apply to higher-risk plans and any write/shell actions unless overridden with --yes.
- Agent summaries report confidence/risk flags, run IDs, and final status.

Memory behavior:
- Agent memory handling mirrors `ask`:
  - read-only context injection (bounded, audited)
  - memory_suggestions are advisory by default
  - applying suggestions requires explicit flag + policy + confirmation

This is guarded behavior. Treat changes here as security-sensitive.

-------------------------------------------------------------------------------

OPERATING RULES (ENFORCE THESE)

- Tests are the contract.
- Windows behavior is not optional.
- CLI must be self-diagnosing.
- If SQLite locks, it’s a bug.
- If behavior is unclear, improve errors/logging before docs.
- No weakening of safety guarantees for convenience.

-------------------------------------------------------------------------------

DEFINITION OF DONE (PHASE 4)

Phase 4 is complete when:
- Terminal dashboard (TUI) is stable and reflects live state
- Web dashboard covers queue/runs/memory/plans with action controls
- TTS synthesis and voice selection work end-to-end without cloud deps
- Interactive plan approval covers full CLI + web UI lifecycle
- Always-on local service behavior is documented and testable
- No regressions in queue/daemon/policy/memory
- Tests pass on Windows reliably

Currently done: TUI, web UI, TTS, plan approval.
Remaining: always-on service hardening.

-------------------------------------------------------------------------------

NEXT ENGINEERING TARGET (RECOMMENDED)

Phase 4 remainder:
- Always-on local service behavior: auto-start on login, service-style lifecycle.
- Windows Task Scheduler / launchd integration.

After Phase 4:
- Evaluate operator feedback on plan approval UX (approval rate, edit patterns).
- Harden Windows handle hygiene at higher concurrency (agent loops + web server + daemon).
- Consider notification hooks (e.g. desktop toast on plan ready for approval).

-------------------------------------------------------------------------------

RELEASE READINESS (WHAT “A NEW RELEASE” MEANS)

A release should ship only when:
- scripts/verify.py passes
- docs reflect actual behavior
- export paths are deterministic
- CLI usage examples are accurate
- guardrails are explicit and auditable

If something is not stable, document it as experimental and keep it behind flags.

-------------------------------------------------------------------------------

MAINTAINER NOTES

Keep the system boring.
If you are tempted to add power, add policy controls and audits first.
Prefer explicitness over cleverness.
Never trade determinism for convenience.
