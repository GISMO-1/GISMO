# ASK_CONTRACT.md

## Purpose

`gismo ask` is a **planning** interface that converts an operator’s natural-language intent into a **bounded, auditable, deterministic** plan composed of GISMO primitives (enqueue/run of supported operator commands) **without executing anything** during planning.

This document is the contract for what `ask` **must** do, **must not** do, and what artifacts it must produce so runs are replayable, explainable, and safe.

---

## Definitions

- **Operator**: The human user invoking GISMO.
- **Ask**: The planning phase that produces a plan (or a denial) from a prompt.
- **Plan**: A structured object describing intent, assumptions, actions, and risk metadata.
- **Action**: A single proposed unit of work, typically an `enqueue` of an operator command.
- **Policy**: The active permission model that determines which actions are permitted.
- **Confirmation**: An explicit operator acknowledgement required for risky or policy-flagged plans.
- **Hermetic**: A plan/result that is stable and reproducible given the same inputs (prompt, policy, toolset).

---

## Non-Negotiable Safety Rules

### Ask must never execute work
During `gismo ask`, GISMO **MUST NOT**:
- Execute tools (no filesystem writes, no shell execution, no network calls, no daemon actions).
- Enqueue work unless the operator explicitly requested enqueue and confirmation rules are satisfied.
- Modify the state database except to write **audit events** and optional **plan artifacts**.

`ask` is advisory. Execution happens only via `enqueue/run/daemon`.

### Ask must be deterministic and auditable
Given the same:
- operator prompt,
- system prompt template version,
- policy file contents,
- tool registry / allowed tools,
- LLM model identifier and config,
- GISMO version,

the resulting **validated plan object** must be stable (or changes must be attributable in audit logs to version/config differences).

---

## Inputs

`gismo ask` accepts:

1. **Operator Prompt** (required)
   - A natural language request, e.g. “clean up temp files”, “summarize last failed run”.

2. **Context Inputs** (optional)
   - `--db` path (state store location)
   - `--policy` path (policy file)
   - `--model`, `--url`, `--timeout` (LLM configuration)
   - `--dry-run` / `--yes` / `--confirm` style flags (operator confirmation behavior)
   - Any additional flags that influence planning must be captured in audit

---

## Outputs (Guaranteed Shapes)

### Console output (human-facing)
`ask` prints a summary that includes:
- Selected LLM model + URL + timeout
- A plan header (`=== GISMO LLM Plan ===`)
- Intent
- Assumptions (if any)
- Actions (if any), each with:
  - type (enqueue/run suggestion)
  - command string (operator command)
  - timeout_seconds
  - retries
  - risk level
  - why
- Notes (validation notes, policy notes, limits)
- Confidence (HIGH/MEDIUM/LOW)
- Risk flags (zero or more)
- Explanation (brief, grounded)

### Machine plan object (internal / exportable)
The validated plan object must be representable as JSON and include, at minimum:

- `schema_version` (string)
- `intent` (string)
- `assumptions` (array of strings)
- `actions` (array of objects)
- `notes` (array of strings)
- `confidence` (HIGH/MEDIUM/LOW)
- `risk_flags` (array of strings)
- `explanation` (string)

Each action object must include:
- `action_type` (e.g., `enqueue`, `run` — `ask` should prefer `enqueue` unless explicitly requested)
- `command` (string operator command, e.g. `shell: git status`)
- `timeout_seconds` (int)
- `retries` (int)
- `risk` (low/medium/high)
- `why` (string)
- `idempotency_key` (optional but recommended for stable repeats)

---

## The Only Three Outcomes

`gismo ask` MUST resolve into exactly one of these outcomes:

1. **APPROVED**
   - A valid plan exists.
   - No policy denial is present.
   - Risk is acceptable under current confirmation rules.

2. **REQUIRES_CONFIRMATION**
   - A valid plan exists, but one or more conditions require explicit operator confirmation:
     - high risk actions
     - write actions under restricted policy
     - too_many_steps (exceeds step limit)
     - destructive intent tokens detected
     - includes shell actions (depending on policy)
     - any other configured risk gate

3. **DENIED**
   - No valid plan can be produced or plan violates policy in a way that cannot be confirmed/overridden.
   - The denial must be explicit and explain *why*.

No other outcomes are allowed. No partial execution. No silent fallback.

---

## Confirmation Rules

### Default behavior
- `ask` should behave as **dry-run by default**: generate the plan and print it.
- If the operator requested enqueue (or `ask` is running in “queue mode”), GISMO must:
  - require confirmation for **REQUIRES_CONFIRMATION**
  - allow enqueue without extra confirmation for **APPROVED**
  - refuse enqueue for **DENIED**

### Confirmation mechanisms
Acceptable confirmation mechanisms include:
- interactive prompt (Y/N)
- `--yes` override flag
- `--non-interactive` mode:
  - must fail closed (do not enqueue) if confirmation would be required

---

## What Must Never Happen in Ask

During `ask`, GISMO must not:
- Execute `run_shell`
- Execute file-writing tools
- Call network-fetch tools
- Start or control daemons
- Mutate queue state by adding tasks unless the operator explicitly requested enqueue and confirmation gates are satisfied
- “Auto-expand” permissions/policies based on LLM suggestions

---

## Validation Requirements

Before printing or returning a plan, GISMO must:

1. **Validate schema**
   - Required fields present
   - Types correct
   - No unexpected top-level keys unless explicitly allowed

2. **Normalize**
   - Coerce legacy or synonymous action types into canonical forms
   - Drop ungrounded assumptions if policy requires it

3. **Assess risk**
   - Identify destructive tokens / dangerous intent
   - Flag write actions
   - Flag shell actions
   - Flag too many actions (limit is a contract parameter; default 12)
   - Derive confidence

4. **Apply policy**
   - Mark actions as permitted/denied based on current policy
   - Include policy denial notes in plan `notes`
   - Ensure outcome is computed consistently:
     - policy-denied + non-overridable -> DENIED
     - policy-denied + overridable -> REQUIRES_CONFIRMATION (if override mechanism exists)

---

## Audit Requirements

Every `ask` invocation must write an audit event capturing:

- timestamp
- GISMO version
- command line args relevant to ask
- db path
- policy path + policy content hash
- tool registry version / allowed tools
- LLM config:
  - url
  - model
  - timeout
  - transport
  - any keep-alive behavior
- raw model response (optional; if stored, must be size-limited and redactable)
- extracted candidate plan text
- validated plan JSON
- outcome (APPROVED / REQUIRES_CONFIRMATION / DENIED)
- whether enqueue was requested
- whether enqueue occurred
- operator confirmation result (accepted/declined/non-interactive)

Audit must allow later reconstruction of:
- “What did the operator ask?”
- “What did the model propose?”
- “What did GISMO validate/deny and why?”
- “What was enqueued (if anything)?”

---

## Enqueue Mapping (Ask → Queue)

If enqueue occurs, it must be mechanically derived from the validated plan:

- For each action with `action_type == "enqueue"`:
  - create a queue item with the exact command string
  - persist action metadata (risk, why, idempotency info)
  - preserve plan/run linkage by storing `plan_id` or `ask_event_id` references

If a plan contains unsupported action types:
- GISMO must ignore them, document the ignore in `notes`,
- and the outcome should reflect lowered confidence.

---

## Limits

### Action limit
- Default maximum actions in a plan: **12**
- Plans exceeding this must set:
  - risk flag: `too_many_steps`
  - outcome: `REQUIRES_CONFIRMATION` (never silently truncate)
  - note recommending batching

### Redaction and size limits
- Audit fields containing:
  - file contents
  - shell output
  - raw LLM response
must be redactable and size-bounded.

---

## Examples

### Example 1: Safe, read-only
Prompt: “Show me my git status.”

Expected plan:
- intent: queue
- actions:
  - enqueue: `shell: git status`
- outcome: APPROVED (if allowlisted), otherwise REQUIRES_CONFIRMATION/DENIED based on policy

### Example 2: Too many steps
Prompt: “Write notes step 0 through step 12.”

Expected plan:
- 13 actions -> too_many_steps flagged
- outcome: REQUIRES_CONFIRMATION
- note: “consider batching into 12 or fewer steps”

### Example 3: Destructive intent
Prompt: “Cleanup by deleting everything in root.”

Expected plan:
- destructive intent flagged
- outcome: DENIED (or REQUIRES_CONFIRMATION only if explicitly supported and policy allows)
- explanation must be explicit

---

## Contract Stability

Any change to:
- plan schema
- default limits
- outcome computation
- confirmation behavior
- audit fields

must be documented as a versioned update to this contract and referenced in CHANGELOG/Handoff.

---

## Checklist (for contributors)

- [ ] Ask does not execute tools
- [ ] Plan schema is validated and normalized
- [ ] Exactly one outcome is produced
- [ ] Confirmation gates are enforced
- [ ] Audit is complete and reproducible
- [ ] Enqueue mapping is mechanical and traceable
