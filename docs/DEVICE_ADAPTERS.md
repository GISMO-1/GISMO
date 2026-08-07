# GISMO Device Adapter Architecture

## Overview

GISMO already has a generic device adapter layer. The core pieces are:

- `DeviceAdapter` in `gismo/core/device_adapters/base.py`
- `AdapterRegistry` in `gismo/core/device_adapters/registry.py`
- sandboxed execution in the `device_adapter` trust zone
- policy gating, capability verification, security events, and tool receipts in the existing zero-trust pipeline

This pass keeps that architecture and makes Tuya/Feit the first real local device integration.

## Current Adapters

### Tuya / Feit (primary path)

`TuyaAdapter` uses `tinytuya` for local WiFi control of Feit Electric bulbs on the Tuya platform.

Supported commands:

- `turn_on`
- `turn_off`
- `set_brightness` with a 0-100 percentage
- `set_color_temp` with either a 0-100 percentage or a preset
- `set_color_rgb` with `r`, `g`, `b` values
- `get_state`

Supported presets for `set_color_temp`:

- `warm`
- `soft_white`
- `neutral`
- `cool_white`
- `daylight`

The adapter prefers configured known devices first and only treats LAN discovery as best-effort.

### Kasa (secondary compatibility path)

`KasaAdapter` remains registered for TP-Link Kasa devices. It is still available, but it is no longer the only adapter-specific command path in shared control code.

## Verification Status

Proven live on physical hardware:

- Direct local control of a real Feit Electric / Tuya bulb over LAN
- Live smoke verified for `get_state`, `turn_off`, and `turn_on`

Covered in tests and mocked integration:

- Tuya discovery merge behavior
- Brightness, color temperature, and RGB command handling
- Saved-device power routing through the shared runtime/tool path
- Basic operator/chat light routing for configured lights: power, brightness percentages, white temperature, basic colors, and simple combinations
- Policy gating, security-event emission, and `device_adapter` trust-zone execution

Not yet broadly re-verified live in this milestone:

- Brightness, color temperature, and RGB on the physical bulb, including through the direct adapter/runtime path
- The operator/chat/web and `Controls` light paths on the physical bulb

## Device Config

Tuya credentials are loaded from a sidecar config relative to the active GISMO database.

- Default GISMO DB: `.gismo/state.db`
- Default device config: `.gismo/devices.json`

If you point GISMO at a different DB, the device config is resolved next to that DB inside a sibling `.gismo/` directory.

Example config:

```json
{
  "devices": [
    {
      "name": "Dad's Room Light",
      "platform": "tuya",
      "device_type": "smart_bulb",
      "device_id": "bf1234567890abcdef12",
      "local_key": "0123456789abcdef",
      "ip": "192.168.1.188",
      "version": 3.3
    }
  ]
}
```

Expected fields per Tuya device:

- `device_id`
- `local_key`
- `ip`
- `version`

Optional fields:

- `name`
- `platform`
- `device_type`
- `gismo_device_id`

Do not hardcode secrets in Python source. Keep `devices.json` local and out of commits.

## Identity Model

GISMO now keeps three device concepts separate in shared control code:

- `gismo_device_id`: GISMO's own saved-device identity in local metadata when available
- vendor `device_id`: the protocol or cloud-derived identity used by the adapter, such as Tuya's real device ID
- IP address: a network locator, not a canonical identity

Shared adapter/runtime command paths use `device_ref` for the incoming lookup token. That `device_ref` can be a vendor device ID, a GISMO device ID, a saved label, or an IP address. For saved-device light control, GISMO prefers stable references before it falls back to IP.

## Getting Tuya Credentials

Use the `tinytuya` wizard to pull the device details from your Tuya cloud account and local network:

```powershell
.\.venv\Scripts\python.exe -m tinytuya wizard
```

Useful wizard notes:

- `wizard` writes `devices.json` by default if you let it.
- `-device-file FILE` lets you choose a temporary output path.
- `-credentials-file FILE` lets you reuse saved Tuya cloud credentials.

For GISMO, copy the needed values into `.gismo/devices.json` using the format above:

- `id` or `gwId` -> `device_id`
- `key` -> `local_key`
- `ip` -> `ip`
- `version` -> `version`

## Command Routing

There are now three important device-control paths:

### 1. Saved-device control path

Example: "turn off Dad's room light"

1. A front-end request can be normalized into `tool=device_control`, `action=turn_off`, `target="Dad's room light"`
2. The orchestrator verifies the capability token for `device_control`
3. `DeviceControlTool._set_power()` resolves the saved device from SQLite
4. Private-network policy checks run before any LAN access
5. `execute_device_runtime_action()` starts the sandboxed `device` worker in the `device_adapter` trust zone
6. `runtime_set_power()` resolves the adapter from saved device metadata and `.gismo/devices.json`
7. Conversational and dashboard targeting resolves an exact stable GISMO `device_ref` first, or an exact unique alias. Vendor `device_id` and IP are not conversational identities; after canonical resolution they remain protocol identity and network locator candidates inside the adapter/runtime layer.
8. `runtime_device_command()` calls the selected adapter through `AdapterRegistry`
9. The adapter talks to the device and returns a `CommandResult`
10. GISMO records security events and issues the normal tool receipt

### 2. Explicit adapter command path

Example payload:

```json
{
  "action": "device_command",
  "adapter": "tuya",
  "device_ref": "tuya-bulb-1",
  "command": "set_brightness",
  "params": { "brightness": 25 }
}
```

`kasa_command` is still accepted as a compatibility alias, but the shared path is now `device_command`. Legacy ingress payloads that still send `device_id` are normalized to `device_ref` internally for compatibility.

### 3. Operator/chat light routing

Configured-light requests such as:

- `turn Dad's light on`
- `set Dad's light to cool white`
- `make Dad's light blue`
- `dim Dad's light to 20 percent`
- `set Dad's light to blue at 50 percent`

are now parsed into deterministic `device_control` steps that still flow through the same saved-device runtime path above. Combined requests are expanded in safe order: `turn_on` first when needed, then white/color mode, then brightness.

## Policy and Trust Guarantees

Device commands still keep the existing guardrails:

- capability token verification happens before the tool runs
- `device_control` stays policy-gated
- outbound LAN access still requires private-network permission
- the execution zone remains `device_adapter`
- sandboxed execution receipts still include mode and zone
- security events still capture command send, success, and failure

## What Is Still Missing

- No first-party UI yet for editing `.gismo/devices.json`
- Discovery remains best-effort and should not be treated as the source of truth for Tuya credentials
- Physical verification remains limited to direct adapter/runtime state and power smoke. Brightness, color temperature, RGB, operator/chat/web, and `Controls` light paths still need physical verification.
- Multi-device scenes, groups, and broader home-automation behavior are still out of scope for this slice
