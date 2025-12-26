# Architecture

This document describes the **structural architecture** of GISMO: the core abstractions, execution model, and control-plane boundaries. It is intentionally implementation-aware but **non-procedural** (no CLI usage).

---

## System Overview

At its core, GISMO is a **stateful orchestration engine** that coordinates task execution through explicit queues, agents, and tools, with all actions recorded in persistent state.

```
┌────────────┐
│    CLI     │
└─────┬──────┘
      │
      ▼
┌────────────┐        IPC (local, authenticated)
│  IPC Server│◄──────────────────────────┐
└─────┬──────┘                           │
      │                                  │
      ▼                                  │
┌────────────┐        supervise          │
│   Daemon   │◄───────────────┐          │
└─────┬──────┘                │          │
      │                        │          │
      ▼                        ▼          │
┌────────────────────────────────────────┐
│               StateStore                │
│        (SQLite: runs, tasks, audit)     │
└───────────────┬────────────────────────┘
                ▼
        Tools / Actuators
```

GISMO itself **does not execute actions directly**.
It schedules, authorizes, and records actions executed by tools.

---

## Core Abstractions

### Run

A **Run** is the top-level execution context for an orchestration attempt.

* Represents a single operator intent or workflow
* Owns tasks, dependencies, and final outcome
* Acts as the audit boundary

---

### Task

A **Task** is a unit of work with explicit lifecycle state.

* Has dependencies (`depends_on`)
* Transitions through states: queued → running → succeeded / failed / blocked
* Is idempotent at the state level

Tasks **do not execute logic themselves**.
They describe *what* must be done, not *how*.

---

### ToolCall

A **ToolCall** is an immutable, auditable execution record.

* Captures:

  * Tool name
  * Inputs
  * Normalized input hash
  * Outputs or error
  * Timing and retries
* Enables:

  * Idempotency
  * Replay prevention
  * Forensic audit

---

### Tool

A **Tool** is a permission-gated actuator.

* Deterministic inputs → deterministic outputs
* Deny-by-default
* Policy-governed
* Side effects are explicit and logged

Examples:

* `echo`
* `write_note`
* `run_shell` (restricted)

---

### Agent

An **Agent** is an execution delegate.

* Evaluates task readiness
* Invokes tools on behalf of the orchestrator
* Operates within:

  * Policy constraints
  * Tool allowlists
  * Scoped permissions

Agents do **not** own state.

---

### Orchestrator

The **Orchestrator** coordinates execution.

Responsibilities:

* Determines runnable tasks
* Enforces dependency ordering
* Dispatches tasks to agents
* Handles failure propagation
* Declares completion or deadlock

The orchestrator is **pure coordination logic**.

---

### StateStore

The **StateStore** is the system of record.

* Backed by SQLite
* Stores:

  * Runs
  * Tasks
  * ToolCalls
  * Audit metadata
* Provides:

  * Transactional safety
  * Deterministic recovery
  * Postmortem analysis

State is **explicit and authoritative**.

---

### Daemon Heartbeat

The daemon persists a **heartbeat** row in SQLite at a fixed interval.

* Captures the daemon PID, start time, and last-seen timestamp
* Serves as the authoritative health signal for status checks
* Complements PID files, which are best-effort metadata

---

## Execution Model

### Task Graph Scheduling Rules

* Tasks declare dependencies explicitly
* A task is runnable when:

  * All dependencies succeeded
  * The task is non-terminal
* Failures propagate by policy:

  * Dependents may block or fail
* Scheduler iterates until:

  * All tasks are terminal, or
  * No runnable tasks remain

A **deadlock** is declared when tasks are non-terminal but unrunnable.

---

## Control Plane vs Data Plane

GISMO separates **control** from **execution**:

### Control Plane

* CLI
* IPC server
* Supervisor
* Policies
* Permissions

### Data Plane

* Daemon
* Task execution
* Tool invocation

This separation enables:

* Headless execution
* Safe remote control (local IPC)
* Supervisor-managed lifecycles

---

## IPC Model (Local Only)

* IPC is **same-machine only**
* Authenticated via shared token
* Endpoint is derived deterministically from the database path
* Used for:

  * Queue inspection
  * Enqueueing tasks
  * Daemon control (pause/resume/status)

IPC **never bypasses policy**.

---

## Supervisor Model

The supervisor is a **process coordinator**, not a scheduler.

* Starts/stops IPC and daemon together
* Tracks PIDs best-effort (metadata, not authority)
* Reconciles:

  * IPC reachability
  * Daemon state
  * Pause status

The supervisor exists to make GISMO **operable**, not intelligent.

---

## Audit & Permission Model

* Every tool invocation is recorded
* No implicit permissions
* No silent execution
* All mutations flow through the StateStore

If an action cannot be explained after the fact, it is a bug.

---

## Design Constraints (Intentional)

* Orchestration before intelligence
* State before behavior
* Execution before conversation
* Explicit boundaries over implicit automation
* Operability over convenience

---

## Non-Goals

* Conversational interfaces
* Autonomous decision-making
* Network-exposed control planes
* UI-first workflows
* Implicit side effects

---

## Summary

GISMO is an **operating core**, not an assistant.

It exists to **coordinate systems that act**, with:

* Explicit state
* Auditable execution
* Enforced authority boundaries

Everything else is intentionally out of scope.

---
