# OPERATOR GUIDE — GISMO

This document is for operators running GISMO day-to-day. It focuses on how to run the system, how to inspect it, how to recover it, and how to keep it safe. This is not a contributor guide and not an architecture deep dive.

-------------------------------------------------------------------------------

BASIC MENTAL MODEL

GISMO is a local orchestration core.

- You enqueue work.
- A daemon executes work.
- Everything is persisted to SQLite.
- Policy gates every action.
- Nothing runs silently.
- If it did not log, it did not happen.

-------------------------------------------------------------------------------

CANONICAL INVOCATION

Preferred invocation (always valid):

  python -m gismo.cli.main ...

If installed in editable mode:

  gismo ...

-------------------------------------------------------------------------------

STATE & FILE LOCATIONS

Default state database:
  .gismo/state.db

Exports directory:
  .gismo/exports/

Important:
- Export paths are anchored to the database location.
- Exports do NOT depend on your current working directory.
- If you change --db, exports move with it.

Example:
  --db D:\gismo\data\state.db
  Exports -> D:\gismo\data\exports\

-------------------------------------------------------------------------------

CORE COMMANDS

RUN A SINGLE COMMAND (IMMEDIATE):

  gismo run "echo:hello world"

- Executes immediately
- Still audited
- Does not require daemon

ENQUEUE A COMMAND:

  gismo enqueue "note:remember this"

- Adds item to durable queue
- Requires daemon to execute

-------------------------------------------------------------------------------

DAEMON & SUPERVISION

FOREGROUND DAEMON:

  gismo daemon

- Runs execution loop in foreground
- Ctrl+C stops it cleanly

SUPERVISED MODE (RECOMMENDED FOR LONG RUNS):

  gismo up
  gismo status
  gismo down
  gismo recover

What this does:
- Starts daemon + IPC server in background
- Tracks health via heartbeat
- Allows safe recovery if something crashes

STATUS CHECK:
- Uses DB heartbeat, not PID alone
- If heartbeat is stale, daemon is considered down

RECOVER:
- Stops orphaned processes
- Clears stale PID/IPC state
- Does NOT touch the database

-------------------------------------------------------------------------------

QUEUE INSPECTION

QUEUE STATS:

  gismo queue stats

QUEUE LIST:

  gismo queue list

QUEUE SHOW:

  gismo queue show ID_OR_PREFIX

Notes:
- Short ID prefixes are supported
- Ambiguity is detected and reported
- Failed items remain by design

-------------------------------------------------------------------------------

EXPORTING LOGS

LATEST RUN:

  gismo export --latest

SPECIFIC RUN:

  gismo export --run RUN_ID

ALL RUNS:

  gismo export --all

Format:
- JSON Lines (one event per line)
- Safe to parse with jq, Python, etc.
- Includes tool inputs, outputs, errors

-------------------------------------------------------------------------------

LOCAL LLM PLANNER (ASK)

BASIC USE:

  gismo ask "summarize last 5 failures" --dry-run

EXECUTE PLAN:

  gismo ask "do X safely" --enqueue

Planner rules:
- Produces enqueue-only plans
- Action count is bounded
- Output is normalized
- Uses Ollama JSON mode with keep_alive to reduce reload latency
- Policy is still enforced at execution time
- Planner cannot execute directly
- Confidence assessment and risk flags are printed with every plan
- Higher-risk plans require confirmation before enqueueing unless --yes is used
- Use --explain to print expanded assessment details
- Use --debug to print tracebacks for ask failures

Planner configuration:
- Increase --timeout-s on CPU machines (60s baseline) if Ollama is slow.
- Environment overrides:
  - GISMO_LLM_MODEL or GISMO_OLLAMA_MODEL
  - GISMO_LLM_TIMEOUT_S or GISMO_OLLAMA_TIMEOUT_S
  - GISMO_OLLAMA_URL or OLLAMA_HOST
  - GISMO_OLLAMA_TRANSPORT=python|curl (Windows defaults to curl when available because urllib can be slow)
- keep_alive defaults to 10m so models remain loaded for repeated calls.

Always prefer:
- --dry-run first
- Review plan
- Then --enqueue

-------------------------------------------------------------------------------

MAINTENANCE & RECOVERY

MAINTAIN STALE TASKS (ONE SHOT):

  gismo maintain --once --stale-minutes 0

CONTINUOUS MAINTENANCE:

  gismo maintain --interval-seconds 30 --stale-minutes 10

What this does:
- Requeues tasks stuck IN_PROGRESS
- Uses DB timestamps
- Safe to run alongside daemon
- Fully audited

-------------------------------------------------------------------------------

POLICY & SAFETY

Default behavior:
- Deny by default
- shell: blocked unless allowlisted

Policies:
- dev-safe.json
- readonly.json

Use custom policy:

  gismo --policy path\to\policy.json run "..."

If a task fails with PERMISSION_DENIED:
- This is expected behavior
- Adjust policy explicitly if needed
- Never weaken policy casually

-------------------------------------------------------------------------------

WINDOWS NOTES

- Windows is the reference platform.
- Path handling is explicit.
- Shell builtins are executed via cmd /c where required.
- SQLite locking issues are treated as bugs, not “Windows quirks”.

If something only works on Linux, it is not finished.

-------------------------------------------------------------------------------

SAFE OPERATING PRACTICES

- Use dry-run for planner requests.
- Inspect queue regularly.
- Export logs for audits.
- Stop system cleanly (gismo down).
- Use recover if something crashes.
- Keep policies tight.
- If behavior surprises you, investigate logs.

-------------------------------------------------------------------------------

COMMON FAILURE MODES

PERMISSION_DENIED:
- Policy blocked the action
- Fix policy or request

FAILED TASK:
- Logged and retained intentionally
- Inspect with queue show

DAEMON NOT RUNNING:
- Start with gismo up or gismo daemon
- Check status heartbeat

-------------------------------------------------------------------------------

FINAL NOTES

GISMO is designed to be boring, predictable, and safe.
If it feels exciting, something is probably wrong.

Policy before power.
Explicit over implicit.
State is truth.
Audit everything.
