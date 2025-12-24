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

## Next Steps
- Expand tool catalog and add richer permission policies.
- Add query/reporting helpers for audit trails.
- Extend orchestration tests to cover recovery workflows.
- Consider richer operator command validation and error messaging.
- Add policy-driven examples for operator tasks using the new toolpack.
- Extend daemon workflows with observability metrics and backoff tuning.

## Tests
- `python scripts/verify.py`
- `python -m gismo.cli.main enqueue "echo: systemd smoke" --db /tmp/gismo_test.db`
- `python -m gismo.cli.main daemon --once --policy policy/readonly.json --db /tmp/gismo_test.db`

## Notes
- Validation is Python-only; do not run cargo/npm checks.
