# Architecture

## Core Abstractions

- **Run**: The top-level execution context for an orchestration attempt.
- **Task**: A unit of work with dependencies and lifecycle state.
- **ToolCall**: An auditable invocation record for a tool execution.
- **Agent**: A delegated executor that evaluates and runs tasks.
- **Tool**: A permission-gated actuator with deterministic inputs/outputs.
- **StateStore**: Persistence layer for runs, tasks, tool calls, and audits.
- **Orchestrator**: Coordinator that schedules tasks, invokes agents, and enforces rules.

## Task Graph Scheduling Rules

- Tasks define dependencies via `depends_on`.
- A task is runnable when **all dependencies are completed successfully** and the task is not terminal.
- Failures propagate by marking dependent tasks as blocked or failed according to policy.
- The scheduler iterates runnable tasks until no progress is made.
- A deadlock is declared when tasks remain non-terminal but none are runnable.

## Audit + Permission Model

- Every tool call is recorded with timestamp, actor, inputs, outputs, and status.
- Tools are **deny-by-default**; explicit policy grants are required before execution.
- State mutations are persisted through the `StateStore` to preserve an auditable trail.
