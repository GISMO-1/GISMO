# GISMO Objectives, Progress Log, and Next Steps

**Last updated:** 2026-03-22
**Environment:** Windows 11, Python 3.14.x
**Repo:** `GISMO-1/GISMO` (branch: `main`)
**Local path:** `D:\repos\gismo`
**Virtualenv:** `.venv`

---

## 1) Project Goal

Build a **persistent, operator-grade orchestration runtime** that can:

* Accept structured operator commands and natural language requests
* Persist runs, tasks, tool calls, and queue state
* Execute work headlessly and safely with zero-trust capability tokens
* Be paused, resumed, inspected, and audited at runtime
* Manage memory (facts, preferences, procedures) with policy-gated access
* Run reliably on Windows-first environments

GISMO is an **execution control plane** with operator-facing surfaces (TUI, web API, chat).

---

## 2) Phase Summary

### Phase 1 -- Execution Spine (COMPLETE)

* State store, task graph, orchestrator, tool registry
* SQLite-backed persistence with WAL mode
* CLI `--db` flag placement, queue inspection

### Phase 2 -- CLI and Daemon (COMPLETE)

* `ask`, `agent`, `daemon`, queue management, TUI
* IPC control plane (local, token-authenticated)
* Supervisor lifecycle (up/status/down)
* Windows reliability gate: ShellTool builtins, SQLite locking

### Phase 3 -- Memory Layer (COMPLETE)

* Memory store with namespaces, profiles, injection, selection traces
* Summarize, explain, snapshot (export/import/diff), doctor (check/repair)
* Retention rules, tombstoning, trust metadata, provenance tracking
* Policy-gated memory access (deny-by-default)

### Phase 4 -- Operator Surfaces (COMPLETE except Task Scheduler)

* Web API with deterministic chat routing (calendar/device/conversational/operational)
* TTS with kokoro (primary, 11 voices) and piper (fallback, 5 voices)
* Calendar tool, device control, network policy
* Model policy with deterministic chat routing and fallback chains
* Onboarding system, plugin runtime
* **Open:** Always-on Windows service via Task Scheduler integration

### Phase 5 -- Zero-Trust Execution (COMPLETE, landed in 700ea9f)

* Signed capability tokens (HMAC-SHA256) for every tool execution
* Deny-by-default policy gating on all tool calls
* Full audit trail via security_events table
* Capability claims: subject, action, resource, constraints, TTL, run/task/plan scoping
* Input-hash binding: tokens bound to exact tool input payloads
* Token verification at orchestrator execution time

### Phase 6 -- Operator Readiness Surfaces (IN PROGRESS)

* Unified runtime status builder with state machine
* Readiness stages: state, worker, setup, model, API
* Model health probing (cached and full modes)
* Queue health monitoring with stale-running detection
* **Open:** `starting` state can persist indefinitely after daemon crash

---

## 3) Closed Objectives (Do Not Revisit)

* CLI `--db` flag placement (global + subcommand-safe)
* Queue inspection UX
* SQLite lifecycle correctness
* IPC authentication and authorization
* Pause/resume semantics
* Supervisor lifecycle coordination
* Windows IPC reliability
* Memory layer completeness (no stubs or TODOs)

---

## 4) Non-Goals (Explicit)

* Remote/networked IPC
* Robotics or physical actuators
* Autonomous policy mutation
* Multi-user/multi-tenant operation

---

## 5) Developer Notes

* PowerShell is not bash -- avoid POSIX assumptions
* SQLite requires strict connection hygiene on Windows (WinError 32)
* IPC endpoints must be derived consistently from `--db`
* Supervisor PID data is diagnostic only
* Policies must remain deny-by-default
* Capability tokens must be verified before every tool execution
* Memory operations require policy hash for audit trails
