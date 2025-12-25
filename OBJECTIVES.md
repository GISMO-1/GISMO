# GISMO Objectives, Progress Log, and Next Steps

Date: 2025-12-25  
Environment: Windows (PowerShell), Python 3.13.x  
Repo path: `D:\repos\GISMO`  
Virtual environment: `.venv`

---

## 1) What we accomplished (this session)

### Queue CLI introspection (implemented)

The following queue inspection commands are now implemented in the CLI:

- `queue stats`
- `queue list`
- `queue show <id>`

Capabilities:
- Short-ID resolution (prefix matching with ambiguity detection).
- Human-readable output.
- JSON output where requested.
- Verified logic for resolving short IDs against full UUIDs.

A new test suite was added and pushed:

- `tests/test_queue_cli.py`
  - Covers stats, list, show
  - Covers short-id resolution and ambiguity handling

Commit pushed:
- `Add queue CLI tests (short id, stats, list, show)`

---

### Packaging / import stability

- Confirmed correct invocation pattern:
  - `python -m gismo.cli.main ...`
- Confirmed repo-root execution is required.
- Project installed as editable:
  - `python -m pip install -e .`
- Virtual environment created and used successfully.
- Verified module resolution:
  - `import gismo` resolves to repo source.

---

### State / database behavior

- Active SQLite DB location:
  - `.gismo/state.db`
- Schema verified:
  - `queue_items`, `runs`, `tasks`, `tool_calls`
- CLI enqueue + daemon execution works for supported operators:
  - `echo:`
  - `note:`
  - `graph:`

---

### Operator constraints (expected behavior)

- `shell:` operator is intentionally unsupported by default.
- Unsupported commands fail with:
  - Clear rejection (but messaging still needs improvement).

---

### Git hygiene

- Repo is clean.
- Branch `main` is up to date with `origin/main`.
- New tests are committed and pushed.

---

## 2) Current status snapshot

### What is working

- Queue inspection commands exist and function logically.
- Short-ID resolution works.
- Editable install + venv workflow is stable.
- State persistence works across CLI and daemon runs.
- Tests correctly encode expected behavior.

---

### What is NOT working (current blockers)

These are **real bugs**, not design decisions.

#### A) CLI `--db` flag handling (critical)

Tests expect this to work:

```bash
python -m gismo.cli.main queue stats --db <path>
````

Current behavior:

* `queue` subcommands reject `--db` as an unrecognized argument.

Root cause:

* Argument parsing structure does not propagate `--db` to `queue` subcommands.

---

#### B) SQLite file locking on Windows (critical)

Symptoms:

* Many tests fail with:

  ```
  PermissionError: [WinError 32] The process cannot access the file because it is being used by another process
  ```
* Temporary directories fail to clean up because `state.db` is still open.

Root cause:

* SQLite connections are not being closed deterministically.
* Some code paths leak connections or cursors.
* Windows does not allow deletion of open SQLite files.

Impact:

* Massive test failure cascade.
* Prevents reliable Windows support.

---

#### C) ShellTool fails on Windows (test failure)

Failing test:

* `ShellToolTest.test_shell_tool_logs_output_and_exit_code`

Root cause:

* `echo` is a shell builtin on Windows.
* `subprocess.run(["echo", "hello"], shell=False)` fails with `FileNotFoundError`.

Expected behavior:

* Allowlisted shell commands should work cross-platform.
* Windows requires `cmd /c echo hello` or equivalent handling.

---

## 3) Decisions and direction

### Priority shift (important)

The original goal of *adding queue inspection commands* is **complete**.

The new immediate priority is:

> **Make the existing implementation pass the full test suite on Windows.**

This is now a **stability and correctness phase**, not a feature phase.

---

## 4) Immediate next steps (do these in order)

### Step 1 — Fix CLI `--db` argument plumbing

Requirements:

* `--db` (or `--db-path`) must be accepted by:

  * `queue stats`
  * `queue list`
  * `queue show`
* Tests explicitly place `--db` **after** the subcommand.
* Argparse structure must support this without breaking existing commands.

Acceptance criteria:

* All queue CLI tests pass argument parsing.
* No regression to other commands.

---

### Step 2 — Fix SQLite connection lifecycle (Windows-safe)

Requirements:

* Every SQLite connection must be explicitly closed.
* No connection may survive beyond command execution.
* All early-return and exception paths must close connections.
* Temp DBs must be deletable immediately after CLI exits.

Targets to audit:

* StateStore initialization
* Queue claim logic
* Daemon execution loop
* Export paths
* Any cached or global connection usage

Acceptance criteria:

* Temporary directories clean up without error.
* No `WinError 32` anywhere in test suite.
* `pytest -q` passes SQLite-heavy tests on Windows.

---

### Step 3 — Fix ShellTool Windows compatibility

Requirements:

* Allowlisted commands like `["echo", "hello"]` must work on Windows.
* Security model (allowlist) must remain intact.
* Behavior must still work on POSIX systems.

Acceptable approaches:

* Detect Windows + `echo` and route through `cmd.exe /c`.
* Or enable `shell=True` *only* for allowlisted commands, with careful quoting.

Acceptance criteria:

* `ShellToolTest` passes.
* No broad shell enablement.
* No policy regression.

---

## 5) Explicit non-goals (for now)

* No new operators.
* No networking features.
* No policy expansion beyond what tests require.
* No refactors unless required to fix correctness issues.

---

## 6) Notes / gotchas

* PowerShell is not bash.
* Do not paste prompt prefixes into commands.
* SQLite behaves differently on Windows — treat file locks as fatal.
* Tests are the source of truth right now.
* Do not weaken or skip tests.

---

## 7) Definition of done

This phase is complete when:

* `pytest -q` passes on Windows.
* Queue CLI works with `--db` everywhere.
* SQLite temp DBs clean up reliably.
* ShellTool works cross-platform.
* Repo is stable enough to resume feature work.
