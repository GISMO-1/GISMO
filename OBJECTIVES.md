# GISMO Objectives, Progress Log, and Next Steps

Date: 2025-12-25  
Environment: Windows (PowerShell), Python 3.13.x, repo path `D:\repos\GISMO`, venv `.venv`  
Repo: `GISMO-1/GISMO` (main)

## 1) Current Project Goal

Build a reliable, test-covered, SQLite-backed “agent ops” runtime:
- CLI-driven operator commands (echo/note/graph today; shell later under explicit policy)
- Persistent run/task/tool-call/queue storage
- A daemon that processes queued commands safely and predictably
- A CLI that can diagnose and inspect state without raw SQLite poking

## 2) What is working now (verified)

### Queue + daemon happy path
- `enqueue` creates queue items in `.gismo/state.db`
- `daemon --once` processes queued items and updates status fields
- `queue stats`, `queue list`, and `queue show` work for human inspection
- Short-id / prefix resolution works (e.g. `queue show f0a4c078` resolves correctly)

### Operator behavior
- Supported operator prefixes currently:
  - `echo:`
  - `note:`
  - `graph:`
- Unsupported prefixes (like `shell:`) correctly fail with an “unsupported command” message and mark queue items FAILED.

### Git state
- `tests/test_queue_cli.py` was committed and pushed:
  - Commit: `2047462` (Add queue CLI tests)

## 3) What is failing now (must fix)

### A) CLI `--db` parsing with queue subcommands
Tests expect:
- `python -m gismo.cli.main queue stats --db <path>`
- `python -m gismo.cli.main queue list --db <path> ...`
- `python -m gismo.cli.main queue show --db <path> <id>`

Current behavior:
- `--db` is only accepted before `queue`, not after `stats/list/show`, so argparse rejects it.

### B) Windows temp DB cleanup failures (WinError 32)
Many tests fail at `TemporaryDirectory()` cleanup because:
- `state.db` is still open/locked at teardown.

This indicates:
- A connection is being held open too long, or not being closed on some code path.

### C) ShellTool fails on Windows for builtin commands
Test uses allowlist command:
- `["echo", "hello"]`
On Windows, `echo` is a `cmd.exe` builtin, so subprocess fails unless executed through `cmd /c`.

## 4) Immediate Objectives (next coding sprint)

### Objective 1 — Make queue CLI accept `--db` exactly as tests expect
Acceptance criteria:
- `queue stats --db PATH` works
- `queue list --db PATH` works
- `queue show --db PATH <id>` works
- Keep existing behavior (`--db` before `queue`) working too

### Objective 2 — Eliminate SQLite file locks during tests on Windows
Acceptance criteria:
- All tests pass without `WinError 32` on cleanup
- No lingering SQLite connections after CLI/daemon operations
- Prefer “open connection late, close early” patterns:
  - Open/close per operation, not per loop

### Objective 3 — Fix ShellTool on Windows without weakening policy gates
Acceptance criteria:
- `ShellTool` can run allowlisted `["echo", "hello"]` on Windows
- Implementation uses `cmd /c` (or equivalent) only on Windows
- Allowlist checks still apply to the original command tokens

## 5) Optional (later) enhancements

### Better failure UX
- When rejecting unsupported operator prefixes:
  - include rejected prefix
  - list supported prefixes
  - include next action (“use echo:, note:, graph:”)

### Add `shell:` operator (explicitly gated)
- Disabled by default
- Policy allowlist required
- Clear security warning in docs/help

## 6) Developer Notes / Gotchas

- PowerShell is not bash: don’t use bash heredoc patterns.
- SQLite on Windows is strict about open handles; teardown will fail if any connection remains open.
- Argparse only recognizes options in the parser that owns them; subcommand-local options must be added at that level if tests place flags after subcommands.
