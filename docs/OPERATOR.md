# Operator Guide

## PowerShell placeholder note

PowerShell treats `<` and `>` as redirection operators. When copying commands, replace placeholders without angle brackets (e.g., use `RUN_ID`).

## Operator lifecycle

Each operator command has a single responsibility:

- `daemon`: executes queued work from the SQLite state store. It does **not** start IPC.
- `ipc serve`: starts the local control plane for queue/daemon commands. It does **not** execute work.
- `supervise up`: starts both `ipc serve` and `daemon` together and records their PIDs.
- `supervise status`: reports PID metadata plus IPC heartbeat health.
- `supervise down`: stops only the IPC/daemon processes launched by `supervise up`.
- `up`, `status`, `down`: aliases for the matching `supervise` subcommands.
- `maintain`: requeues stale `IN_PROGRESS` queue items; safe to run alongside a daemon.

## Recovery

If things get stuck, use the recovery flow (PowerShell-safe examples):

```bash
python -m gismo.cli.main status --db .gismo/state.db
python -m gismo.cli.main recover --db .gismo/state.db
python -m gismo.cli.main up --db .gismo/state.db
```

`recover` stops supervised IPC/daemon processes (best-effort), removes stale supervisor
state, and is safe to run repeatedly.
Ensure `GISMO_IPC_TOKEN` matches for `status` and `up`.

## How status works

GISMO records a daemon heartbeat in SQLite while the daemon is running.
Status commands use the heartbeat freshness as the **source of truth** for daemon health.
PID files are best-effort metadata and may go stale without a matching heartbeat.

## Maintenance loop

Use `maintain` to periodically requeue stale `IN_PROGRESS` queue items.
It is a local-only loop that never contacts the network or invokes an LLM.
Use `--stale-minutes 0` to treat any in-progress item as stale immediately.
Use `--once` for a single iteration or omit it to loop on `--interval-seconds`.
Use `--dry-run` to report what would be requeued without making changes.

PowerShell-safe examples:

```bash
python -m gismo.cli.main maintain --db .gismo/state.db --once
python -m gismo.cli.main maintain --db .gismo/state.db --interval-seconds 30 --stale-minutes 10
python -m gismo.cli.main maintain --db .gismo/state.db --once --stale-minutes 0
python -m gismo.cli.main maintain --db .gismo/state.db --once --stale-minutes 10 --dry-run
```

Each iteration prints a single-line summary:

```
maintain: requeued 3 stale items (stale_minutes=10)
maintain: no stale items (stale_minutes=10)
maintain: requeued 1 stale items (stale_minutes=0)
maintain: dry-run would requeue 2 stale items (stale_minutes=10)
maintain: dry-run no stale items (stale_minutes=10)
```

Each iteration records an audit event. Requeues emit `queue_requeue_stale`; no-op or dry-run
iterations emit `maintenance_check`.
