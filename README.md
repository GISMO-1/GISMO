# GISMO

**General Intelligent System for Multi-flow Operations**

---

## Overview

GISMO is a **persistent orchestration runtime** designed to **execute, coordinate, and supervise work** across tools, agents, and processes.

GISMO is not a chatbot.
GISMO is not a personality.
GISMO is not a UI product.

GISMO is an **operator-grade system core**.

It maintains durable state, enforces authority, executes actions, and exposes a deterministic control plane for managing work.

---

## What GISMO Is

GISMO provides:

* **Persistent state** for tasks, runs, tools, and outcomes
* **Deterministic execution** via a queue + daemon model
* **Local IPC control plane** for same-machine control
* **Supervisor orchestration** for managing IPC + daemon lifecycles
* **Strict policy-gated tool execution**
* **Full auditability** of every action taken

GISMO is designed to be embedded inside larger systems as the **execution and coordination layer**.

---

## What GISMO Is Not

GISMO explicitly does **not** attempt to be:

* A conversational assistant
* A general AI or AGI system
* A UI-first application
* A robotics framework
* A speculative research project

Anything that does not serve **orchestration, execution, delegation, or state** is out of scope.

---

## Mental Model

```
Inputs / Triggers
        ↓
 ┌───────────────────┐
 │       GISMO       │
 │  State + Rules    │
 │  Orchestration    │
 └───────────────────┘
     ↓        ↓
   Agents    Tools
```

GISMO **does not act directly**.
It **coordinates systems that act**.

---

## Core Capabilities

### 1. Persistent State

* SQLite-backed state for runs, tasks, queue items, and tool calls
* Explicit tracking of lifecycle: queued → running → succeeded / failed
* Idempotent execution with retry semantics

### 2. Task Orchestration

* Deterministic task scheduling
* Dependency-aware execution graphs
* Failure classification and recovery

### 3. Headless Execution (Daemon)

* Background daemon processes queued work
* Always policy-enforced
* Safe to run unattended

### 4. Local IPC Control Plane

* Same-machine IPC using:

  * Unix sockets (POSIX)
  * Named pipes (Windows)
* Token-based authentication
* Full control over:

  * Queue inspection
  * Daemon pause/resume
  * Maintenance actions

### 5. Supervisor

* Single command to start IPC + daemon together
* PID tracking (best-effort metadata)
* Windows-safe lifecycle handling
* Designed for long-running environments

### 6. Authority & Safety

* All tool execution is policy-gated
* Deny-by-default toolpack
* All actions logged and auditable
* No silent side effects

---

## Status

🟢 **Core runtime stabilized**

The following components are complete and working on Windows:

* CLI
* Queue + daemon
* IPC control plane
* Supervisor
* Windows-safe process, pipe, and shutdown handling

Breaking changes going forward are limited to **higher-level orchestration features**, not the runtime core.

---

## Quickstart

### Requirements

* Python **3.11+**
* No external dependencies

Activate your virtual environment and verify:

```bash
python scripts/verify.py
```

---

## Basic Operator Commands

```bash
python -m gismo.cli.main run "echo: hello"
python -m gismo.cli.main run "note: remember this"
python -m gismo.cli.main run "graph: echo A -> note B -> echo C"
```

Show a run summary:

PowerShell note: `<` and `>` are redirection operators. Replace placeholders without angle brackets (e.g., use `RUN_ID`).

```bash
python -m gismo.cli.main run show RUN_ID
```

---

## Queue & Daemon

Enqueue work:

```bash
python -m gismo.cli.main enqueue "echo: daemon hello" --db .gismo/state.db
python -m gismo.cli.main enqueue --timeout 30 --retries 2 "echo: daemon hello" --db .gismo/state.db
```

Run the daemon once:

```bash
python -m gismo.cli.main daemon --once --db .gismo/state.db
```

Inspect the queue:

```bash
python -m gismo.cli.main queue stats --db .gismo/state.db
python -m gismo.cli.main queue list --db .gismo/state.db
python -m gismo.cli.main queue show QUEUE_ITEM_ID --db .gismo/state.db
python -m gismo.cli.main queue cancel QUEUE_ITEM_ID --db .gismo/state.db
```

Cancellation requests for in-progress items are best-effort; the daemon checks between steps.

---

## IPC Control Plane (Local Only)

Set a token (required):

```bash
export GISMO_IPC_TOKEN="your-token"
```

Start the IPC server:

```bash
python -m gismo.cli.main ipc serve --db .gismo/state.db
```

Control the system:

```bash
python -m gismo.cli.main ipc ping --db .gismo/state.db
python -m gismo.cli.main ipc daemon-status --db .gismo/state.db
python -m gismo.cli.main ipc daemon-pause --db .gismo/state.db
python -m gismo.cli.main ipc daemon-resume --db .gismo/state.db
python -m gismo.cli.main ipc enqueue "echo: hello" --db .gismo/state.db
python -m gismo.cli.main ipc enqueue --timeout 30 --retries 2 "echo: hello" --db .gismo/state.db
python -m gismo.cli.main ipc queue-cancel QUEUE_ITEM_ID --db .gismo/state.db
```

### Windows Note

On Windows, the IPC named pipe is **derived from the database path**.
You **must** use the same `--db` value for:

* `ipc serve`
* All IPC client commands
* `supervise`

---

## Supervisor (Recommended)

Run IPC + daemon together:

```bash
export GISMO_IPC_TOKEN="your-token"
python -m gismo.cli.main supervise up --db .gismo/state.db
```

Check status:

```bash
python -m gismo.cli.main supervise status --db .gismo/state.db
```

Stop everything:

```bash
python -m gismo.cli.main supervise down --db .gismo/state.db
```

The supervisor reconciles:

* IPC reachability
* Daemon state
* PID metadata (best-effort)
* Heartbeat freshness (source of truth)

---

## Policies

GISMO uses JSON policies to explicitly allow tools.

* `policy/readonly.json` — default if no policy is provided
* `policy/dev-safe.json` — development allowlist

Example:

```json
{
  "allowed_tools": ["echo", "write_note", "run_shell"],
  "fs": { "base_dir": "." },
  "shell": {
    "base_dir": ".",
    "allowlist": [["git", "status"], ["python", "-m", "unittest", "-v"]]
  }
}
```

---

## Toolpack Safety

* Filesystem access is scoped
* Shell commands are exact-match allowlisted
* No implicit networking
* All executions are logged

---

## Roadmap

* **v0** — Core orchestration runtime ✅
* **v1** — Multi-agent delegation & advanced workflows (in progress)
* **v2** — External system integrations
* **v3** — Optional physical actuators (plugin-based)

---

## Philosophy

Most AI systems **talk**.
GISMO is built to **operate**.

Deterministic. Auditable. Controlled.
