# AGENTS.md — Codex Operating Manual for GISMO

This repository builds **GISMO**: a persistent AI orchestration core (brain + nervous system).  
Codex must behave like an exacting infrastructure engineer, not a chatty assistant.

---

## 0) Prime Directive
GISMO is **orchestration + state + delegation + execution**.

Anything that does not directly serve those four pillars is out of scope unless explicitly requested.

---

## 1) What to Build (v0 Scope)
Codex should prioritize:

- Persistent state storage (auditable, queryable)
- Task lifecycle management
- Tool/actuator interfaces (permission-gated)
- Agent abstraction (delegation/execution)
- Orchestrator that ties state + agent + tools together
- Logging/audit trail for every action

**Not in v0:**
- Robotics / mobility
- Voice UI
- Web dashboards
- OpenAI API integrations (stubs only if needed)
- Fancy frameworks or heavy dependencies

---

## 2) Non-Negotiables
### 2.1 Auditability
Every significant action must be recorded with:
- Timestamp
- Actor (agent/tool name)
- Inputs
- Outputs
- Status (success/failure)
- Error details (if any)

### 2.2 Permission Gating
All tools/actuators are **deny-by-default**.  
A policy must explicitly allow any tool name before use.

### 2.3 Determinism by Default
Prefer deterministic behavior and pure functions.
Avoid hidden side effects.

---

## 3) Repo Conventions
### 3.1 Language & Dependencies
- Python 3.11+
- Standard library preferred
- If third-party libraries are proposed, Codex must justify them and get approval in the PR description.

### 3.2 Code Style
- Use `dataclasses` + `typing`
- Keep modules small and focused
- Fail fast with explicit exceptions
- Use clear, minimal docstrings where they add clarity

### 3.3 File Structure (Expected)
At minimum, keep core code in `gismo/` and tests in `tests/`.

---

## 4) Change Management Rules
### 4.1 Small, Reviewable PRs
- One cohesive change set per PR
- No drive-by refactors
- No formatting-only churn

### 4.2 No Silent Breakage
- If behavior changes, update tests and README quickstart if relevant.

### 4.3 Backwards Compatibility (Early Phase)
Breaking changes are allowed early, but they must be:
- Explicitly documented in the PR description
- Reflected in the demo workflow/output

---

## 5) Testing Requirements
- Add or update tests for new behavior.
- Use `unittest` unless explicitly instructed otherwise.
- Include at least one smoke test validating end-to-end orchestration behavior.

---

## 5.1) Validation
- Required: `python scripts/verify.py`
- Optional: `make test`, `make demo`, `make demo-graph`
- Do not run cargo/npm commands; this repo is Python-only.

---

## 6) Documentation Requirements
### 6.1 README
README must remain truthful and minimal.  
If Codex adds a new command or demo, update Quickstart.

### 6.2 Inline Guidance
Avoid large design essays in code.
Put architectural notes in `docs/` only if requested.

---

## 7) How Codex Should Communicate in PRs
Each PR description must include:
- **What changed**
- **Why it changed**
- **How to test** (exact commands)
- **Risks / limitations**

---

## 8) Definition of Done (for any task)
A task is done when:
- Code compiles/runs
- Tests pass
- Demo still works (if applicable)
- Permissions/audit trail are preserved
- Documentation updated as needed

---

## 9) Security / Secrets
- Do not commit secrets.
- Add `.env.example` placeholders when needed.
- Prefer local-only defaults.

---

## 10) Default Build Target
The default “proof of life” for GISMO is a CLI demo that:
- Creates a run
- Creates tasks
- Executes tools through an agent
- Persists state
- Prints an auditable summary

Leashed autonomy:
- The `agent` CLI loop must only plan → enqueue → execute via the queue/daemon and keep confirmation gates intact.
- Agent memory context injection and suggestion application are optional, gated behaviors; defaults remain read-only unless explicitly enabled.

Agent roles:
- Agent roles provide operator-defined identities tied to memory profiles.
- Roles are sequential and do not add parallel execution or autonomy.
- Creating/retiring roles requires policy allowance plus explicit confirmation.

If a change breaks this, it is not acceptable without explicit approval.
