# GISMO Status (source of truth)

Last updated: 2026-03-23

## Completed Phases

### Phase 1 -- Core execution engine
- State store, task graph, orchestrator, tool registry
- SQLite-backed persistence with WAL mode

### Phase 2 -- CLI and daemon
- `ask`, `agent`, `daemon`, queue management, TUI
- Windows reliability gate: `--db` propagation, ShellTool Windows builtins

### Phase 3 -- Memory layer
- Memory store with namespaces, profiles, injection, selection traces
- Summarize, explain, snapshot (export/import/diff), doctor (check/repair)
- Retention rules, tombstoning, trust metadata, provenance tracking

### Phase 4 -- Operator surfaces and tooling
- Web API with chat routing (deterministic calendar/device, conversational, operational)
- TTS with kokoro (primary) and piper (fallback) voice engines
- Calendar tool, device control, network policy
- Model policy with deterministic chat routing
- Onboarding system
- Plugin runtime
- Windows Task Scheduler service (`gismo service install/uninstall/status`), policy-gated with audit trail

### Phase 5 -- Zero-trust execution (landed in 700ea9f)
- Signed capability tokens (HMAC-SHA256) for every tool execution
- Deny-by-default policy gating on all tool calls
- Full audit trail via security_events table (capability_issued, capability_verified, capability_rejected)
- Capability claims: subject, action, resource, constraints, TTL, run/task/plan scoping
- Input-hash binding: capability tokens are bound to exact tool input payloads

### Phase 5b -- Device Adapter Architecture
- Pluggable adapter layer: `DeviceAdapter` ABC, `AdapterRegistry`, `AdapterInfo`, `CommandResult` dataclasses
- Tuya adapter (tinytuya): local Feit/Tuya bulb control for on/off, brightness, color temperature, RGB, and state
- Sidecar device config resolved from `.gismo/devices.json` relative to the active DB path
- Generic `device_command` action wired through `DeviceControlTool` -> sandboxed subprocess -> adapter registry
- Shared command lookup now uses `device_ref` internally so saved-device identity, vendor `device_id`, and IP locator stay distinct
- Saved-device `turn_on` / `turn_off` now resolve the adapter instead of calling tinytuya directly from shared runtime code
- Kasa adapter (python-kasa) retained as a secondary compatibility adapter
- Security events: `device_command_sent`, `device_command_succeeded`, `device_command_failed`
- Full audit trail through existing execution boundary (device_adapter trust zone, sandboxed mode)
- Deny-by-default policy gating preserved (outbound private network scope check)

### Concrete Milestone (2026-03-23)
- First successful local physical device control via Tuya adapter: a real Feit Electric / Tuya smart bulb was controlled over the local LAN with live `get_state`, `turn_off`, and `turn_on` smoke tests.

## Current Focus: Phase 6 -- Operator Readiness Surfaces

### Done
- `readiness.py`: unified runtime status builder with state machine (ready, starting, degraded, approval_needed, blocked, offline)
- Readiness stages: state, worker, setup, model, API
- Model health probing (cached and full modes)
- Startup event tracking (autostart success/failure/skip)
- Queue health monitoring with stale-running detection
- Background worker heartbeat integration
- `get_status` API returns `offline` when no daemon is running (was incorrectly `ready`)

### Open
- `starting` state can persist indefinitely if daemon crashed after initial heartbeat (readiness.py line 276-278)
- Model status defaults to `unknown` which allows `ready` state even when model is unreachable

## Test Status (2026-03-22, full suite: 451 passed, 1 skipped, 0 failed)

Test fixes applied:
- Capability token structure: tests updated to use `{"tool": "...", "payload": {...}}` format for `input_json`
- Windows WinError 32: `ignore_cleanup_errors=True` on TemporaryDirectory, gc.collect() before os.remove
- Chat routing: test expectations updated for auto-execute (LOW risk) vs pending plan (MEDIUM+ risk)
- Daemon state: test expectation updated from `ready` to `offline` for no-daemon case
- Snapshot hash: test `_item_hash` updated to include source_type, verification_status, trust_labels, provenance_json
- Doctor index detection: tests call doctor functions directly to avoid onboarding re-creating indexes
- Voice registry: test updated from 5 piper voices to 16 voices (11 kokoro + 5 piper)

## Device Adapter Validation (2026-03-23)

Proven live:

- Direct Tuya adapter/runtime control of a real Feit Electric / Tuya bulb
- Live smoke verified for `get_state`, `turn_off`, and `turn_on`

Proven in targeted tests and mocked integration:

- Tuya discovery merge behavior
- Brightness, color temperature, and RGB command handling
- Shared `device_control` / runtime / registry routing
- Policy gating, security events, receipts, and `device_adapter` trust-zone execution
- Legacy `device_id` ingress compatibility with internal `device_ref` normalization

Not yet re-verified live in this milestone:

- Brightness, color temperature, and RGB on the physical bulb
- Full saved-device orchestration smoke on the physical bulb beyond the direct adapter/runtime validation
- Full chat/web "say it and it acts" device smoke on the physical bulb

Targeted validation rerun for this pass:

- `tests.test_device_adapters`: 39 passed, 0 failed
- `tests.test_device_tool` + `tests.test_network_policy` + device-specific `tests.test_web_api` coverage: 11 passed, 0 failed
- Targeted total: 50 passed, 0 failed

Known note:

- One pre-existing `RuntimeWarning` about an unawaited mocked coroutine still appears during the targeted run. That warning was not expanded here.

## Next Highest-Value Work

- Re-run the real bulb through the normal saved-device orchestration path, not just the direct adapter/runtime smoke.
- Live-test brightness, color temperature, and RGB on the real bulb.
- Add a first-party way to create or edit `.gismo/devices.json` without manual file edits.
- Tighten the normal operator/chat device UX so the real adapter capability is easier to use safely.
