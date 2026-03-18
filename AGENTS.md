# GISMO project instructions



## Mission

GISMO is a local-first personal AI assistant for normal users.

This is a user-facing product, not a developer toy.



## Product rules

- The UI must feel simple and obvious to non-technical users.

- Avoid developer jargon in visible UX copy.

- Do not mention policies, audit trails, database structure, or internal plumbing in the interface.

- Prefer plain language, short labels, and direct feedback.

- Reliability beats cleverness.



## Repository context

- Main repo path: `D:\\repos\\GISMO`

- Virtual environment: `.venv`



## Key code areas

- `gismo/web/server.py` — HTTP server and route wiring

- `gismo/web/api.py` — API behavior

- `gismo/web/templates.py` — command center UI

- `gismo/tts/` — Kokoro text-to-speech

- `gismo/llm/ollama.py` — local LLM integration

- `gismo/core/state.py` — SQLite-backed state

- `gismo/onboarding.py` — first-run setup



## Working rules

- Match existing code patterns already used in the repository.

- Make the smallest defensible change that solves the problem.

- Avoid broad rewrites unless they are clearly necessary.

- Keep unrelated files untouched.

- When debugging, identify the real failing path before changing code.

- When behavior is uncertain, inspect the actual code path, logs, requests, and runtime output first.



## Validation rules

- Always test by running the relevant GISMO flow after changes.

- For app or web UI work, run the product and verify the changed behavior directly.

- Check the browser console for JavaScript errors before finishing UI work.

- Confirm API endpoints, UI state, and visible behavior all agree before considering work complete.

- Do not claim something works unless it was actually verified.



## Priority order

1. Make the feature actually function end to end.

2. Preserve a clean, intuitive user experience.

3. Keep fixes narrow and maintainable.

4. Validate the result in the real running product.



## Subagent policy

- Use read-only exploration agents first when the failure mode is not yet understood.

- Use implementation agents only after the code path is mapped.

- Keep reviewer agents read-only.

- Use one subagent per distinct concern.

- Do not let research agents edit code.

