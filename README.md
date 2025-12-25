# GISMO

**General Intelligent System for Multi-flow Operations**

## What GISMO Is

GISMO is a **persistent AI orchestration core** designed to observe systems, maintain state, delegate tasks, and execute actions across **digital and physical domains** through connected tools and actuators.

GISMO is not a chatbot.
GISMO is not a personality.
GISMO is an **operator**.

Think of GISMO as the **brain and nervous system** of a larger system—capable of coordinating agents, invoking tools, tracking outcomes, and enforcing authority boundaries.

---

## What GISMO Is Not

To avoid ambiguity, GISMO explicitly does **not** aim to be:

* A conversational assistant
* An AGI research project
* A robotics-first platform
* A UI-heavy product demo
* A speculative or philosophical framework

Any functionality that does not serve **orchestration, state, delegation, or execution** is out of scope.

---

## Core Capabilities (Target State)

GISMO is designed around the following non-negotiable capabilities:

1. **Persistent State**

   * Long-lived memory of system configuration, tasks, outcomes, and preferences
   * Explicit tracking of what exists, what is running, and what has failed

2. **Task Orchestration**

   * Break high-level objectives into actionable tasks
   * Route tasks to appropriate agents or tools
   * Monitor execution and recover from failure

3. **Delegation via Agents**

   * Spawn specialized agents for bounded tasks
   * Enforce scopes, permissions, and lifetimes
   * Reassign or terminate agents as needed

4. **Tool & Actuator Interfaces**

   * Execute real actions via APIs, scripts, services, or physical devices
   * Treat mobility and robotics as **pluggable actuators**, not core logic

5. **Authority & Safety**

   * All actions are permission-gated
   * All executions are logged and auditable
   * No implicit or silent side effects

---

## Mental Model

```
Inputs / Sensors
       ↓
 ┌─────────────────┐
 │      GISMO      │
 │ Orchestration   │
 │ + State + Rules │
 └─────────────────┘
    ↓      ↓      ↓
 Agents  Tools  Actuators
```

GISMO **does not act directly**.
It **coordinates systems that act**.

---

## Design Principles

* Orchestration before intelligence
* State before behavior
* Execution before conversation
* Simple primitives over complex frameworks
* Explicit boundaries over implicit magic

---

## Initial Scope (MVP)

The first milestone focuses on **digital-only orchestration**, including:

* Core state model
* Task routing and execution pipeline
* Minimal agent abstraction
* Tool interface with logging and permissions

No robotics, voice interfaces, or mobility components are included in the initial MVP.

---

## Roadmap (High-Level)

* v0: Core orchestration engine with persistent state
* v1: Multi-agent delegation and tool execution
* v2: External system integrations (APIs, services)
* v3: Optional physical actuators (mobility as plugin)

---

## Status

🚧 Early architecture phase.
Expect breaking changes until core abstractions stabilize.

---

## Philosophy

Most AI systems **talk**.
GISMO is built to **operate**.

---

## Quickstart

**Python:** 3.11+

Run the demo workflow:

```bash
python -m gismo.cli.main demo
```

If `policy/readonly.json` exists and `--policy` is not provided, the CLI defaults to that readonly policy.

Run the demo workflow with a policy:

```bash
python -m gismo.cli.main demo --policy policy/dev.json
```

Run the dependency graph demo:

```bash
python -m gismo.cli.main demo-graph
```

Run operator commands:

```bash
python -m gismo.cli.main run "echo: hello"
python -m gismo.cli.main run "note: remember this"
python -m gismo.cli.main run "graph: echo A -> note B -> echo C"
```

Show a detailed run summary:

```bash
python -m gismo.cli.main run show <RUN_ID>
```

Run operator commands with a policy:

```bash
python -m gismo.cli.main run --policy policy/dev.json "echo: hello"
```

Daemon mode (queue + headless execution):

```bash
python -m gismo.cli.main enqueue "echo: daemon hello" --db .gismo/state.db
python -m gismo.cli.main daemon --once --policy policy/readonly.json --db .gismo/state.db
```

Local IPC control plane (same-machine only, token required):

```bash
export GISMO_IPC_TOKEN="your-token"
python -m gismo.cli.main --db .gismo/state.db ipc queue-stats
python -m gismo.cli.main ipc serve --db .gismo/state.db
python -m gismo.cli.main ipc enqueue "echo: hello"
python -m gismo.cli.main ipc ping
python -m gismo.cli.main ipc queue-stats
python -m gismo.cli.main ipc daemon-status
python -m gismo.cli.main ipc daemon-pause
python -m gismo.cli.main ipc daemon-resume
python -m gismo.cli.main ipc purge-failed
python -m gismo.cli.main ipc requeue-stale --older-than-minutes 10 --limit 25
python -m gismo.cli.main ipc run-show <RUN_ID>
```

Local supervisor (IPC + daemon in one terminal):

```bash
export GISMO_IPC_TOKEN="your-token"
python -m gismo.cli.main supervise up --db .gismo/state.db
python -m gismo.cli.main supervise status --db .gismo/state.db
python -m gismo.cli.main supervise down --db .gismo/state.db
```

Note: `supervise status` reconciles IPC reachability and daemon status with the PID file; the PID file is best-effort metadata only.

Install the Windows Task Scheduler entry for an always-on daemon:

```bash
python -m gismo.cli.main daemon install-windows-task --db .gismo/state.db
python -m gismo.cli.main daemon install-windows-task --db .gismo/state.db --name "GISMO Daemon" --force
python -m gismo.cli.main daemon install-windows-task --db .gismo/state.db --on-startup
```

Note: `--on-startup` may require running PowerShell as Administrator.

If Task Scheduler is blocked by policy, install a per-user Startup launcher instead:

```bash
python -m gismo.cli.main daemon install-windows-startup --db .gismo/state.db
python -m gismo.cli.main daemon install-windows-startup --db .gismo/state.db --name "GISMO Daemon" --force
```

Remove the Windows Task Scheduler entry:

```bash
python -m gismo.cli.main daemon uninstall-windows-task --name "GISMO Daemon" --yes
```

Remove the Windows Startup launcher:

```bash
python -m gismo.cli.main daemon uninstall-windows-startup --name "GISMO Daemon" --yes
```

Inspect queue items (DB flag can be supplied before or after the queue subcommand):

```bash
python -m gismo.cli.main queue stats --db .gismo/state.db
python -m gismo.cli.main queue list --db .gismo/state.db --limit 10 --json
python -m gismo.cli.main queue show --db .gismo/state.db <QUEUE_ITEM_ID>
python -m gismo.cli.main queue purge-failed --db .gismo/state.db
python -m gismo.cli.main queue purge-failed --db .gismo/state.db --yes
```

Export a run audit trail as JSONL:

```bash
python -m gismo.cli.main export --run <RUN_ID> --format jsonl --out exports/<RUN_ID>.jsonl
python -m gismo.cli.main export --latest --format jsonl
```

Expected behavior:
* Creates a run and two tasks (echo, write_note)
* Echo succeeds immediately
* write_note fails on first attempt due to permissions, then succeeds after being allowed
* Outputs a summary of tasks and tool calls
* Tool execution records retry attempts and idempotency skips in state

## Developer Commands

GISMO is a **Python-only** project. Use the Makefile helpers:

```bash
make demo
make demo-graph
make test
```

## Validation

Required verification:

```bash
python scripts/verify.py
```

Optional checks:

```bash
make test
make demo
make demo-graph
```

## Policies

* `policy/readonly.json`: default readonly policy if no `--policy` is provided.
* `policy/dev-safe.json`: dev-safe policy allowing `run_shell` with a minimal allowlist.

## Operator Commands

GISMO supports deterministic operator-like commands that map to tasks and tools. The CLI only allows the tools needed for each command.

* Echo (routes to `echo` tool)

  ```bash
  python -m gismo.cli.main run "echo: status check"
  ```

* Note (routes to `write_note` tool)

  ```bash
  python -m gismo.cli.main run "note: rotating credentials on Friday"
  ```

* Graph (one-line chain with dependencies)

  ```bash
  python -m gismo.cli.main run "graph: echo A -> note B -> echo C"
  ```

## Daemon Mode

Queue commands for headless execution and run the daemon to process them:

```bash
python -m gismo.cli.main enqueue "echo: daemon hello" --db /var/lib/gismo/gismo.db
python -m gismo.cli.main daemon --once --policy policy/readonly.json --db /var/lib/gismo/gismo.db
```

Daemon runs always enforce policies; keep policies least-privilege and explicitly allow only the tools you need.

## Run as a service (systemd)

See [deploy/systemd/README.md](deploy/systemd/README.md) for production-safe systemd units, hardening defaults, and steps to install a dedicated service user with a stable database path.

## Toolpack Policy & Safety

GISMO ships with a minimal local toolpack (filesystem + restricted shell) that is deny-by-default and policy-gated. Policies are JSON files that explicitly allow tools and define safety boundaries.
If `policy/readonly.json` exists, the CLI will auto-load it unless you pass `--policy` explicitly. Use `--policy policy/dev.json` to opt into the development policy.

Example policy (`policy/dev.json`):

```json
{
  "allowed_tools": ["echo", "write_note", "read_file", "write_file", "list_dir", "run_shell"],
  "fs": { "base_dir": "." },
  "shell": {
    "base_dir": ".",
    "allowlist": [["git", "status"], ["python", "-m", "unittest", "-v"], ["make", "test"]]
  }
}
```

Safety notes:
* Filesystem tools are restricted to the configured base directory (default is the repo root).
* Shell commands must be exact allowlist matches and are executed without a shell, with a default timeout.
* No network calls are added by the built-in toolpack.

## Decisions (v0 scope)

* Core state, task lifecycle, agent execution, and permission gating are implemented with standard library tools.
* Persistence uses SQLite via the `sqlite3` module for auditability and portability.
* Tool calls are idempotent by key + normalized input hash, with retry semantics and a failure taxonomy stored in state.
* Task dependency graphs are persisted on tasks and executed via a scheduler that respects dependency ordering.
