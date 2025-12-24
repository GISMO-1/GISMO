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

## Decisions (v0 scope)

* Core state, task lifecycle, agent execution, and permission gating are implemented with standard library tools.
* Persistence uses SQLite via the `sqlite3` module for auditability and portability.
* Tool calls are idempotent by key + normalized input hash, with retry semantics and a failure taxonomy stored in state.
* Task dependency graphs are persisted on tasks and executed via a scheduler that respects dependency ordering.
