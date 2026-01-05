# MEMORY_CONTRACT.md

## Purpose

GISMO Memory provides **persistent, queryable, auditable state** that can be used to:

- Improve continuity across runs (“what happened last time?”)
- Persist operator preferences and constraints
- Store stable facts and procedures that inform planning/execution

Memory is a **state subsystem**, not an intelligence subsystem.

---

## Non-Goals (Explicit)

Memory is not:

- A conversational chat history
- A hidden context store automatically fed to the LLM
- A freeform “knowledge base” written by the model without consent
- A replacement for audit logs (memory may summarize audits; audits remain canonical)

---

## Definitions

- **Memory Item**: A durable record stored in SQLite.
- **Namespace**: A scope boundary that prevents key collisions and enables selective retrieval.
- **Kind**: A type classifier that encodes intent (fact/preference/procedure/etc.).
- **Tombstone**: A soft-delete marker preserving auditability.
- **Memory Event**: An auditable record of memory operations (put/get/search/delete).

---

## Memory Classes

### 1) Working Memory (Run-Scoped)
- Namespace form: `run:<RUN_ID>`
- Intended for short-lived context relevant to a specific run/goal.
- Default behavior: **may expire** (TTL encouraged).

### 2) Persistent Memory (Long-Term)
- Namespace forms:
  - `global`
  - `project:<NAME>` (optional)
- Intended for durable facts, preferences, constraints, and procedures.

---

## Memory Item Schema (Logical)

A Memory Item MUST contain:

- `id` (UUID)
- `namespace` (string; required)
- `key` (string; required)
- `kind` (enum; required)
- `value` (JSON-serializable; required)
- `tags` (array[string]; optional)
- `confidence` (enum; required)
- `source` (enum; required)
- `ttl_seconds` (int|null; optional)
- `is_tombstoned` (bool; required)
- `created_at` (timestamp; required)
- `updated_at` (timestamp; required)

### Required uniqueness
- Unique constraint: `(namespace, key)`.

---

## Enumerations

### `kind` (required)
One of:

- `fact` — stable truth claim (“Ollama URL is …”)
- `preference` — operator preference (“default model = phi3:mini”)
- `constraint` — hard boundary (“never run destructive shell commands”)
- `procedure` — repeatable steps (“how to export latest run”)
- `note` — human note, not necessarily stable
- `summary` — compressed representation of prior runs/tasks/audits

### `confidence` (required)
One of: `high`, `medium`, `low`

Rules:
- `low` confidence items MUST NOT be used for autonomous execution decisions.
- `confidence` does not grant authority; policy does.

### `source` (required)
One of: `operator`, `system`, `llm`

Rules:
- `llm` source MUST be treated as untrusted unless confirmed or system-derived.

---

## Operations (Primitives)

### `memory.put` (write/update)
Creates or updates a Memory Item.

Required inputs:
- `namespace`, `key`, `kind`, `value`, `confidence`, `source`, `tags?`, `ttl_seconds?`

Behavior:
- Upserts by `(namespace, key)`.
- Writes MUST create a Memory Event.
- Persistent namespaces (`global`, `project:*`) SHOULD require confirmation by default (policy-dependent).

### `memory.get` (read)
Fetches the single item matching `(namespace, key)`.

Behavior:
- Returns tombstoned items only if `--include-tombstoned` is set (CLI).
- Writes a Memory Event (read audit) with minimal metadata.

### `memory.search` (query)
Searches items by:
- full-text query across `key` and serialized `value`
- optional filters: `namespace`, `kind`, `tag`, `source`, `confidence>=`, `include_tombstoned`

Behavior:
- Must be deterministic given the same DB state.
- Writes a Memory Event.

### `memory.delete` (tombstone)
Marks item as tombstoned.

Behavior:
- Sets `is_tombstoned = true`
- Preserves record for auditability
- Writes a Memory Event

Hard-delete is an explicit non-goal for v0.1.

---

## SQLite Tables (Proposed)

### Table: `memory_items`
- `id` TEXT PRIMARY KEY
- `namespace` TEXT NOT NULL
- `key` TEXT NOT NULL
- `kind` TEXT NOT NULL
- `value_json` TEXT NOT NULL
- `tags_json` TEXT NULL
- `confidence` TEXT NOT NULL
- `source` TEXT NOT NULL
- `ttl_seconds` INTEGER NULL
- `is_tombstoned` INTEGER NOT NULL DEFAULT 0
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

Indexes:
- UNIQUE(`namespace`, `key`)
- INDEX(`namespace`)
- INDEX(`kind`)
- INDEX(`is_tombstoned`)

Optional (if using FTS later):
- FTS virtual table keyed to `key` + `value_json`

### Table: `memory_events`
- `id` TEXT PRIMARY KEY
- `timestamp` TEXT NOT NULL
- `operation` TEXT NOT NULL (`put|get|search|delete`)
- `actor` TEXT NOT NULL (`operator|system|llm`)
- `policy_hash` TEXT NOT NULL
- `request_json` TEXT NOT NULL
- `result_meta_json` TEXT NOT NULL
- `related_run_id` TEXT NULL
- `related_ask_event_id` TEXT NULL

Indexes:
- INDEX(`timestamp`)
- INDEX(`operation`)
- INDEX(`actor`)
- INDEX(`related_run_id`)

---

## CLI Contract (User-Facing)

### `gismo memory put`
Example:
```bash
python -m gismo.cli.main memory put \
  --namespace global \
  --key default_model \
  --kind preference \
  --value '"phi3:mini"' \
  --confidence high \
  --source operator \
  --tag llm --tag defaults
```

Rules:

* `--value` MUST accept either:

  * valid JSON string (recommended), OR
  * `--value-text` shortcut that stores as JSON string
* CLI MUST print the resulting item header (namespace/key/kind/updated_at).

### `gismo memory get`

```bash
python -m gismo.cli.main memory get --namespace global default_model
```

Rules:

* Default excludes tombstoned items.
* `--json` prints full JSON.

### `gismo memory search`

```bash
python -m gismo.cli.main memory search "phi3" --namespace global --kind preference
```

Rules:

* Output defaults to a compact table.
* `--json` prints array of items.
* Must support `--limit` and deterministic ordering (e.g., `updated_at DESC, key ASC`).

### `gismo memory delete`

```bash
python -m gismo.cli.main memory delete --namespace global default_model
```

Rules:

* Tombstone by default.
* Must print a clear confirmation of tombstoning.
* Non-interactive mode must fail closed if confirmation required by policy.

---

## Audit Requirements

Every memory operation MUST create a `memory_events` record including:

* policy hash
* actor
* operation
* request parameters (bounded size)
* result metadata (e.g., hit count, id of affected item)
* linkage to run/ask where applicable

Audit entries must be sufficient to answer:

* Who wrote this?
* Under which policy?
* When was it read and by whom?
* What was changed?

---

## Determinism Requirements

Memory operations must be deterministic given the same DB state and inputs:

* `memory.search` ordering is fixed
* `memory.put` upsert behavior is explicit
* `memory.get` returns single canonical item

---

## Security & Policy Hooks (Deferred, But Required)

This contract defines the hooks, not the policy file structure:

* Writes to persistent namespaces SHOULD require confirmation
* Writes originating from `llm` SHOULD be denied or require confirmation unless explicitly enabled
* Deletes SHOULD require confirmation for persistent namespaces

Policy enforcement remains the source of truth.

---

## Implementation Checklist (Definition of Done for v0.1)

* [ ] SQLite tables created + migrations
* [ ] `memory put/get/search/delete` CLI implemented
* [ ] Full unit tests (Windows as source of truth)
* [ ] Memory events audited for every operation
* [ ] Deterministic search ordering
* [ ] Tombstone behavior verified
* [ ] No planner auto-injection; reads/writes are explicit
