"""Connected device inspection and control."""
from __future__ import annotations

from typing import Any

from gismo.core.device_runtime import execute_device_runtime_action, serialize_device
from gismo.core.outbound import check_outbound_scope, check_outbound_target
from gismo.core.models import ConnectedDevice
from gismo.core.permissions import NetworkPolicy
from gismo.core.state import StateStore
from gismo.core.tools import Tool


class DeviceControlTool(Tool):
    def __init__(
        self,
        state_store: StateStore,
        network_policy: NetworkPolicy | None = None,
    ) -> None:
        super().__init__(
            name="device_control",
            description="Inspect and control saved connected devices",
            schema={"type": "object"},
        )
        self._state_store = state_store
        self._network_policy = network_policy

    def run(
        self,
        tool_input: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = str(tool_input.get("action") or "").strip().lower()
        target = str(tool_input.get("target") or "").strip()
        request = str(tool_input.get("request") or "").strip()
        execution_context = context or {}

        if action == "scan":
            return self._scan_network(
                request or target or "scan",
                context=execution_context,
            )
        if action == "list":
            return self._list_devices(context=execution_context)
        if action == "check":
            return self._check_devices(target, context=execution_context)
        if action in {"turn_on", "turn_off"}:
            return self._set_power(
                target,
                turn_on=action == "turn_on",
                context=execution_context,
            )
        raise ValueError(f"Unsupported device action '{action}'")

    def _scan_network(
        self,
        request: str,
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_private_network_scope()
        execution = execute_device_runtime_action(
            component="device_control",
            action="scan_network",
            actor=_context_actor(context),
            db_path=self._state_store.db_path,
            payload={
                "saved_devices": [
                    serialize_device(device)
                    for device in self._state_store.list_devices()
                ],
                "timeout_seconds": 10.0,
            },
            timeout_s=10.0,
            related_run_id=_context_optional_str(context, "related_run_id"),
            related_task_id=_context_optional_str(context, "related_task_id"),
            related_plan_id=_context_optional_str(context, "related_plan_id"),
        )
        results = list(execution.get("devices") or [])
        count = len(results)
        if count == 0:
            summary = "I scanned your network and did not find any devices yet."
        else:
            preview = ", ".join(_device_result_label(item) for item in results[:4])
            if count > 4:
                preview += f", and {count - 4} more"
            summary = f"I found {count} device{'s' if count != 1 else ''}: {preview}."
        self._state_store.record_event(
            actor="worker",
            event_type="device_scan",
            message=summary,
            json_payload={"request": request, "found": count},
        )
        return {
            "summary": summary,
            "found": count,
            "devices": results,
            "execution": dict(execution.get("execution") or {}),
        }

    def _list_devices(
        self,
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        devices = self._state_store.list_devices()
        if not devices:
            payload = {
                "summary": "You do not have any connected devices saved in GISMO yet.",
                "devices": [],
            }
            self._record("device_list", payload["summary"], {"count": 0})
            return payload
        self._require_private_network_scope()
        for device in devices:
            self._require_device_target(device)
        execution = execute_device_runtime_action(
            component="device_control",
            action="device_status",
            actor=_context_actor(context),
            db_path=self._state_store.db_path,
            payload={"devices": [serialize_device(device) for device in devices]},
            timeout_s=5.0,
            related_run_id=_context_optional_str(context, "related_run_id"),
            related_task_id=_context_optional_str(context, "related_task_id"),
            related_plan_id=_context_optional_str(context, "related_plan_id"),
        )
        details = list(execution.get("devices") or [])
        summary = _summarize_device_list(details)
        payload = {
            "summary": summary,
            "devices": details,
            "execution": dict(execution.get("execution") or {}),
        }
        self._record("device_list", summary, {"count": len(details)})
        return payload

    def _check_devices(
        self,
        target: str,
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        devices = self._resolve_devices(target)
        if not devices:
            payload = {
                "summary": f"I could not find a connected device matching {target or 'that request'}.",
                "devices": [],
            }
            self._record("device_check", payload["summary"], {"target": target, "matched": 0})
            return payload

        self._require_private_network_scope()
        for device in devices:
            self._require_device_target(device)
        execution = execute_device_runtime_action(
            component="device_control",
            action="device_status",
            actor=_context_actor(context),
            db_path=self._state_store.db_path,
            payload={"devices": [serialize_device(device) for device in devices]},
            timeout_s=5.0,
            related_run_id=_context_optional_str(context, "related_run_id"),
            related_task_id=_context_optional_str(context, "related_task_id"),
            related_plan_id=_context_optional_str(context, "related_plan_id"),
        )
        details = list(execution.get("devices") or [])
        label = _target_label(target, details)
        online = [item["name"] for item in details if item["status"] == "online"]
        offline = [item["name"] for item in details if item["status"] != "online"]

        parts = [f"I checked {label}."]
        if online:
            parts.append(f"Online: {_join_human(online)}.")
        if offline:
            parts.append(f"Offline: {_join_human(offline)}.")
        summary = " ".join(parts)
        payload = {
            "summary": summary,
            "devices": details,
            "execution": dict(execution.get("execution") or {}),
        }
        self._record("device_check", summary, {"target": target, "matched": len(details)})
        return payload

    def _set_power(
        self,
        target: str,
        *,
        turn_on: bool,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        devices = self._resolve_devices(target)
        if not devices:
            payload = {
                "summary": f"I could not find a connected light matching {target or 'that request'}.",
                "devices": [],
            }
            self._record(
                "device_power",
                payload["summary"],
                {"target": target, "matched": 0, "turn_on": turn_on},
            )
            return payload

        self._require_private_network_scope()
        for device in devices:
            self._require_device_target(device)
        execution = execute_device_runtime_action(
            component="device_control",
            action="device_power",
            actor=_context_actor(context),
            db_path=self._state_store.db_path,
            payload={
                "devices": [serialize_device(device) for device in devices],
                "turn_on": turn_on,
            },
            timeout_s=8.0,
            related_run_id=_context_optional_str(context, "related_run_id"),
            related_task_id=_context_optional_str(context, "related_task_id"),
            related_plan_id=_context_optional_str(context, "related_plan_id"),
        )
        details = list(execution.get("devices") or [])
        changed = list(execution.get("changed") or [])
        needs_setup = list(execution.get("needs_setup") or [])
        unsupported = list(execution.get("unsupported") or [])
        failed = list(execution.get("failed") or [])

        verb = "on" if turn_on else "off"
        parts: list[str] = []
        if changed:
            parts.append(f"I turned {verb} {_join_human(changed)}.")
        if needs_setup:
            parts.append(
                f"{_join_human(needs_setup)} needs a little more setup in GISMO before I can control it."
            )
        if unsupported:
            parts.append(
                f"I can check {_join_human(unsupported)}, but I cannot switch it on or off yet."
            )
        if failed:
            parts.append(f"I could not finish {verb} for {_join_human(failed)}.")
        if not parts:
            parts.append("I could not change any device right now.")
        summary = " ".join(parts)
        payload = {
            "summary": summary,
            "devices": details,
            "changed": changed,
            "needs_setup": needs_setup,
            "failed": failed,
            "execution": dict(execution.get("execution") or {}),
        }
        self._record(
            "device_power",
            summary,
            {
                "target": target,
                "turn_on": turn_on,
                "changed": len(changed),
                "needs_setup": len(needs_setup),
                "failed": len(failed),
            },
        )
        return payload

    def _resolve_devices(self, target: str) -> list[ConnectedDevice]:
        devices = self._state_store.list_devices()
        if not devices:
            return []
        normalized = _normalize_text(target)
        if normalized in {"", "devices", "all", "everything"}:
            return devices
        if normalized in {"camera", "cameras"}:
            return [device for device in devices if "camera" in device.device_type.lower()]
        if normalized in {"light", "lights", "lamp", "lamps", "bulb", "bulbs"}:
            return [device for device in devices if _looks_like_light(device)]

        matches = [device for device in devices if _device_matches(device, normalized)]
        if matches:
            return matches
        return [device for device in devices if normalized in _device_search_text(device)]

    def _record(self, event_type: str, message: str, payload: dict[str, Any]) -> None:
        self._state_store.record_event(
            actor="worker",
            event_type=event_type,
            message=message,
            json_payload=payload,
        )

    def _require_private_network_scope(self) -> None:
        if self._network_policy is None:
            return
        check_outbound_scope(
            component="device_control",
            scope="private",
            policy=self._network_policy,
            actor="worker",
            action="device_scan",
            db_path=self._state_store.db_path,
        )

    def _require_device_target(self, device: ConnectedDevice) -> None:
        if self._network_policy is None:
            return
        check_outbound_target(
            component="device_control",
            target=device.ip,
            policy=self._network_policy,
            actor="worker",
            action="device_connect",
            db_path=self._state_store.db_path,
        )


def _device_matches(device: ConnectedDevice, normalized_target: str) -> bool:
    if normalized_target == device.id.lower():
        return True
    fields = [
        _device_name(device),
        device.hostname or "",
        device.ip,
        device.brand,
        device.device_type,
    ]
    for field in fields:
        if _normalize_text(field) == normalized_target:
            return True
    return False


def _device_search_text(device: ConnectedDevice) -> str:
    return " ".join(
        _normalize_text(value)
        for value in (
            _device_name(device),
            device.hostname or "",
            device.ip,
            device.brand,
            device.device_type,
        )
        if value
    )


def _looks_like_light(device: ConnectedDevice) -> bool:
    text = _device_search_text(device)
    return any(token in text for token in ("light", "lamp", "bulb", "tuya", "feit"))


def _summarize_device_list(details: list[dict[str, Any]]) -> str:
    if not details:
        return "You do not have any connected devices saved in GISMO yet."
    names = [item["name"] for item in details[:4]]
    summary = f"You have {len(details)} connected device{'s' if len(details) != 1 else ''}: {_join_human(names)}."
    if len(details) > 4:
        summary = summary[:-1] + f", and {len(details) - 4} more."
    return summary


def _target_label(target: str, details: list[dict[str, Any]]) -> str:
    normalized = _normalize_text(target)
    if normalized in {"camera", "cameras"}:
        return f"{len(details)} camera{'s' if len(details) != 1 else ''}"
    if normalized in {"light", "lights", "lamp", "lamps", "bulb", "bulbs"}:
        return f"{len(details)} light{'s' if len(details) != 1 else ''}"
    if len(details) == 1:
        return details[0]["name"]
    return f"{len(details)} devices"


def _device_name(device: ConnectedDevice) -> str:
    label = device.metadata_json.get("label") if isinstance(device.metadata_json, dict) else None
    if isinstance(label, str) and label.strip():
        return label.strip()
    if device.hostname and device.hostname != device.ip:
        return device.hostname
    return f"{device.brand} {device.device_type}".strip() or device.ip


def _device_result_label(item: dict[str, Any]) -> str:
    name = str(item.get("hostname") or item.get("ip") or "device").strip()
    device_type = str(item.get("device_type") or "").strip()
    if device_type:
        return f"{name} ({device_type})"
    return name


def _join_human(items: list[str]) -> str:
    clean = [str(item).strip() for item in items if str(item).strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _context_actor(context: dict[str, Any]) -> str:
    return _context_optional_str(context, "actor") or "worker"


def _context_optional_str(context: dict[str, Any], field_name: str) -> str | None:
    value = context.get(field_name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
