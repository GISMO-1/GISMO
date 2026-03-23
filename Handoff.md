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
- Phase 4 (Interactive GISMO): DONE — TUI, web UI, TTS, plan approval, Windows Task Scheduler service

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

RECENT NOTABLE CHANGES

Phase 5 -- Zero-Trust Execution (landed in 700ea9f):
- Signed capability tokens (HMAC-SHA256) for every tool execution
- Deny-by-default policy gating on all tool calls
- Full audit trail via security_events table (capability_issued, capability_verified, capability_rejected)
- Capability claims: subject, action, resource, constraints, TTL, run/task/plan scoping
- Input-hash binding: capability tokens are bound to exact tool input payloads
- Task creation now requires input_json to have {"tool": "...", "payload": {...}} structure
- Token verification at orchestrator execution time with strict resource/input matching

Phase 5b -- Device Adapter Architecture:
- Pluggable adapter layer in gismo/core/device_adapters/ with abstract DeviceAdapter base class
- AdapterRegistry singleton maps adapter names and device types to implementations
- TuyaAdapter: Feit/Tuya local LAN bulb control via tinytuya
- Tuya credentials now load from a sidecar config resolved from `.gismo/devices.json`
- Shared control path generalized to `device_command` with `kasa_command` kept as a compatibility alias
- Shared adapter/runtime lookup now uses `device_ref` for the incoming lookup token; vendor `device_id` remains the canonical protocol identity when resolved
- Saved-device `turn_on` / `turn_off` now route through adapter lookup instead of calling tinytuya directly in shared runtime code
- KasaAdapter remains available for TP-Link Kasa devices
- Security events emitted at command send/succeed/fail with full audit payload
- All execution goes through existing device_adapter trust zone with sandboxed boundary
- tinytuya remains in dependencies for Tuya/Feit support; python-kasa remains for Kasa support
- See docs/DEVICE_ADAPTERS.md for architecture, pipeline, and how to add new adapters

What just became real:
- First successful local physical device control via Tuya adapter: a real Feit Electric / Tuya smart bulb was controlled over the local LAN with live `get_state`, `turn_off`, and `turn_on` smoke tests.
- The known-good path for that bulb is `.gismo/devices.json` -> `TuyaAdapter` / device runtime -> tinytuya local LAN control.
- Shared routing now uses `device_ref` as the lookup token; resolved vendor `device_id` remains the protocol identity and IP stays a locator/fallback.
- Kasa remains in the repo as a secondary compatibility path, not the primary live device story.

What is wired versus what is live-verified:
- Wired now: `device_control`, sandboxed device runtime, adapter registry, Tuya adapter, Kasa compatibility, web chat/device parsing for scan/list/check/simple on-off requests.
- Live-verified now: direct Tuya adapter/runtime smoke for `get_state`, `turn_off`, and `turn_on` on one real Feit/Tuya bulb.
- Not yet broadly live-verified: brightness, color temperature, and RGB on the physical bulb through the broader orchestration path; polished end-to-end chat/device UX on the physical bulb.

Phase 6 -- Operator Readiness Surfaces (in progress):
- readiness.py: unified runtime status builder with state machine
  (ready, starting, degraded, approval_needed, blocked, offline)
- Readiness stages: state, worker, setup, model, API
- Model health probing (cached and full modes)
- Queue health monitoring with stale-running detection
- get_status API returns offline when no daemon is running

Phase 4 completion -- Windows Task Scheduler service (always-on daemon):
- `gismo service install` / `uninstall` / `status` CLI commands
- Policy-gated (service.install / service.uninstall in allowed_tools, deny-by-default)
- Audit trail: service_install and service_uninstall security events with full payload
- Task Scheduler ONLOGON trigger, pythonw.exe for no console window
- Refuses to overwrite existing task (must uninstall first)
- Status shows both Task Scheduler state and daemon heartbeat

Phase 4 highlights (completed earlier):
- TUI, web dashboard, TTS (kokoro primary + piper fallback, 16 voices), plan approval
- Deterministic chat routing for calendar/device queries
- Model policy with fallback chains
- Onboarding system, plugin runtime

Device-path validation (2026-03-23): 50 targeted tests passed, 0 failed

Immediate next device work:
- Re-run the real bulb through the normal saved-device orchestration path, not just the direct adapter/runtime smoke.
- Live-test brightness, color temperature, and RGB on the physical bulb.
- Tighten the operator/chat path so the real adapter capability is easier to use without manual setup steps.
- Add a first-party way to create or edit `.gismo/devices.json`.

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

Currently done: TUI, web UI, TTS, plan approval, Windows Task Scheduler service.
Phase 4 is complete.

-------------------------------------------------------------------------------

NEXT ENGINEERING TARGET (RECOMMENDED)

Phase 6 completion:
- Fix readiness.py starting state persistence after daemon crash.
- Model status defaults to unknown which allows ready state even when model is unreachable.

After Phase 6:
- Add a first-party way to edit `.gismo/devices.json` from GISMO instead of manual file edits.
- Teach planner/device routing to express brightness, color temperature, and RGB actions in natural language.
- Improve known-device reconciliation if Tuya IPs change and only the cloud-derived vendor device ID is stable.
- Harden Windows handle hygiene at higher concurrency (agent loops + web server + daemon).
- Consider notification hooks (e.g. desktop toast on plan ready for approval).
- Evaluate operator feedback on plan approval UX (approval rate, edit patterns).

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
