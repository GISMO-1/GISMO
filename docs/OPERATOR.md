# Operator Guide

## How status works

GISMO records a daemon heartbeat in SQLite while the daemon is running.
Status commands use the heartbeat freshness as the **source of truth** for daemon health.
PID files are best-effort metadata and may go stale without a matching heartbeat.
