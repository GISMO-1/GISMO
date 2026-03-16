```markdown
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

OPERATOR SMOKE SCRIPTS (WINDOWS)

Quick smoke checks (no Ollama required):

  powershell -ExecutionPolicy Bypass -File scripts/operator_smoke.ps1
  powershell -ExecutionPolicy Bypass -File scripts/e2e_smoke.ps1

What they prove:
- operator_smoke.ps1: basic operator run + export on a temp DB.
- e2e_smoke.ps1: enqueue → daemon --once → export on a temp DB.

Notes:
- Scripts create temp state DBs under %TEMP% and clean them up on exit.
- Use -EnableMemoryPreview on e2e_smoke.ps1 to record and verify a memory injection trace event.

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
- Produces enqueue-only plans except for intent=inquire (echo-only, non-enqueue)
- Action count is bounded
- Output is normalized (schema enforcement; malformed model output fails closed)
- Uses Ollama JSON mode with keep_alive to reduce reload latency
- Policy is still enforced at execution time
- Planner cannot execute directly
- Inquire intent is echo-only and never enqueues work; ask behaves as read-only answer mode.
- Explicit write intent or flags (for example, --enqueue or --apply-memory-suggestions)
  are required to log/remember.
- Deterministic risk assessment (LOW/MEDIUM/HIGH), flags, and rationale printed with every plan
- MEDIUM/HIGH risk plans require confirmation before enqueueing unless --yes is used
- Non-interactive mode fails closed if confirmation would be required
- Dry-run prints explain output and records audit events only (no state writes beyond audit)
- Use --explain to print expanded explain details
- Use --json to emit a stable JSON explain artifact
- Planner prompts are policy-aware (allowed tools, shell allowlist summary, write permissions)
- Use --debug to print tracebacks for ask failures

Risk levels:
- LOW: read-only inspection (echo/list/show/diff/export/explain)
- MEDIUM: more than 3 actions, memory modifications, or supervisor lifecycle commands
- HIGH: shell usage or write/modify tools (including dangerous tool categories)

Planner configuration:
- Increase --timeout-s on CPU machines (60s baseline) if Ollama is slow.
- Environment overrides:
  - GISMO_LLM_MODEL or GISMO_OLLAMA_MODEL
  - GISMO_LLM_TIMEOUT_S or GISMO_OLLAMA_TIMEOUT_S
  - GISMO_OLLAMA_URL or OLLAMA_HOST
  - GISMO_OLLAMA_TRANSPORT=python|curl (Windows defaults to curl when available because urllib can be slow)
- keep_alive defaults to 10m so models remain loaded for repeated calls.

DEFER PLAN FOR REVIEW (INTERACTIVE APPROVAL):

  gismo ask "do X" --defer

- Saves the LLM plan as a pending record in the DB instead of enqueueing it
- Prints the plan ID; nothing executes until you explicitly approve

Always prefer:
- --dry-run first
- Review plan
- Then --enqueue (or --defer if you want to hold it for later)

-------------------------------------------------------------------------------

PLAN APPROVAL (INSPECT, EDIT, APPROVE/REJECT)

If a plan was deferred with --defer, or if you want human-in-the-loop control before any execution, use the plan commands.

LIST PENDING PLANS:

  gismo plan list
  gismo plan list --status PENDING
  gismo plan list --status APPROVED
  gismo plan list --json

INSPECT A PLAN:

  gismo plan show PLAN_ID
  gismo plan show PLAN_ID --json

- Prints intent, risk level, risk flags, rationale, and all actions
- Short-ID prefix resolution supported (e.g. first 8 chars)

APPROVE A PLAN:

  gismo plan approve PLAN_ID
  gismo plan approve PLAN_ID --yes

- Enqueues all actions from the plan (same as manual --enqueue)
- Marks plan APPROVED; status is immutable after approval
- Shows a summary of enqueued IDs

REJECT A PLAN:

  gismo plan reject PLAN_ID
  gismo plan reject PLAN_ID --reason "too risky"

- Marks plan REJECTED with optional reason; no actions are enqueued

EDIT BEFORE APPROVING:

  gismo plan edit PLAN_ID --action 1 --cmd "echo:updated"
  gismo plan edit PLAN_ID --action 2 --remove

- --action N: 1-based action index
- --cmd: replace the action's command text (validated before saving)
- --remove: delete the action entirely
- Only PENDING plans can be edited; approved/rejected plans are immutable

Notes:
- Use gismo plan show to review actions before editing.
- You can also approve/reject/edit plans in the web UI Plans tab.
- Plan IDs support short-prefix resolution (same as queue item IDs).

-------------------------------------------------------------------------------

AGENT LOOP (LEASHED AUTONOMY)

The agent is an operator-leashed iteration loop. It plans and acts only through the queue/daemon and never bypasses policy.

ONE-SHOT (PLAN/ACT ONCE):

  gismo agent "summarize last 5 failures" --dry-run
  gismo agent "do X safely" --once

BOUNDED MULTI-CYCLE:

  gismo agent "do X safely" --max-cycles 3 --yes

Notes:
- The agent uses the same safety model as ask: bounded actions, enqueue-only plans, policy enforcement.
- Confirmation gates still apply. Use --non-interactive to fail closed rather than prompting.

-------------------------------------------------------------------------------

MEMORY (PERSISTENT, POLICY-GATED)

Memory is a local SQLite-backed store for facts, preferences, and operational notes.

Direct memory writes (operator-controlled):

  gismo memory put --namespace global --key key --kind note --value-text "value" \
    --confidence high --source operator --policy policy/dev-safe.json --yes

Read memory:

  gismo memory get --namespace global key --policy policy/dev-safe.json
  gismo memory namespace list --policy policy/dev-safe.json

Memory in ask/agent (read-only injection):

  gismo ask "plan with memory context" --dry-run --memory
  gismo ask "plan with operator profile" --dry-run --memory-profile operator

Memory injection trace (bounded, deterministic):

  gismo memory explain --plan PLAN_EVENT_ID --json
  gismo memory preview --memory-profile operator --policy policy/dev-operator.json --json

Notes:
- Ordering is deterministic (updated_at desc, namespace, key).
- The trace includes eligibility counts, selected items, dropped counts, and an injection_hash.

Memory profiles are governance objects:
- Profile lifecycle (create/retire) is policy-gated and requires explicit confirmation.
- Use an operator policy that allows memory.profile.create/retire.

  gismo memory profile create --name operator --description "Operator defaults" \
    --include-namespace global --include-kind preference --include-kind fact \
    --max-items 20 --policy policy/dev-operator.json --yes
  gismo memory profile retire operator --policy policy/dev-operator.json --yes

Memory suggestions:
- The LLM may emit memory_suggestions.
- Suggestions are advisory by default (no auto-write).
- Apply them only when explicitly requested:

  gismo ask "remember default model" --dry-run
  gismo ask "remember default model" --apply-memory-suggestions \
    --policy policy/dev-safe.json --yes

-------------------------------------------------------------------------------

TERMINAL DASHBOARD (TUI)

  gismo tui

- Live curses dashboard: queue, runs, daemon status
- Auto-refreshes every 3 seconds
- No external dependencies

-------------------------------------------------------------------------------

LOCAL WEB DASHBOARD

  gismo web
  gismo web --port 8080
  gismo web --no-browser

- Opens a browser to 127.0.0.1:7800 by default
- Tabs: Queue, Runs, Memory, Plans, Settings
- Queue tab: view items, cancel individual items, purge all failed
- Runs tab: run list with task/tool call detail view
- Memory tab: namespace list + all active memory items
- Plans tab: pending/approved/rejected plans with inline editing, approve/reject buttons
- Settings tab: TTS voice selection and live playback test
- Daemon sidebar: live status, pause/resume controls
- No external HTTP libraries; stdlib only

-------------------------------------------------------------------------------

WEB CHAT

STARTING THE CHAT INTERFACE:

  gismo web

- Chat tab is available in the web dashboard at 127.0.0.1:7800
- Uses the gismo Ollama model (local, no cloud)
- Full conversation history is maintained within the session

SENDING A MESSAGE:

- Type in the input box and press Enter or click Send
- GISMO responds inline in the chat window
- Responses reflect GISMO's identity and operator policy

MIC BUTTON (VOICE INPUT):

- Click the mic button to record a voice message
- Recording stops when you click again or after silence is detected
- Transcribed text is inserted into the chat input automatically
- Requires a browser with microphone access (localhost is always allowed)
- No audio is sent to any external service; transcription is local

CHAT HISTORY LOGGING:

Every user/assistant exchange is automatically appended to:

  .gismo/chat_history.jsonl

Format (one JSON object per line):

  {"timestamp": "2026-03-16T14:23:01+00:00", "user": "...", "assistant": "..."}

Notes:
- The file is created automatically on first chat message
- Directory (.gismo/) is created if it does not exist
- Logging failures are silent and never block the chat
- This file is suitable as fine-tuning training data for future model runs
- To review recent exchanges: python -c "import json,pathlib; [print(json.loads(l)['user']) for l in pathlib.Path('.gismo/chat_history.jsonl').read_text().splitlines()]"

-------------------------------------------------------------------------------

TEXT-TO-SPEECH (TTS)

GISMO supports local TTS synthesis using piper-tts. Voice models download on first use.

LIST AVAILABLE VOICES:

  gismo tts voices list

SET VOICE PREFERENCE:

  gismo tts voices set en_US-ryan-high

DOWNLOAD A VOICE MODEL IN ADVANCE:

  gismo tts voices download en_US-lessac-medium

SPEAK TEXT:

  gismo tts speak "Hello from GISMO"
  gismo tts speak "Hello" --voice en_GB-alan-medium
  gismo tts speak "Hello" --out hello.wav

Available voices:
  en_GB-northern_english_male-medium  (default)
  en_GB-alan-medium
  en_US-lessac-medium
  en_US-ryan-high
  en_US-amy-medium

Notes:
- Voice models are cached at ~/.cache/gismo/tts/ and not re-downloaded once present.
- Voice preference is stored in memory (namespace gismo:settings, key tts.voice).
- Voice settings are also available in the web dashboard Settings tab.
- GISMO is pronounced "GHIZMO" in synthesis (hard G applied via preprocessing).

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

- Use --dry-run for planner requests.
- Use --defer if you want to hold a plan for review before execution.
- Inspect queue regularly.
- Export logs for audits.
- Stop system cleanly (gismo down).
- Use recover if something crashes.
- Keep policies tight.
- If behavior surprises you, investigate logs.
- Review pending plans before approving; use gismo plan show to inspect actions.

-------------------------------------------------------------------------------

COMMON FAILURE MODES

PERMISSION_DENIED:
- Policy blocked the action
- Fix policy or request

FAILED TASK:
- Logged and retained intentionally
- Inspect with queue show
- Export the run if you need to share the incident trail

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
```
