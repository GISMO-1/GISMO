# GISMO

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

GISMO is a local-first orchestration system for day-to-day operator work.

It keeps state in SQLite, routes actions through a durable queue and daemon, gates side effects with policy, and records receipts and audit events. Desktop, web, TUI, and CLI surfaces all sit on top of that same execution core.

The first real local device path is now in place: GISMO has a device adapter architecture, and a Feit Electric smart bulb on the Tuya platform has been controlled over the local LAN with `tinytuya`. The live smoke test that has been verified so far is `get_state`, `turn_off`, and `turn_on` through the Tuya adapter/runtime path.

## What GISMO Is

- Local-first orchestration core
- SQLite-backed state, queue, daemon, and exports
- Policy-controlled execution with capability checks and audit trails
- Persistent memory and settings
- Desktop app, web dashboard, CLI, and TUI surfaces
- Extensible device adapter layer for LAN devices

## What GISMO Can Do Today

- Run immediate or queued work through the same auditable execution path
- Plan and gate operational work instead of acting silently
- Keep persistent memory, settings, receipts, runs, and event history
- Serve desktop, web, chat, and terminal operator surfaces
- Discover and inspect saved devices on the local network
- Switch configured saved lights on and off through `device_control` when local device details are present
- Control a real Feit/Tuya bulb locally through the Tuya adapter

## Current Device Status

- Tuya / Feit is the primary live adapter path
- Kasa remains present as a secondary compatibility adapter
- Tuya local credentials are loaded from `.gismo/devices.json`
- Shared device routing now uses `device_ref` as the lookup token
- Resolved vendor `device_id` remains the protocol identity
- IP address is treated as a locator and fallback, not canonical identity

What is proven today:

- Direct Tuya adapter/runtime smoke on a real bulb for `get_state`, `turn_off`, and `turn_on`
- Saved-device on/off routing through the generic adapter architecture in targeted tests
- Mocked and targeted test coverage for discovery, brightness, color temperature, RGB, policy gating, security events, and trust-zone execution

What is not finished yet:

- No first-party editor for `.gismo/devices.json`
- Richer light controls are not yet broadly exposed through the normal operator/chat flow
- The ideal "say it and it acts" device experience still needs more live end-to-end validation
- Scenes, groups, and higher-level home automation flows are not complete

## Quick Start

### Prerequisites

- Python 3.11+
- Ollama for local model-backed planning/chat
- Windows is the reference platform

### Install

```bash
git clone https://github.com/GISMO-1/GISMO.git
cd GISMO
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

pip install -e .
```

### Launch

```bash
# Desktop app
gismo app

# Web dashboard
gismo web

# Terminal dashboard
gismo tui

# CLI ask flow
gismo ask "What can you do?" --dry-run
```

## Device Setup

For Tuya / Feit local control, keep device credentials in:

```text
.gismo/devices.json
```

Expected fields per Tuya device:

- `device_id`
- `local_key`
- `ip`
- `version`

Keep that file local. Do not commit secrets.

See [docs/DEVICE_ADAPTERS.md](docs/DEVICE_ADAPTERS.md) for the adapter contract, config format, and `tinytuya` credential flow.

## Operator Surfaces

- `gismo app`: desktop window
- `gismo web`: local web dashboard and chat surface
- `gismo tui`: terminal dashboard
- `gismo run`, `gismo ask`, `gismo agent`, `gismo queue`, `gismo export`: CLI/operator entry points

Chat -> plan/queue -> execute remains the strategic front door. Device requests are wired into that broader flow for scan, list, check, and simple on/off requests, but the live physical proof for the first bulb milestone is the adapter/runtime path rather than a polished end-to-end chat demo.

## Project State

- Phase 1-4 core execution, memory, and operator surfaces: done
- Phase 5 zero-trust execution: done
- Phase 5b device adapter architecture: landed
- Phase 5b first live device milestone: real Feit/Tuya bulb controlled locally
- Phase 6 operator readiness surfaces: in progress

See [STATUS.md](STATUS.md) for the current source-of-truth status and [HANDOFF.md](HANDOFF.md) for maintainer context.

## Docs

- [docs/OPERATOR.md](docs/OPERATOR.md)
- [docs/DEVICE_ADAPTERS.md](docs/DEVICE_ADAPTERS.md)
- [STATUS.md](STATUS.md)
- [HANDOFF.md](HANDOFF.md)

## License

MIT License.
