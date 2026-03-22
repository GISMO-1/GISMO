"""Formal trust-zone definitions for GISMO runtime components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ZONE_CORE_TRUSTED = "core_trusted"
ZONE_MODEL_RUNTIME = "model_runtime"
ZONE_TOOL_EXECUTION = "tool_execution"
ZONE_DEVICE_ADAPTER = "device_adapter"
ZONE_EXTERNAL_INGRESS = "external_ingress"
ZONE_PLUGIN_RUNTIME = "plugin_runtime"

EXECUTION_MODE_IN_PROCESS = "in_process"
EXECUTION_MODE_ISOLATED_SUBPROCESS = "isolated_subprocess"
EXECUTION_MODE_SANDBOXED = "sandboxed"
EXECUTION_MODE_FUTURE_SANDBOX = EXECUTION_MODE_SANDBOXED

STATE_ACCESS_NONE = "none"
STATE_ACCESS_CONTROLLED = "controlled_interface"
STATE_ACCESS_DIRECT = "direct_handle"

BOUNDARY_TRUSTED_CORE = "trusted_core"
BOUNDARY_LOGICAL = "logical_only"
BOUNDARY_PROCESS = "process_isolated"
BOUNDARY_SANDBOX = "constrained_subprocess"
BOUNDARY_FUTURE = "future_work"


@dataclass(frozen=True)
class TrustZoneDefinition:
    zone: str
    semantics: str
    boundary_kind: str
    notes: str

    def to_dict(self) -> dict[str, str]:
        return {
            "zone": self.zone,
            "semantics": self.semantics,
            "boundary_kind": self.boundary_kind,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ComponentTrustBinding:
    component: str
    zone: str
    default_execution_mode: str
    state_access: str
    boundary_kind: str
    notes: str

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "zone": self.zone,
            "default_execution_mode": self.default_execution_mode,
            "state_access": self.state_access,
            "boundary_kind": self.boundary_kind,
            "notes": self.notes,
        }


_ZONE_DEFINITIONS: dict[str, TrustZoneDefinition] = {
    ZONE_CORE_TRUSTED: TrustZoneDefinition(
        zone=ZONE_CORE_TRUSTED,
        semantics=(
            "Trusted orchestration and persistent state. Capability checks, memory policy, "
            "quarantine state, receipts, and security events live here."
        ),
        boundary_kind=BOUNDARY_TRUSTED_CORE,
        notes="This zone is authoritative for trust decisions and state transitions.",
    ),
    ZONE_MODEL_RUNTIME: TrustZoneDefinition(
        zone=ZONE_MODEL_RUNTIME,
        semantics=(
            "Model invocation and response handling. Model output is observed but not trusted "
            "for execution or memory promotion by default."
        ),
        boundary_kind=BOUNDARY_LOGICAL,
        notes=(
            "Model requests use subprocess isolation. Python transport runs in a constrained "
            "sandboxed worker profile and curl transport uses subprocess isolation."
        ),
    ),
    ZONE_TOOL_EXECUTION: TrustZoneDefinition(
        zone=ZONE_TOOL_EXECUTION,
        semantics=(
            "Higher-risk tool execution surfaces, especially shell-like command execution."
        ),
        boundary_kind=BOUNDARY_PROCESS,
        notes=(
            "Shell execution is process-separated from the trusted core through a JSON worker "
            "boundary. This is isolation-by-process, not an OS sandbox."
        ),
    ),
    ZONE_DEVICE_ADAPTER: TrustZoneDefinition(
        zone=ZONE_DEVICE_ADAPTER,
        semantics=(
            "LAN-facing device discovery and control paths. Network scope is policy-controlled "
            "and deny-by-default."
        ),
        boundary_kind=BOUNDARY_SANDBOX,
        notes=(
            "Device discovery and control run through a constrained subprocess sandbox profile "
            "for the network-touching runtime path."
        ),
    ),
    ZONE_EXTERNAL_INGRESS: TrustZoneDefinition(
        zone=ZONE_EXTERNAL_INGRESS,
        semantics=(
            "Imported or externally sourced content entering GISMO. Content may be seen and "
            "quarantined without being trusted."
        ),
        boundary_kind=BOUNDARY_LOGICAL,
        notes="Ingress trust is mediated through quarantine and trust-label promotion.",
    ),
    ZONE_PLUGIN_RUNTIME: TrustZoneDefinition(
        zone=ZONE_PLUGIN_RUNTIME,
        semantics=(
            "Plugin and adapter loading/execution surfaces. Signatures and signer trust are "
            "required before use."
        ),
        boundary_kind=BOUNDARY_SANDBOX,
        notes=(
            "Manifest verification is enforced and runtime execution uses a constrained "
            "subprocess sandbox profile."
        ),
    ),
}

_COMPONENT_BINDINGS: dict[str, ComponentTrustBinding] = {
    "state_store": ComponentTrustBinding(
        component="state_store",
        zone=ZONE_CORE_TRUSTED,
        default_execution_mode=EXECUTION_MODE_IN_PROCESS,
        state_access=STATE_ACCESS_DIRECT,
        boundary_kind=BOUNDARY_TRUSTED_CORE,
        notes="Authoritative persistent state.",
    ),
    "memory_store": ComponentTrustBinding(
        component="memory_store",
        zone=ZONE_CORE_TRUSTED,
        default_execution_mode=EXECUTION_MODE_IN_PROCESS,
        state_access=STATE_ACCESS_DIRECT,
        boundary_kind=BOUNDARY_TRUSTED_CORE,
        notes="Trusted memory selection and retention logic.",
    ),
    "orchestrator": ComponentTrustBinding(
        component="orchestrator",
        zone=ZONE_CORE_TRUSTED,
        default_execution_mode=EXECUTION_MODE_IN_PROCESS,
        state_access=STATE_ACCESS_DIRECT,
        boundary_kind=BOUNDARY_TRUSTED_CORE,
        notes="Capability verification and receipt issuance.",
    ),
    "run_shell": ComponentTrustBinding(
        component="run_shell",
        zone=ZONE_TOOL_EXECUTION,
        default_execution_mode=EXECUTION_MODE_ISOLATED_SUBPROCESS,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_PROCESS,
        notes="Shell commands are executed in a worker subprocess with marshaled JSON input/output.",
    ),
    "device_control": ComponentTrustBinding(
        component="device_control",
        zone=ZONE_DEVICE_ADAPTER,
        default_execution_mode=EXECUTION_MODE_SANDBOXED,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_SANDBOX,
        notes="Device network actions run in a constrained subprocess with marshaled device payloads.",
    ),
    "web_device_discovery": ComponentTrustBinding(
        component="web_device_discovery",
        zone=ZONE_DEVICE_ADAPTER,
        default_execution_mode=EXECUTION_MODE_SANDBOXED,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_SANDBOX,
        notes="Web device discovery uses the same constrained subprocess runtime as device tools.",
    ),
    "ollama_client": ComponentTrustBinding(
        component="ollama_client",
        zone=ZONE_MODEL_RUNTIME,
        default_execution_mode=EXECUTION_MODE_SANDBOXED,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_SANDBOX,
        notes="Python transport is sandboxed; curl transport remains isolated_subprocess.",
    ),
    "quarantine_ingress": ComponentTrustBinding(
        component="quarantine_ingress",
        zone=ZONE_EXTERNAL_INGRESS,
        default_execution_mode=EXECUTION_MODE_IN_PROCESS,
        state_access=STATE_ACCESS_CONTROLLED,
        boundary_kind=BOUNDARY_LOGICAL,
        notes="Ingress only becomes memory-relevant through explicit review or policy-mediated writes.",
    ),
    "plugin_loader": ComponentTrustBinding(
        component="plugin_loader",
        zone=ZONE_PLUGIN_RUNTIME,
        default_execution_mode=EXECUTION_MODE_SANDBOXED,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_SANDBOX,
        notes="Plugin manifest verification and runtime execution use a constrained subprocess profile.",
    ),
    "plugin_runtime": ComponentTrustBinding(
        component="plugin_runtime",
        zone=ZONE_PLUGIN_RUNTIME,
        default_execution_mode=EXECUTION_MODE_SANDBOXED,
        state_access=STATE_ACCESS_NONE,
        boundary_kind=BOUNDARY_SANDBOX,
        notes="Verified plugins execute in a constrained subprocess with marshaled payloads only.",
    ),
}


def list_trust_zones() -> list[TrustZoneDefinition]:
    return [_ZONE_DEFINITIONS[name] for name in sorted(_ZONE_DEFINITIONS)]


def get_trust_zone(zone: str) -> TrustZoneDefinition:
    try:
        return _ZONE_DEFINITIONS[zone]
    except KeyError as exc:
        raise KeyError(f"Unknown trust zone: {zone}") from exc


def list_component_bindings() -> list[ComponentTrustBinding]:
    return [_COMPONENT_BINDINGS[name] for name in sorted(_COMPONENT_BINDINGS)]


def get_component_binding(component: str) -> ComponentTrustBinding:
    try:
        return _COMPONENT_BINDINGS[component]
    except KeyError as exc:
        raise KeyError(f"Unknown trust-zone component: {component}") from exc


def describe_trust_zones() -> dict[str, Any]:
    zones = [zone.to_dict() for zone in list_trust_zones()]
    components = [binding.to_dict() for binding in list_component_bindings()]
    return {
        "zones": zones,
        "components": components,
    }
