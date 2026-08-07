"""Connected device inspection and control."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from gismo.core.device_runtime import execute_device_runtime_action, serialize_device
from gismo.core.execution import ExecutionBoundaryError
from gismo.core.outbound import check_outbound_scope, check_outbound_target
from gismo.core.models import ConnectedDevice, FailureType
from gismo.core.permissions import NetworkPolicy
from gismo.core.state import StateStore
from gismo.core.tools import Tool, ToolExecutionError


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
        if action in {"set_brightness", "set_color_temp", "set_color_rgb"}:
            params = tool_input.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            return self._set_light_command(
                target,
                command=action,
                params=params,
                context=execution_context,
            )
        if action in {"device_command", "kasa_command", "tuya_command"}:
            return self._device_command(
                tool_input,
                action=action,
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
            summary = f"I could not find a connected light matching {target or 'that request'}."
            event_id = self._record(
                "device_power",
                summary,
                {"target": target, "matched": 0, "turn_on": turn_on},
            )
            result = _build_device_command_result(
                action="turn_on" if turn_on else "turn_off",
                target=target,
                details=[],
                error_details=[summary],
                event_id=event_id,
                context=context,
            )
            raise ToolExecutionError(summary, result={"summary": summary, "device_command_result": result})

        self._require_private_network_scope()
        for device in devices:
            self._require_device_target(device)
        try:
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
        except ExecutionBoundaryError as exc:
            summary = "I could not finish that device command because the device runtime timed out or stopped."
            event_id = self._record(
                "device_power_failed",
                summary,
                {"target": target, "turn_on": turn_on, "reason": "execution_boundary"},
            )
            result = _build_device_command_result(
                action="turn_on" if turn_on else "turn_off",
                target=target,
                details=[{"id": device.id, "name": _device_name(device), "control": "failed"} for device in devices],
                error_details=[str(exc) or "Device runtime failed."],
                event_id=event_id,
                context=context,
            )
            raise ToolExecutionError(
                summary,
                result={"summary": summary, "device_command_result": result},
                failure_type=FailureType.TOOL_ERROR,
            ) from exc
        details = list(execution.get("devices") or [])
        changed = list(execution.get("changed") or [])
        needs_setup = list(execution.get("needs_setup") or [])
        unsupported = list(execution.get("unsupported") or [])
        failed = list(execution.get("failed") or [])
        details = _normalize_command_details(details, changed, needs_setup, unsupported, failed)

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
        event_id = self._record(
            "device_power",
            summary,
            {
                "target": target,
                "turn_on": turn_on,
                "changed": len(changed),
                "needs_setup": len(needs_setup),
                "failed": len(failed),
                "confirmed": sum(1 for detail in details if bool(detail.get("confirmed"))),
                "verified_states": _verified_states(details),
            },
        )
        result = _build_device_command_result(
            action="turn_on" if turn_on else "turn_off",
            target=target,
            details=details,
            error_details=failed,
            event_id=event_id,
            context=context,
        )
        payload["device_command_result"] = result
        if result["status"] == "failed":
            raise ToolExecutionError(
                summary,
                result=payload,
                failure_type=FailureType.TOOL_ERROR,
            )
        return payload

    def _set_light_command(
        self,
        target: str,
        *,
        command: str,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_target = str(target or "").strip()
        if not normalized_target:
            raise ValueError("target is required for light commands")
        devices = self._resolve_devices(normalized_target)
        if not devices:
            summary = f"I could not find a connected light matching {normalized_target or 'that request'}."
            event_id = self._record(
                "device_light_command",
                summary,
                {"target": normalized_target, "command": command, "matched": 0},
            )
            result = _build_device_command_result(
                action=command,
                target=normalized_target,
                details=[],
                error_details=[summary],
                event_id=event_id,
                context=context,
            )
            raise ToolExecutionError(summary, result={"summary": summary, "device_command_result": result})

        self._require_private_network_scope()
        for device in devices:
            self._require_device_target(device)
        try:
            execution = execute_device_runtime_action(
                component="device_control",
                action="device_target_command",
                actor=_context_actor(context),
                db_path=self._state_store.db_path,
                payload={
                    "devices": [serialize_device(device) for device in devices],
                    "command": command,
                    "params": params,
                },
                timeout_s=15.0,
                related_run_id=_context_optional_str(context, "related_run_id"),
                related_task_id=_context_optional_str(context, "related_task_id"),
                related_plan_id=_context_optional_str(context, "related_plan_id"),
            )
        except ExecutionBoundaryError as exc:
            summary = "I could not finish that light command because the device runtime timed out or stopped."
            event_id = self._record(
                "device_light_command_failed",
                summary,
                {"target": normalized_target, "command": command, "reason": "execution_boundary"},
            )
            result = _build_device_command_result(
                action=command,
                target=normalized_target,
                details=[{"id": device.id, "name": _device_name(device), "control": "failed"} for device in devices],
                error_details=[str(exc) or "Device runtime failed."],
                event_id=event_id,
                context=context,
            )
            raise ToolExecutionError(
                summary,
                result={"summary": summary, "device_command_result": result},
                failure_type=FailureType.TOOL_ERROR,
            ) from exc
        details = list(execution.get("devices") or [])
        changed = list(execution.get("changed") or [])
        needs_setup = list(execution.get("needs_setup") or [])
        unsupported = list(execution.get("unsupported") or [])
        failed = list(execution.get("failed") or [])
        details = _normalize_command_details(details, changed, needs_setup, unsupported, failed)

        change_phrase = _light_change_phrase(command, params)
        failure_phrase = _light_failure_phrase(command, params)
        parts: list[str] = []
        if changed:
            parts.append(f"I set {_join_human(changed)} {change_phrase}.")
        if needs_setup:
            parts.append(
                f"{_join_human(needs_setup)} needs a little more setup in GISMO before I can control it."
            )
        if unsupported:
            parts.append(
                f"I can check {_join_human(unsupported)}, but I cannot apply that light setting yet."
            )
        if failed:
            parts.append(f"I could not {failure_phrase} for {_join_human(failed)}.")
        if not parts:
            parts.append("I could not change any light right now.")
        summary = " ".join(parts)
        payload = {
            "summary": summary,
            "devices": details,
            "changed": changed,
            "needs_setup": needs_setup,
            "failed": failed,
            "execution": dict(execution.get("execution") or {}),
        }
        event_id = self._record(
            "device_light_command",
            summary,
            {
                "target": normalized_target,
                "command": command,
                "params": params,
                "changed": len(changed),
                "needs_setup": len(needs_setup),
                "failed": len(failed),
                "confirmed": sum(1 for detail in details if bool(detail.get("confirmed"))),
                "verified_states": _verified_states(details),
            },
        )
        result = _build_device_command_result(
            action=command,
            target=normalized_target,
            details=details,
            error_details=failed,
            event_id=event_id,
            context=context,
        )
        payload["device_command_result"] = result
        if result["status"] == "failed":
            raise ToolExecutionError(
                summary,
                result=payload,
                failure_type=FailureType.TOOL_ERROR,
            )
        return payload

    def _device_command(
        self,
        tool_input: dict[str, Any],
        *,
        action: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        default_adapter = "kasa" if action == "kasa_command" else "tuya" if action == "tuya_command" else ""
        adapter_name = str(tool_input.get("adapter") or default_adapter).strip()
        device_ref = str(tool_input.get("device_ref") or tool_input.get("device_id") or "").strip()
        command = str(tool_input.get("command") or "").strip()
        params = tool_input.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if not adapter_name:
            raise ValueError("adapter is required for device_command")
        if not device_ref:
            raise ValueError("device_ref is required for device_command")
        if not command:
            raise ValueError("command is required for device_command")

        self._require_private_network_scope()
        self._record(
            "device_command_sent",
            f"Sending {command} to {device_ref} via {adapter_name}",
            {
                "adapter": adapter_name,
                "device_ref": device_ref,
                "device_id": tool_input.get("device_id") or device_ref,
                "command": command,
            },
        )

        try:
            execution = execute_device_runtime_action(
                component="device_control",
                action="device_command",
                actor=_context_actor(context),
                db_path=self._state_store.db_path,
                payload={
                    "adapter": adapter_name,
                    "device_ref": device_ref,
                    "command": command,
                    "params": params,
                },
                timeout_s=15.0,
                related_run_id=_context_optional_str(context, "related_run_id"),
                related_task_id=_context_optional_str(context, "related_task_id"),
                related_plan_id=_context_optional_str(context, "related_plan_id"),
            )
        except ExecutionBoundaryError as exc:
            summary = f"I could not finish {command} for {device_ref} because the device runtime timed out or stopped."
            event_id = self._record(
                "device_command_failed",
                summary,
                {"adapter": adapter_name, "device_ref": device_ref, "command": command, "reason": "execution_boundary"},
            )
            structured = _build_device_command_result(
                action=command,
                target=device_ref,
                details=[{"id": device_ref, "control": "failed"}],
                error_details=[str(exc) or "Device runtime failed."],
                event_id=event_id,
                context=context,
            )
            raise ToolExecutionError(
                summary,
                result={"summary": summary, "device_command_result": structured},
                failure_type=FailureType.TOOL_ERROR,
            ) from exc

        result = execution.get("result") or {}
        ok = bool(result.get("ok", False))
        error = result.get("error")
        if ok:
            summary = f"Successfully sent {command} to {device_ref}."
            event_id = self._record(
                "device_command_succeeded",
                summary,
                {
                    "adapter": adapter_name,
                    "device_ref": device_ref,
                    "device_id": result.get("device_id") or tool_input.get("device_id") or device_ref,
                    "command": command,
                    "result": result,
                },
            )
        else:
            summary = f"Failed to send {command} to {device_ref}: {error or 'unknown error'}"
            event_id = self._record(
                "device_command_failed",
                summary,
                {
                    "adapter": adapter_name,
                    "device_ref": device_ref,
                    "device_id": tool_input.get("device_id") or device_ref,
                    "command": command,
                    "error": error,
                },
            )

        structured = _build_device_command_result(
            action=command,
            target=device_ref,
            details=[{"id": device_ref, "control": "changed" if ok else "failed", "confirmed": False}],
            error_details=[str(error)] if error else [],
            event_id=event_id,
            context=context,
        )
        payload = {
            "summary": summary,
            "ok": ok,
            "result": result,
            "execution": dict(execution.get("execution") or {}),
            "device_command_result": structured,
        }
        if not ok:
            raise ToolExecutionError(summary, result=payload, failure_type=FailureType.TOOL_ERROR)
        return payload

    def _kasa_command(
        self,
        tool_input: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return self._device_command(tool_input, action="kasa_command", context=context)

    def _resolve_devices(self, target: str) -> list[ConnectedDevice]:
        devices = list(self._state_store.list_devices())
        devices.extend(_configured_devices(self._state_store.db_path))
        if not devices:
            return []
        normalized = _normalize_text(target)
        if normalized in {"", "devices", "all", "everything"}:
            return devices
        if normalized in {"camera", "cameras"}:
            return [device for device in devices if "camera" in device.device_type.lower()]
        if normalized in {"light", "lights", "lamp", "lamps", "bulb", "bulbs"}:
            return [device for device in devices if _looks_like_light(device)]

        canonical = [device for device in devices if normalized == _normalize_text(device.id)]
        if len(canonical) == 1:
            return canonical
        if len(canonical) > 1:
            raise ValueError("Device identity is duplicated; use a unique device_ref.")
        matches = [device for device in devices if _device_matches(device, normalized)]
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            raise ValueError("That device name is ambiguous; use the exact device_ref.")
        return []

    def _record(self, event_type: str, message: str, payload: dict[str, Any]) -> str:
        event = self._state_store.record_event(
            actor="worker",
            event_type=event_type,
            message=message,
            json_payload=payload,
        )
        return event.id

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


def _build_device_command_result(
    *,
    action: str,
    target: str,
    details: list[dict[str, Any]],
    error_details: list[str],
    event_id: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    attempted: list[str] = []
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    confirmed_refs: list[str] = []
    verified_state: dict[str, dict[str, Any]] = {}

    for detail in details:
        device_ref = str(detail.get("id") or detail.get("device_ref") or detail.get("name") or "").strip()
        if not device_ref:
            continue
        attempted.append(device_ref)
        control = str(detail.get("control") or "failed").strip().lower()
        if control == "changed":
            succeeded.append(device_ref)
            state = detail.get("verified_state")
            if bool(detail.get("confirmed")) and isinstance(state, dict):
                confirmed_refs.append(device_ref)
                verified_state[device_ref] = dict(state)
        elif control in {"needs_setup", "unsupported"}:
            skipped.append({"device_ref": device_ref, "reason": control})
        else:
            failed.append({"device_ref": device_ref, "error": "Device command failed."})

    clean_errors = [str(error).strip() for error in error_details if str(error).strip()]
    if not details and clean_errors:
        failed.append({"device_ref": str(target or "").strip(), "error": clean_errors[0]})

    if not succeeded:
        status = "failed"
    elif failed or skipped or len(succeeded) < len(attempted):
        status = "partial"
    elif len(confirmed_refs) == len(succeeded):
        status = "confirmed"
    else:
        status = "accepted"

    return {
        "requested_action": str(action or "").strip().lower(),
        "target_device_ref": str(target or "").strip(),
        "status": status,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "physical_result_confirmed": bool(succeeded) and len(confirmed_refs) == len(succeeded),
        "verified_state": verified_state or None,
        "error_details": clean_errors,
        "references": {
            "event_ids": [event_id] if event_id else [],
            "run_id": _context_optional_str(context, "related_run_id"),
            "task_id": _context_optional_str(context, "related_task_id"),
        },
    }


def _normalize_command_details(
    details: list[dict[str, Any]],
    changed: list[str],
    needs_setup: list[str],
    unsupported: list[str],
    failed: list[str],
) -> list[dict[str, Any]]:
    changed_refs = {_normalize_text(value) for value in changed}
    setup_refs = {_normalize_text(value) for value in needs_setup}
    unsupported_refs = {_normalize_text(value) for value in unsupported}
    failed_refs = {_normalize_text(value).split(":", 1)[0] for value in failed}
    normalized: list[dict[str, Any]] = []
    for raw_detail in details:
        detail = dict(raw_detail)
        if not str(detail.get("control") or "").strip():
            refs = {
                _normalize_text(detail.get("id") or ""),
                _normalize_text(detail.get("name") or ""),
            }
            if refs & changed_refs:
                detail["control"] = "changed"
            elif refs & setup_refs:
                detail["control"] = "needs_setup"
            elif refs & unsupported_refs:
                detail["control"] = "unsupported"
            elif refs & failed_refs:
                detail["control"] = "failed"
        normalized.append(detail)
    return normalized


def _verified_states(details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for detail in details:
        device_ref = str(detail.get("id") or detail.get("device_ref") or "").strip()
        state = detail.get("verified_state")
        if device_ref and bool(detail.get("confirmed")) and isinstance(state, dict):
            states[device_ref] = dict(state)
    return states


def _device_matches(device: ConnectedDevice, normalized_target: str) -> bool:
    return any(_normalize_text(field) == normalized_target for field in _device_match_fields(device))


def _configured_devices(db_path: str) -> list[ConnectedDevice]:
    try:
        from gismo.core.device_adapters.config import load_configured_devices
    except Exception:  # noqa: BLE001
        return []

    try:
        _, configured = load_configured_devices(db_path=db_path)
    except RuntimeError:
        return []

    devices: list[ConnectedDevice] = []
    for entry in configured:
        device = _configured_entry_to_device(entry)
        if device is not None:
            devices.append(device)
    return devices


def _configured_entry_to_device(entry: dict[str, Any]) -> ConnectedDevice | None:
    if not isinstance(entry, dict):
        return None
    device_ref = _configured_device_ref(entry)
    ip = str(entry.get("ip") or "").strip()
    if not device_ref:
        return None

    platform = ""
    for field in ("adapter", "platform", "controller"):
        value = str(entry.get(field) or "").strip().lower()
        if value:
            platform = " ".join(value.split())
            break

    name = _configured_device_name(entry, fallback=device_ref or ip)
    device_type = str(entry.get("device_type") or "").strip() or (
        "light" if platform in {"tuya", "feit", "feit electric"} else "smart device"
    )
    brand = str(entry.get("brand") or entry.get("manufacturer") or "").strip()
    if not brand:
        if "feit" in platform:
            brand = "FEIT"
        elif platform:
            brand = platform.title()
        else:
            brand = "Configured"

    metadata: dict[str, Any] = {
        "label": name,
        "platform": platform or None,
        "gismo_device_id": str(entry.get("gismo_device_id") or "").strip() or None,
        "device_id": str(entry.get("device_id") or "").strip() or None,
        "adapter": "tuya" if platform in {"tuya", "feit", "feit electric"} else platform or None,
        "controller": "tuya" if platform in {"tuya", "feit", "feit electric"} else platform or None,
        "inventory_kind": "configured_actuator",
    }
    if metadata.get("adapter") == "tuya":
        metadata["open_ports"] = [6668]
    metadata = {key: value for key, value in metadata.items() if value is not None}

    return ConnectedDevice(
        id=device_ref or ip,
        ip=ip,
        hostname=name,
        device_type=device_type,
        brand=brand,
        metadata_json=metadata,
        created_at=datetime.now(timezone.utc),
    )


def _configured_device_ref(entry: dict[str, Any]) -> str:
    for field in ("gismo_device_id", "id"):
        value = str(entry.get(field) or "").strip()
        if value:
            return value
    return ""


def _configured_device_name(entry: dict[str, Any], *, fallback: str) -> str:
    for field in ("name", "label", "alias", "gismo_device_id", "device_id", "ip"):
        value = str(entry.get(field) or "").strip()
        if value:
            return value
    return fallback or "Configured device"


def _device_search_text(device: ConnectedDevice) -> str:
    return " ".join(
        _normalize_text(value)
        for value in _device_match_fields(device)
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
    normalized = (text or "").strip().lower().replace("'", "").replace("’", "")
    normalized = re.sub(r"[-_]+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9.\s]", " ", normalized)
    return " ".join(normalized.split())


def _device_match_fields(device: ConnectedDevice) -> list[str]:
    metadata = device.metadata_json if isinstance(device.metadata_json, dict) else {}
    fields = [
        _device_name(device),
        device.hostname or "",
        str(metadata.get("label") or ""),
        str(metadata.get("name") or ""),
        str(metadata.get("alias") or ""),
        str(metadata.get("gismo_device_id") or ""),
    ]
    return [field for field in fields if isinstance(field, str) and field.strip()]


def _light_change_phrase(command: str, params: dict[str, Any]) -> str:
    if command == "set_brightness":
        brightness = params.get("brightness")
        return f"brightness to {brightness}%"
    if command == "set_color_temp":
        preset = str(params.get("preset") or "white").strip().replace("_", " ")
        return f"to {preset}"
    if command == "set_color_rgb":
        color_name = str(params.get("color_name") or "that color").strip()
        return f"to {color_name}"
    return "right now"


def _light_failure_phrase(command: str, params: dict[str, Any]) -> str:
    if command == "set_brightness":
        return f"set the brightness to {params.get('brightness')}%"
    if command == "set_color_temp":
        preset = str(params.get("preset") or "white").strip().replace("_", " ")
        return f"set the color to {preset}"
    if command == "set_color_rgb":
        color_name = str(params.get("color_name") or "that color").strip()
        return f"set the color to {color_name}"
    return "change that light"


def _context_actor(context: dict[str, Any]) -> str:
    return _context_optional_str(context, "actor") or "worker"


def _context_optional_str(context: dict[str, Any], field_name: str) -> str | None:
    value = context.get(field_name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
