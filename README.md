# GISMO

GISMO (General Intelligent System for Multiflow Operations) is a local-first, operator-grade autonomous orchestration system. It can plan, schedule, execute, audit, and recover tasks on a single machine using a controlled local LLM (Large Language Model) for planning. GISMO is not a chatbot or a cloud service — it runs entirely on your hardware and is built for determinism, safety, and clarity.

Key Features and Principles:

- Fully Local & Deterministic:
  GISMO operates with no external dependencies or cloud APIs. All decisions and state are local. Execution is deterministic and repeatable, with a persistent SQLite state database (.gismo/state.db by default) recording runs and tasks.

- Operator Commands & Structured Tasks:
  GISMO accepts explicit operator commands (like echo:, note:, shell:, graph:) that define actions. High-level goals can be decomposed by the planner into sequences of these commands, but only within strict safety bounds.

- Queued Orchestration Engine:
  Tasks are enqueued and executed via a durable queue and daemon process, ensuring reliable, resume-safe operation. Each task transitions through clear states (QUEUED → IN_PROGRESS → SUCCEEDED/FAILED), with retry handling and failure retention for auditability.

- Policy-Enforced Tooling:
  All operations are gated by security policies. Policies supported include readonly and dev-safe modes. Disallowed actions (like unapproved shell: commands) are blocked by default, logged, and safely marked failed.

- Auditability:
  Every action and decision is logged. GISMO produces detailed JSONL audit logs per run, capturing tool inputs/outputs, tool receipts (canonical payloads + hashes), and outcomes. Nothing happens silently.

- Memory & Context (Persistent, Policy-Gated):
  GISMO includes a persistent local memory layer (SQLite-backed) for facts, preferences, notes, and retention-governed context. The planner and agent can inject read-only memory context into prompts (bounded and audited). The LLM may emit “memory suggestions”; these are advisory by default and are only written if explicitly applied (policy + confirmation gated).

- Leashed Autonomy (Agent Loop):
  GISMO includes an operator-leashed agent loop that can iterate toward a goal under strict guardrails. The agent plans, enqueues, and executes only through the same queue/daemon machinery, with the same confirmation gates and policy checks as ask.

- Cross-Platform, Windows-First:
  GISMO is built to run reliably on Windows first, as well as Linux. It avoids Unix assumptions and handles Windows-specific concerns (paths, subprocess behavior, locking) explicitly.

- No Magic, No Surprises:
  Policy before power. Explicit > implicit. No silent actions. No hidden behavior.

-------------------------------------------------------------------------------

INSTALLATION

Prereqs:
- Python 3.11+ recommended (tested on Python 3.13)
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
- The `--db` flag can appear before or after the subcommand (e.g., `gismo --db PATH queue stats` or `gismo queue stats --db PATH`).
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

Shell commands require explicit policy allowance and exact allowlist matches:

  gismo run "shell:echo hello"

- Shell commands are deny-by-default.
- Policies must include run_shell in allowed_tools and a matching shell.allowlist entry.

Start the daemon loop (foreground):

  gismo daemon

Queue introspection:

  gismo queue stats
  gismo queue list
  gismo queue show ID_OR_PREFIX

Notes:
- queue show supports short-id prefix resolution (with ambiguity detection).
- Queue item IDs are not run IDs. Use `runs show RUN_ID` or `export --run RUN_ID` for run-level data.
- Human-readable output is available; JSON output is available where requested.

Run introspection:

  gismo runs list
  gismo runs show RUN_ID
  gismo runs show RUN_ID --json

Export audit logs:

  gismo export --latest
  gismo export --run RUN_ID

Tool receipt audit + replay:

  gismo tools receipts list --run RUN_ID
  gismo tools receipts show RECEIPT_ID
  gismo tools replay --run RUN_ID --from-export /path/to/export.jsonl --dry-run

Memory management (policy-gated; confirmation required for high-risk namespaces):

  gismo memory put --namespace global --key key --kind note --value-text "value" \
    --confidence high --source operator --policy policy/dev-safe.json --yes
  gismo memory delete --namespace global key --policy policy/dev-safe.json --yes
  gismo memory namespace list --policy policy/dev-safe.json
  gismo memory namespace show global --policy policy/dev-safe.json
  gismo memory namespace retire global --reason "governance" \
    --policy /path/to/policy.json --yes
  gismo memory profile list --policy policy/dev-safe.json
  gismo memory profile show operator --policy policy/dev-safe.json
  gismo memory profile create --name operator --description "Operator defaults" \
    --include-namespace global --include-kind preference --include-kind fact \
    --max-items 20 --policy /path/to/policy.json --yes
  gismo memory profile retire operator --policy /path/to/policy.json --yes
  gismo memory retention list --policy policy/dev-safe.json
  gismo memory retention show global --policy policy/dev-safe.json
  gismo memory retention set global --max-items 500 --ttl-seconds 86400 \
    --reason "governance" --policy /path/to/policy.json --yes
  gismo memory retention clear global --policy /path/to/policy.json --yes
  gismo memory snapshot export --namespace project:* --out snapshots/project.json \
    --policy policy/dev-safe.json
  gismo memory snapshot diff --in snapshots/project.json --db .gismo/state.db \
    --policy policy/dev-safe.json
  gismo memory snapshot import --in snapshots/project.json --mode merge \
    --policy policy/dev-safe.json --yes --non-interactive
  gismo memory snapshot import --in snapshots/project.json --mode merge --dry-run \
    --policy policy/dev-safe.json --yes --non-interactive
  gismo memory explain --plan PLAN_EVENT_ID
  gismo memory explain --run RUN_ID --json
  gismo memory doctor check --db .gismo/state.db --policy policy/dev-safe.json
  gismo memory doctor check --db .gismo/state.db --policy policy/dev-safe.json --json
  gismo memory doctor repair --rebuild-indexes --policy /path/to/policy.json --yes
  gismo memory doctor repair --purge-tombstones --namespace global --older-than-seconds 86400 \
    --limit 1000 --policy /path/to/policy.json --yes

Notes:
- Global/project namespaces require confirmation unless policy explicitly exempts them.
- Use --non-interactive to fail closed instead of prompting.
- Namespace retirement requires a policy that allows memory.namespace.retire for the target namespace.
- Memory profiles control read-only visibility only; they never write memory.
- Memory profile create/retire requires policy allowance for memory.profile.create and
  memory.profile.retire plus explicit confirmation.
- Retention enforcement is policy/confirmation-gated via memory.retention.enforce and runs only on writes.
- Memory explain is observational only; it reads selection traces captured during ask/agent runs.
- Memory doctor repairs are operator-controlled, policy-gated, and require explicit flags (no automatic fixes).
- Snapshot item_hash values are computed from a canonical JSON payload that includes
  created_at/updated_at timestamps; snapshot_hash is the sha256 of ordered item_hashes.

Windows examples (explicit module invocation):

  python -m gismo.cli.main --db .\tmp\dev.db runs list
  python -m gismo.cli.main --db .\tmp\dev.db runs show RUN_ID
  python -m gismo.cli.main --db .\tmp\dev.db runs show RUN_ID --json
  python -m gismo.cli.main --db .\tmp\dev.db export RUN_ID

Planner (local LLM via Ollama):

  gismo ask "Summarize the last 10 queue failures" --dry-run
  gismo ask "Do X safely" --enqueue
  gismo ask "Plan with memory context" --dry-run --memory
  gismo ask "Plan with operator memory profile" --dry-run --memory-profile operator
  gismo ask "Remember the default model" --apply-memory-suggestions \
    --policy policy/dev-safe.json --yes

Agent loop (leashed autonomy):

  gismo agent "Summarize the last 10 queue failures" --dry-run
  gismo agent "Do X safely" --once
  gismo agent "Do X safely" --max-cycles 3 --yes
  gismo agent "Plan with memory context" --dry-run --memory
  gismo agent "Plan with operator memory profile" --dry-run --memory-profile operator
  gismo agent --role planner "Plan as the planner role"
  gismo agent "Apply memory suggestions" --dry-run --apply-memory-suggestions \
    --policy policy/dev-safe.json --yes
  gismo agent role list --policy policy/dev-safe.json
  gismo agent role create --name planner --memory-profile operator \
    --policy policy/dev-safe.json --yes
  gismo agent role retire planner --policy policy/dev-safe.json --yes

Agent sessions (operator-controlled checkpointing, no background autonomy):

  gismo agent session start --goal "Prepare incident summary" --role planner
  gismo agent session list
  gismo agent session show SESSION_ID --json
  gismo agent session resume SESSION_ID --yes
  gismo agent session pause SESSION_ID --yes
  gismo agent session resume SESSION_ID --dry-run
  gismo agent session cancel SESSION_ID --yes

Session notes:
- Each resume runs a single bounded iteration (no daemons, no timers, no parallelism).
- Confirmation gates and policy checks still apply; non-interactive mode fails closed.

Agent notes:
- The agent loop is leashed autonomy: it plans, enqueues, and executes only through the queue/daemon.
- Confirmation is required for high-risk plans or any shell/write actions unless --yes is provided.
- Memory context and suggestion handling mirror `ask`: suggestions are advisory unless
  --apply-memory-suggestions is set (policy/confirmation-gated; use --non-interactive to fail closed).
- Agent roles provide sequential, operator-controlled identities; roles determine which memory profile
  is injected and are recorded in audit logs. Roles cannot be used once retired.
- Use either --role or --memory/--memory-profile; role selection is authoritative.

Memory profiles (read-only selection):
- Profiles define which namespaces/kinds are visible for memory injection; they do not write memory.
- Example profiles:
  - operator: include global preferences/facts with a max-items cap.
  - project: include project:<name> namespace kinds for task context.
  - minimal: no filters (empty profile yields no injected memory).

Agent roles (multi-agent identities):
- Roles bind a name + description to a memory profile and are immutable except for retirement.
- Roles are sequential only; they do not enable parallel execution or autonomy.
- Roles differ from memory profiles: profiles describe visibility rules, while roles bind those rules
  to a named identity (e.g., planner vs executor).
- Creating/retiring roles requires policy allowance for agent.role.create/agent.role.retire plus
  explicit confirmation (use --yes or --non-interactive to fail closed).

Planner behavior:
- Produces enqueue-only plans under strict schema.
- Actions are bounded (hard limit on action count).
- Normalization/coercion exists so malformed model output does not break the system.
- Optional memory suggestions may be included in plan output for operator review (advisory only; no auto-write).
- Use --apply-memory-suggestions to write memory items from validated suggestions (policy-gated).
- Ollama is called in JSON mode and uses keep_alive to avoid repeated model reloads.
- Full audit trail is recorded for planner outputs and execution.
- Every plan includes a confidence assessment, risk flags, and a short explanation.
- Higher-risk plans require confirmation before enqueueing unless --yes is used.
- --explain prints additional assessment details.
- Use --debug to print tracebacks for ask failures.
- --memory injects eligible read-only memory context into the planner prompt (bounded, audited).
- --apply-memory-suggestions writes memory_suggestions after policy + confirmation checks. Use --yes to auto-confirm.

Planner configuration:
- Increase --timeout-s on CPU machines (60s baseline) if prompts time out.
- Environment overrides:
  - GISMO_LLM_MODEL or GISMO_OLLAMA_MODEL (model name)
  - GISMO_LLM_TIMEOUT_S or GISMO_OLLAMA_TIMEOUT_S (LLM timeout)
  - GISMO_OLLAMA_URL or OLLAMA_HOST (Ollama endpoint)
  - GISMO_OLLAMA_TRANSPORT=python|curl (Windows defaults to curl when available because urllib can be slow)
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

Phase 2 — Control & Guardrails: COMPLETE
- Hard limits on action count
- Enqueue-only execution model
- Explicit tool allowlists
- Read-only / dev-safe policy modes
- Planner confidence scoring (low/medium/high)
- Risk assessment + user confirmation gates for higher-risk plans
- Policy-aware planning prompts
- Explain-before-enqueue mode
- Full audit trail for every decision (plan, assessment, receipts, outcomes)

Phase 3 — Memory & Context: IN PROGRESS
- Persistent memory store (SQLite) with namespaces, profiles, and retention
- Read-only memory context injection into ask/agent prompts (bounded, audited)
- Advisory memory suggestions with explicit apply (policy + confirmation gated)
- Memory snapshots (export/diff/import) with dry-run and tamper detection
- Memory explain and doctor tooling for observability and operator-controlled maintenance

Next:
- Optional summarization workflows (promote run outcomes into memory under policy)
- More refined default memory profiles (operator/project/minimal) and documentation
- Expanded selection traces (why a memory item was included/excluded)

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
