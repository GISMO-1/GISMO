# Operator Guide

## PowerShell placeholder note

PowerShell treats `<` and `>` as redirection operators. When copying commands, replace placeholders without angle brackets (e.g., use `RUN_ID`).

## How status works

GISMO records a daemon heartbeat in SQLite while the daemon is running.
Status commands use the heartbeat freshness as the **source of truth** for daemon health.
PID files are best-effort metadata and may go stale without a matching heartbeat.

## Maintenance loop

Use `maintain` to periodically requeue stale `IN_PROGRESS` queue items.
It is a local-only loop that never contacts the network or invokes an LLM.
Use `--stale-minutes 0` to treat any in-progress item as stale immediately.

PowerShell-safe examples:

```bash
python -m gismo.cli.main maintain --db .gismo/state.db --once
python -m gismo.cli.main maintain --db .gismo/state.db --interval-seconds 30 --stale-minutes 10
python -m gismo.cli.main maintain --db .gismo/state.db --once --stale-minutes 0
```

Each iteration prints a single-line summary:

```
maintain: requeued 3 stale items (stale_minutes=10)
maintain: no stale items (stale_minutes=10)
maintain: requeued 1 stale items (stale_minutes=0)
```

The maintenance loop records an audit event only when it requeues stale items, keeping the
events table focused on actionable changes instead of per-interval noise.
