# Handoff

## Status
- Implemented GISMO core scaffolding (models, state store, permissions, tools, agent, orchestrator).
- Added CLI demo and smoke test.
- Added minimal packaging config and environment placeholder.
- Hardened orchestration with idempotency keys, retry tracking, failure taxonomy, and transactional state updates.
- Added task dependency persistence and scheduler-driven task graph execution.
- Extended CLI demo and smoke tests for dependency graphs and deadlock handling.
- Added repository hygiene files, developer tooling, and architecture/decision docs.
- Added operator command parsing and CLI run flow with deterministic idempotency keys and summaries.
- Expanded smoke tests to cover operator run commands, permissions, graph dependencies, and idempotency skips.
- Added policy-driven filesystem and shell toolpack with strict base directory and allowlist enforcement.
- Added policy loader for CLI workflows and documented policy usage in README.
- Added toolpack tests covering base directory enforcement and shell allowlist outputs.
- Restored verification coverage for toolpack tests and made tests importable as a package.
- Added readonly default policy auto-loading with enforcement tests for denied tools.
- Added JSONL audit export for runs, tasks, and tool calls with optional redaction.
- Added dev-safe shell policy profile and tests for allowlisted shell execution.
- Extended CLI and documentation with export and policy usage.
- Wired CLI subcommand handlers for demo, run, and export with parser routing tests.
- Added SQLite-backed queue with daemon execution loop and CLI enqueue/daemon commands.
- Added daemon queue tests for enqueue/claim, execution, retries, and non-retryable failures.
- Added systemd service templates plus documentation and CLI support for consistent DB paths via --db.
- Closed SQLite connections deterministically and fixed queue CLI db flag parsing for Windows.
- Adjusted shell tool execution to support Windows built-in commands via cmd.exe.
- Added CLI run show summaries with task/tool call output details for operator introspection.
- Added queue purge-failed CLI command with dry-run confirmation and enriched queue list columns.
- Added tests covering run show output and purge-failed safety behavior.
- Added daemon shutdown signal handling and Windows Task Scheduler install/uninstall CLI helpers.
- Added optional Windows Task Scheduler startup trigger and improved install error reporting for non-admin defaults.
- Added Windows Startup folder launcher install/uninstall commands with tests and Task Scheduler access-denied guidance.
- Added local IPC control plane (named pipe/Unix socket) with token auth, CLI wiring, and handler tests.
- Restored global CLI db flag parsing and IPC subcommand support for db path overrides.
- Added IPC client connection error handling with friendly CLI messaging and tests.
- Added daemon pause state persistence, IPC daemon controls, and queue maintenance IPC actions.
- Added queue requeue-stale and purge-failed IPC behaviors with coverage tests.
- Added supervise CLI command to run IPC server and daemon together with PID-based control.
- Added IPC ping CLI wiring plus supervisor reuse/authorization checks and PID tracking for started children.
- Reconciled supervise status against IPC reachability and daemon status with Windows-safe PID checks.
- Derived Windows IPC pipe names from the database path and aligned supervise/IPC client discovery.
- Added Windows IPC endpoint unit coverage and supervise db-path probe validation.
- Hardened IPC accept shutdown handling with Windows-specific error guards and coverage tests.
- Added daemon heartbeat persistence, IPC status enrichment, and supervise health interpretation.
- Documented heartbeat status semantics and operator guidance.

## Next Steps
- Validate IPC CLI db flag usage on Windows.
- Validate IPC client connection errors on Windows named pipes.
- Validate Windows IPC pipe binding error messaging and supervise cleanup.
- Validate IPC daemon pause/resume behavior in long-running deployments.
- Validate supervise up/down behavior on Windows named pipes and terminal restarts.
- Validate supervise status output on Windows when IPC is running outside supervise.
- Expand tool catalog and add richer permission policies.
- Add query/reporting helpers for audit trails.
- Extend orchestration tests to cover recovery workflows.
- Consider richer operator command validation and error messaging.
- Add policy-driven examples for operator tasks using the new toolpack.
- Extend daemon workflows with observability metrics and backoff tuning.
- Consider additional CLI filters for run/task search and failure triage.
- Add Windows-specific operational playbooks for Task Scheduler maintenance.
- Document Windows Startup launcher maintenance steps.
- Consider IPC integration tests over the real transport layer.
- Validate heartbeat freshness thresholds on long-running daemons.

## Tests
- `python scripts/verify.py`

## Notes
- Validation is Python-only; do not run cargo/npm checks.
- Windows daemon task install: `python -m gismo.cli.main daemon install-windows-task --db .gismo/state.db`
- Optional startup trigger (may require admin): `python -m gismo.cli.main daemon install-windows-task --db .gismo/state.db --on-startup`
- Windows daemon task uninstall: `python -m gismo.cli.main daemon uninstall-windows-task --name "GISMO Daemon" --yes`
- Windows startup launcher install: `python -m gismo.cli.main daemon install-windows-startup --db .gismo/state.db`
- Windows startup launcher uninstall: `python -m gismo.cli.main daemon uninstall-windows-startup --name "GISMO Daemon" --yes`
