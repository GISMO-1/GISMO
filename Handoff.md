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

Overall: Stable foundation complete, local planner complete, guardrails in progress.

Completed:
- Phase 0 (Foundation): DONE
- Phase 1 (Local LLM Planner): DONE

In progress:
- Phase 2 (Control & Guardrails): ~75%

Planned:
- Phase 3 (Memory & Context)
- Phase 4 (Interactive GISMO)

-------------------------------------------------------------------------------

WHAT IS “DONE” (NON-NEGOTIABLE BASELINE)

Core persistence + execution:
- SQLite state store (.gismo/state.db by default)
- Durable queue and daemon execution loop
- Deterministic, restart-safe orchestration
- Retained FAILED items for auditability (intentional)

CLI and operator UX:
- Canonical invocation: gismo ... (fallback: python -m gismo.cli.main ...)
- CLI entrypoint supports: run, enqueue, daemon, export, queue introspection
- Queue introspection complete:
  - queue stats
  - queue list
  - queue show ID_OR_PREFIX
  - short-id prefix resolution with ambiguity detection

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
    ...                  command groups (queue, export, supervise, ipc, etc.)
  core/
    state.py             SQLite StateStore + schema and persistence
    orchestrator.py      run execution + tool dispatch
    daemon.py            durable execution loop
    export.py            export helpers and defaults
    paths.py             canonical path resolution, DB-anchored helpers
    ...
  tools/
    ...                  tool implementations (echo/note/shell/etc.)
  llm/
    ...                  local planner integration (Ollama client, prompts, normalization)

policy/
  dev-safe.json
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
- defines policy (what is allowed)
- provides intent/goals (ask)
- starts/stops daemon/supervisor

Planner (LLM):
- proposes plan only
- must output enqueue-only plan schema
- bounded actions; normalization/coercion applied
- cannot bypass policy or execute directly

Core orchestrator + tools:
- executes only what is enqueued
- validates inputs
- enforces policy at runtime
- audits everything

No component is allowed to “silently” do work outside of the above chain.

-------------------------------------------------------------------------------

INTENTIONAL LIMITATIONS (NOT BUGS)

- shell: commands are blocked unless explicitly allowlisted by policy
- FAILED queue items are retained for auditability
- No remote interface unless explicitly enabled/installed (CLI-first)
- No policy expansion without explicit reason and tests
- No feature work that undermines determinism, auditability, or Windows correctness

-------------------------------------------------------------------------------

RECENT NOTABLE CHANGE (LATEST WORK)

DB-anchored exports:
- Export defaults now resolve absolute paths anchored to the selected DB location.
- This prevents CWD-dependent exports (operator-grade determinism).
- Coverage added: CLI export defaults tested from non-repo working directory.

This is a structural improvement and should be treated as stable behavior.

Plan assessment gate:
- ask now prints confidence, risk flags, and an explanation for each plan.
- High-risk plans require confirmation before enqueueing unless overridden with --yes.

LATEST UPDATE (OPERATOR NOTES)

Status:
- Ask CLI now accepts options after the prompt so `--timeout-s` drives the LLM banner and request timeout.

Next steps:
- Make planner prompts policy-aware (still pending).
- Monitor operator feedback on ask timeout UX.
- Confirm Windows operator docs emphasize the console entrypoint when installed.

Tests run:
- python scripts/verify.py

-------------------------------------------------------------------------------

OPERATING RULES (ENFORCE THESE)

- Tests are the contract.
- Windows behavior is not optional.
- CLI must be self-diagnosing.
- If SQLite locks, it’s a bug.
- If behavior is unclear, improve errors/logging before docs.
- No weakening of safety guarantees for convenience.

-------------------------------------------------------------------------------

DEFINITION OF DONE (PHASE 2)

Phase 2 is complete when:
- Planner confidence scoring exists (low/medium/high)
- User confirmation gates exist for higher-risk plans
- Planner prompts are policy-aware (LLM is told its constraints)
- Explain-before-execute mode exists (human-legible summary)
- LLM runtime behavior is stable (predictable timeouts, no hangs)
- No regressions in queue/daemon/policy
- pytest passes on Windows reliably

Only after Phase 2 is “boringly reliable” do we move into Phase 3 memory.

-------------------------------------------------------------------------------

NEXT ENGINEERING TARGET (RECOMMENDED)

Do not add “new features” yet.

Priority sequence:
1) Stabilize planner runtime:
   - make timeouts predictable
   - cap token/context behavior
   - lock in default fast model profile (phi3:mini or equivalent)

2) Add one confidence + confirmation gate:
   - simple, auditable, CLI-controlled
   - no UI creep
   - default safe behavior

3) Policy-aware prompts:
   - planner should be told what tools/ops are allowed
   - reduce invalid plans upstream

4) Explain-before-execute mode:
   - summary of plan intent and risk
   - operator can approve/deny

-------------------------------------------------------------------------------

RELEASE READINESS (WHAT “A NEW RELEASE” MEANS)

A release should ship only when:
- verify.py passes
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
