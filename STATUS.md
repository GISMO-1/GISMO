# GISMO Status (source of truth)

## Current Focus: Phase 2A — Windows Reliability Gate

### Completed recently
- Planner: “too many actions” messaging clarified (warning + confirmation, not refusal).

### Blockers
1) CLI `--db` propagation for all subcommands (including when `--db` appears after subcommand).
2) SQLite locking / leaked handles on Windows (temp DB files must be deletable immediately after CLI exit).
3) ShellTool Windows builtins (e.g., `echo`) must work without weakening allowlist security model.

### Done Criteria
- `python scripts/verify.py` passes on Windows.
- No WinError 32 / locked-file failures in tests or manual runs.
- Allowlisted shell commands run cross-platform; non-allowlisted commands deny reliably.
