# GISMO Objectives, Progress Log, and Next Steps

Date: 2025-12-25  
Environment: Windows (PowerShell), Python 3.13.9, repo path `D:\repos\GISMO`, venv `.venv`

## 1) What we accomplished (this session)

### Packaging / import stability

- Confirmed `python -m gismo.cli.main ...` failed when executed from the wrong directory because the package was not importable.
- Verified correct execution from repo root (`D:\repos\GISMO`) and installed the project as editable:
  - `python -m pip install -e .`
- Created and activated a local virtual environment:
  - `python -m venv .venv`
  - `.\.venv\Scripts\Activate.ps1`
- Confirmed module resolution:
  - `python -c "import gismo; print(gismo.__file__)"` resolves to the repo source.

### State / database clarity

- Identified two different SQLite DB locations:
  - Default state DB: `D:\repos\GISMO\.gismo\state.db` (active, contains `queue_items`)
  - Legacy/removed path previously used: `D:\gismo\data\gismo.db` (folder later deleted)
- Verified schema exists in `state.db`:
  - Tables: `queue_items`, `runs`, `tasks`, `tool_calls`
- Verified there are queue entries and execution history in `state.db`.
- Verified that the CLI `enqueue` and `daemon --once` flow works end-to-end for supported commands (`echo:`).

### Operator command constraints (expected behavior)

- Confirmed `shell:` is currently unsupported by the operator implementation:
  - Failed row example:
    - Command: `shell: cmd /c echo PING_FROM_CMD ...`
    - Error: `Unsupported command. Use echo:, note:, or graph:.`
- Confirmed supported operator commands are currently:
  - `echo:`
  - `note:`
  - `graph:`

### Git hygiene

- Verified repo is clean:
  - `git status` shows working tree clean, branch up to date with origin.

## 2) Current status snapshot

### Queue status (as of this session)

- `queue_items` shows a mix of `SUCCEEDED` and `FAILED`.
- The only observed failure cause is use of an unsupported `shell:` command.

### What is working

- Editable install + venv is working.
- CLI can enqueue and daemon can process supported operator commands.
- State DB is persisting in `.gismo\state.db`.

### What is not working (by design, but needs better UX)

- `shell:` commands are rejected; debugging required manual SQLite inspection.
- CLI is missing operator introspection subcommands (queue list/show/stats), forcing ad-hoc scripts.

## 3) Decisions and direction

### Immediate priority (next session)

Make GISMO self-diagnosing from the CLI so debugging does not require SQLite one-liners or scratch scripts.

This means implementing:
1. `queue stats`
2. `queue list`
3. `queue show <id>`

And improving failure messaging so it is obvious when a policy or operator capability blocks a command.

## 4) Next steps (do these in order)

### Step 1 — Add CLI queue introspection commands

Implement a `queue` command group under `gismo.cli`:

- `python -m gismo.cli.main queue stats [--db PATH]`
  - Output: counts grouped by status.

- `python -m gismo.cli.main queue list [--db PATH] [--limit N]`
  - Output columns: created_at, id (short), status, command_text (trimmed).

- `python -m gismo.cli.main queue show <id> [--db PATH]`
  - Output: full row, including `last_error`, attempt_count/max_attempts, timestamps.

Acceptance criteria:
- Works in PowerShell on Windows.
- Defaults to `.gismo\state.db` if `--db` not provided.
- Human-readable output (no giant dumps unless explicitly requested).

### Step 2 — Improve operator error messaging

When an unsupported command is encountered:
- Include the rejected command prefix.
- Include supported prefixes.
- If policy is relevant, state which policy gate blocked it.
- Make the message actionable (what to do next).

### Step 3 — Add a “dev-safe shell” option (optional, explicitly gated)

If desired, add a policy-gated `shell:` operator for local-only usage:
- Disabled by default.
- Explicit allow-list (commands or executables).
- No network by default.
- Clear warnings in docs and CLI help.

## 5) Notes / gotchas to remember

- PowerShell is not bash: heredocs like `python - << 'PY'` will fail in PowerShell.
- Don’t paste prompt prefixes (e.g., `(.venv) PS D:\...>`) back into the terminal.
- If you don’t pass `--db`, GISMO will use its default state DB (currently `.gismo\state.db`).
- When debugging, prefer `queue show` once implemented instead of direct SQLite.
