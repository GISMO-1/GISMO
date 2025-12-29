# GISMO

GISMO (General Intelligent System for Multiflow Operations) is a local-first, operator-grade autonomous orchestration system. It can plan, schedule, execute, audit, and recover tasks on a single machine using a controlled local LLM (Large Language Model) for planning. GISMO is not a chatbot or a cloud service – it runs entirely on your hardware and is built for determinism, safety, and clarity.

Key Features and Principles:

- Fully Local & Deterministic:
  GISMO operates with no external dependencies or cloud APIs. All decisions and state are local. Execution is deterministic and repeatable, with a persistent SQLite state database (.gismo/state.db by default) recording runs and tasks.

- Operator Commands & Structured Tasks:
  GISMO accepts explicit operator commands (like echo:, note:, graph:) that define actions. High-level goals can be decomposed by the planner into sequences of these commands, but only within strict safety bounds.

- Queued Orchestration Engine:
  Tasks are enqueued and executed via a durable queue and daemon process, ensuring reliable, resume-safe operation. Each task transitions through clear states (QUEUED → IN_PROGRESS → SUCCEEDED/FAILED), with retry handling and failure retention for auditability.

- Policy-Enforced Tooling:
  All operations are gated by security policies. Policies supported include readonly and dev-safe modes. Disallowed actions (like unapproved shell: commands) are blocked by default, logged, and safely marked failed.

- Auditability:
  Every action and decision is logged. GISMO produces detailed JSONL audit logs per run, capturing tool inputs/outputs and outcomes. Nothing happens silently.

- Cross-Platform, Windows-First:
  GISMO is built to run reliably on Windows first, as well as Linux. It avoids Unix assumptions and handles Windows-specific concerns (paths, subprocess behavior, locking) explicitly.

- No Magic, No Surprises:
  Policy before power. Explicit > implicit. No silent actions. No hidden behavior.

-------------------------------------------------------------------------------

INSTALLATION

Prereqs:
- Python 3.11+ recommended
- A virtual environment is strongly recommended
- Optional (for planner features): Ollama running locally with an available model

Install (from repo root):

1) Create venv
   Windows (PowerShell):
     python -m venv .venv
     .\.venv\Scripts\Activate.ps1

   Linux/macOS:
     python -m venv .venv
     source .venv/bin/activate

2) Editable install
     pip install -e .

3) Verify
     python scripts/verify.py

-------------------------------------------------------------------------------

CANONICAL INVOCATION

Prefer:
  gismo ...

Fallback (no console script available):
  python -m gismo.cli.main ...

-------------------------------------------------------------------------------

STATE & DEFAULT PATHS

- Default DB path: .gismo/state.db
- Exports are DB-anchored:
  By default, exports are written to an `exports/` directory located next to the DB file.
  Example:
    DB:      .gismo/state.db
    Exports: .gismo/exports/

This behavior is intentional: exports must not depend on the current working directory.

-------------------------------------------------------------------------------

CORE COMMANDS (CLI)

Run a single operator command immediately:

  gismo run "echo:Hello from GISMO"

Enqueue a command to be executed by the daemon later:

  gismo enqueue "note:remember this"

Start the daemon loop (foreground):

  gismo daemon

Queue introspection:

  gismo queue stats
  gismo queue list
  gismo queue show ID_OR_PREFIX

Notes:
- queue show supports short-id prefix resolution (with ambiguity detection).
- Human-readable output is available; JSON output is available where requested.

Export audit logs:

  gismo export --latest
  gismo export --run RUN_ID
  gismo export --all

Planner (local LLM via Ollama):

  gismo ask "Summarize the last 10 queue failures" --dry-run
  gismo ask "Do X safely" --enqueue

Planner behavior:
- Produces enqueue-only plans under strict schema.
- Actions are bounded (hard limit on action count).
- Normalization/coercion exists so malformed model output does not break the system.
- Ollama is called in JSON mode and uses keep_alive to avoid repeated model reloads.
- Full audit trail is recorded for planner outputs and execution.
- Every plan includes a confidence assessment, risk flags, and a short explanation.
- Higher-risk plans require confirmation before enqueueing unless --yes is used.
- --explain prints additional assessment details.
- Use --debug to print tracebacks for ask failures.

Planner configuration:
- Increase --timeout-s on CPU machines (60s baseline) if prompts time out.
- Environment overrides:
  - GISMO_LLM_MODEL or GISMO_OLLAMA_MODEL (model name)
  - GISMO_LLM_TIMEOUT_S or GISMO_OLLAMA_TIMEOUT_S (LLM timeout)
  - GISMO_OLLAMA_URL or OLLAMA_HOST (Ollama endpoint)
- keep_alive defaults to 10m so models stay loaded for smoother repeated calls.

-------------------------------------------------------------------------------

SUPERVISION & IPC (WINDOWS-FIRST)

GISMO includes a supervisor lifecycle for running as a local service-like system:

  gismo up
  gismo status
  gismo down
  gismo recover

These coordinate:
- The daemon process
- The IPC server (Windows named pipes) with token auth

Maintenance loop (stale recovery):

  gismo maintain --once --stale-minutes 0
  gismo maintain --interval-seconds 30 --stale-minutes 10

-------------------------------------------------------------------------------

POLICY & SAFETY MODEL

- Deny by default.
- Tools are permission-gated.
- shell: is blocked unless explicitly allowlisted by policy.
- Permission failures are safe, auditable, and do not partially execute actions.

Supported policies include:
- readonly.json
- dev-safe.json

-------------------------------------------------------------------------------

WHAT GISMO IS (AND IS NOT)

GISMO IS:
- A local-first orchestration core
- Deterministic and stateful via SQLite
- CLI-first, inspectable, and auditable
- Policy-controlled and safe by default

GISMO IS NOT:
- A chatbot
- A cloud agent framework
- A networking/remote admin tool (unless you explicitly build/enable that interface)
- A system that silently does things behind your back

-------------------------------------------------------------------------------

CURRENT STATUS / ROADMAP SNAPSHOT

Phase 0 — Foundation: COMPLETE
- SQLite state store with WAL, retries, backoff, cancellation
- Durable queue + daemon execution engine
- IPC server (Windows named pipes) with token auth
- Supervisor lifecycle (up/down/status/recover)
- Maintenance loop (stale recovery)
- Exportable audit logs (jsonl, run/task/tool granularity)
- Idempotent, test-covered CLI
- Windows-native support

Phase 1 — Local LLM Planner: COMPLETE
- Ollama integration
- Model-agnostic config via env + CLI
- ask pipeline (dry-run, enqueue)
- Strict plan schema (enqueue-only, bounded actions)
- Normalization/coercion for model mistakes
- Timeout handling + failure auditing
- Test coverage for planner behavior

Phase 2 — Control & Guardrails: IN PROGRESS
- Hard limits on action count
- Enqueue-only execution model
- Explicit tool allowlists
- Read-only / dev-safe policy modes
- Full audit trail for every decision

Next:
- Planner confidence scoring (low/medium/high)
- User confirmation gates for higher-risk plans
- Policy-aware planning prompts
- Explain-before-execute mode

Phase 3 — Memory & Context: PLANNED
- Persistent memory layer (prior plans, failures, preferences)
- Context injection and summarization jobs
- Session awareness across runs

Phase 4 — Interactive GISMO: END GAME
- CLI/TUI/Local UI interactions
- Always-on local service behavior
- Plans, explains, executes, remembers, recovers
- No cloud dependency and no silent actions

-------------------------------------------------------------------------------

DOCUMENTATION

- docs/OPERATOR.md  : operator usage and lifecycle guidance
- Handoff.md        : maintainer handoff and architecture overview

-------------------------------------------------------------------------------

LICENSE

TBD (project currently in active development; choose an OSS license before first formal release tag).
