"""Strict operator plan validation and binding helpers."""
from __future__ import annotations

import hashlib
from typing import Any

from gismo.cli.operator import normalize_command
from gismo.core.tool_receipts import canonical_json


ALLOWED_TOOL_NAMES = {
    "echo",
    "write_note",
    "run_shell",
    "device_control",
    "calendar_control",
}
ALLOWED_DEVICE_ACTIONS = {
    "scan",
    "list",
    "check",
    "turn_on",
    "turn_off",
    "set_brightness",
    "set_color_temp",
    "set_color_rgb",
}
ALLOWED_CALENDAR_ACTIONS = {"add", "update", "delete", "delete_range", "list"}
ALLOWED_COLOR_TEMP_PRESETS = {
    "cool",
    "cool_white",
    "daylight",
    "neutral",
    "soft_white",
    "warm",
    "warm_white",
}
OPERATOR_PLAN_BINDING_VERSION = 1
_PLAN_KEYS = {"mode", "steps"}
_STEP_KEYS = {"tool_name", "input_json", "title"}
_BINDING_KEYS = {"version", "visible_command", "plan_sha256", "digest"}


class OperatorPlanValidationError(ValueError):
    """Raised when an operator plan or its binding is malformed."""


def canonicalize_operator_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise OperatorPlanValidationError("operator_plan must be an object")
    extra_keys = sorted(set(plan) - _PLAN_KEYS)
    if extra_keys:
        raise OperatorPlanValidationError(f"operator_plan has unexpected fields: {', '.join(extra_keys)}")
    mode = str(plan.get("mode") or "").strip().lower()
    if mode not in {"single", "graph"}:
        raise OperatorPlanValidationError("operator_plan.mode must be 'single' or 'graph'")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise OperatorPlanValidationError("operator_plan.steps must be a non-empty list")
    if mode == "single" and len(raw_steps) != 1:
        raise OperatorPlanValidationError("single operator plans must contain exactly one step")

    steps = [canonicalize_operator_step(step) for step in raw_steps]
    return {"mode": mode, "steps": steps}


def canonicalize_operator_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise OperatorPlanValidationError("operator_plan steps must be objects")
    extra_keys = sorted(set(step) - _STEP_KEYS)
    if extra_keys:
        raise OperatorPlanValidationError(f"operator_plan step has unexpected fields: {', '.join(extra_keys)}")
    tool_name = str(step.get("tool_name") or "").strip()
    if tool_name not in ALLOWED_TOOL_NAMES:
        raise OperatorPlanValidationError(f"operator_plan tool is not allowed: {tool_name or '<empty>'}")
    title = str(step.get("title") or "").strip()
    if not title:
        raise OperatorPlanValidationError("operator_plan step title is required")
    return {
        "tool_name": tool_name,
        "input_json": canonicalize_tool_input(tool_name, step.get("input_json")),
        "title": title,
    }


def canonicalize_tool_input(tool_name: str, tool_input: Any) -> dict[str, Any]:
    if not isinstance(tool_input, dict):
        raise OperatorPlanValidationError(f"{tool_name} input_json must be an object")
    if tool_name == "echo":
        return {"message": _require_text_field(tool_input, "message", exact_keys={"message"})}
    if tool_name == "write_note":
        return {"note": _require_text_field(tool_input, "note", exact_keys={"note"})}
    if tool_name == "run_shell":
        _reject_extra_keys(tool_input, {"command"}, label="run_shell input_json")
        command = tool_input.get("command")
        if not isinstance(command, list) or not command:
            raise OperatorPlanValidationError("run_shell command must be a non-empty list")
        normalized: list[str] = []
        for index, value in enumerate(command):
            token = str(value or "").strip()
            if not token:
                raise OperatorPlanValidationError(f"run_shell command token {index} must be a non-empty string")
            normalized.append(token)
        return {"command": normalized}
    if tool_name == "device_control":
        return _canonicalize_device_input(tool_input)
    if tool_name == "calendar_control":
        return _canonicalize_calendar_input(tool_input)
    raise OperatorPlanValidationError(f"Unsupported operator_plan tool: {tool_name}")


def build_operator_plan_binding(*, visible_command: str, operator_plan: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical_plan = canonicalize_operator_plan(operator_plan)
    normalized_command = normalize_visible_command(visible_command)
    plan_sha256 = hashlib.sha256(canonical_json(canonical_plan).encode("utf-8")).hexdigest()
    digest = _binding_digest(normalized_command, canonical_plan)
    return canonical_plan, {
        "version": OPERATOR_PLAN_BINDING_VERSION,
        "visible_command": normalized_command,
        "plan_sha256": plan_sha256,
        "digest": digest,
    }


def verify_operator_plan_binding(
    *,
    visible_command: str,
    operator_plan: Any,
    binding: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical_plan, expected_binding = build_operator_plan_binding(
        visible_command=visible_command,
        operator_plan=operator_plan,
    )
    if not isinstance(binding, dict):
        raise OperatorPlanValidationError("operator_plan_binding must be an object")
    extra_keys = sorted(set(binding) - _BINDING_KEYS)
    if extra_keys:
        raise OperatorPlanValidationError(
            f"operator_plan_binding has unexpected fields: {', '.join(extra_keys)}"
        )
    version = binding.get("version")
    if version != OPERATOR_PLAN_BINDING_VERSION:
        raise OperatorPlanValidationError(
            f"operator_plan_binding.version must be {OPERATOR_PLAN_BINDING_VERSION}"
        )
    actual_binding = {
        "version": version,
        "visible_command": str(binding.get("visible_command") or "").strip(),
        "plan_sha256": str(binding.get("plan_sha256") or "").strip(),
        "digest": str(binding.get("digest") or "").strip(),
    }
    if actual_binding["visible_command"] != expected_binding["visible_command"]:
        raise OperatorPlanValidationError("operator_plan_binding visible command mismatch")
    if actual_binding["plan_sha256"] != expected_binding["plan_sha256"]:
        raise OperatorPlanValidationError("operator_plan_binding plan digest mismatch")
    if actual_binding["digest"] != expected_binding["digest"]:
        raise OperatorPlanValidationError("operator_plan_binding digest mismatch")
    return canonical_plan, expected_binding


def normalize_visible_command(command_text: str) -> str:
    normalized = normalize_command(command_text)
    if not normalized:
        raise OperatorPlanValidationError("visible command must be a non-empty string")
    return normalized


def _binding_digest(visible_command: str, canonical_plan: dict[str, Any]) -> str:
    payload = {
        "visible_command": visible_command,
        "operator_plan": canonical_plan,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _canonicalize_device_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(tool_input, {"request", "action", "target", "params"}, label="device_control input_json")
    request = _require_text_field(tool_input, "request")
    action = str(tool_input.get("action") or "").strip().lower()
    if action not in ALLOWED_DEVICE_ACTIONS:
        raise OperatorPlanValidationError(f"device_control action is not allowed: {action or '<empty>'}")
    target = _require_text_field(tool_input, "target")
    normalized = {
        "request": request,
        "action": action,
        "target": target,
    }
    if action in {"scan", "list", "check", "turn_on", "turn_off"}:
        if "params" in tool_input and tool_input.get("params") not in ({}, None):
            raise OperatorPlanValidationError(f"device_control action '{action}' does not accept params")
        return normalized
    params = tool_input.get("params")
    if not isinstance(params, dict):
        raise OperatorPlanValidationError(f"device_control action '{action}' requires params")
    normalized["params"] = _canonicalize_device_params(action, params)
    return normalized


def _canonicalize_device_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    if action == "set_brightness":
        _reject_extra_keys(params, {"brightness"}, label="device_control params")
        brightness = params.get("brightness")
        if not isinstance(brightness, int) or isinstance(brightness, bool):
            raise OperatorPlanValidationError("device_control brightness must be an integer")
        if brightness < 0 or brightness > 100:
            raise OperatorPlanValidationError("device_control brightness must be between 0 and 100")
        return {"brightness": brightness}
    if action == "set_color_temp":
        _reject_extra_keys(params, {"preset"}, label="device_control params")
        preset = str(params.get("preset") or "").strip().lower()
        if preset not in ALLOWED_COLOR_TEMP_PRESETS:
            raise OperatorPlanValidationError(f"device_control preset is not allowed: {preset or '<empty>'}")
        return {"preset": preset}
    if action == "set_color_rgb":
        _reject_extra_keys(params, {"r", "g", "b", "color_name"}, label="device_control params")
        normalized: dict[str, Any] = {}
        for key in ("r", "g", "b"):
            value = params.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise OperatorPlanValidationError(f"device_control {key} must be an integer")
            if value < 0 or value > 255:
                raise OperatorPlanValidationError(f"device_control {key} must be between 0 and 255")
            normalized[key] = value
        color_name = params.get("color_name")
        if color_name is not None:
            normalized["color_name"] = _require_text_value(color_name, "device_control color_name")
        return normalized
    raise OperatorPlanValidationError(f"device_control action is not allowed: {action}")


def _canonicalize_calendar_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(tool_input, {"action", "payload"}, label="calendar_control input_json")
    action = str(tool_input.get("action") or "").strip().lower()
    if action not in ALLOWED_CALENDAR_ACTIONS:
        raise OperatorPlanValidationError(f"calendar_control action is not allowed: {action or '<empty>'}")
    payload = tool_input.get("payload")
    if not isinstance(payload, dict):
        raise OperatorPlanValidationError("calendar_control payload must be an object")
    return {"action": action, "payload": _canonicalize_json_value(payload, label="calendar_control payload")}


def _canonicalize_json_value(value: Any, *, label: str) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonicalize_json_value(child, label=label) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_canonicalize_json_value(child, label=label) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise OperatorPlanValidationError(f"{label} contains a non-JSON value")


def _require_text_field(payload: dict[str, Any], key: str, *, exact_keys: set[str] | None = None) -> str:
    if exact_keys is not None:
        _reject_extra_keys(payload, exact_keys, label=f"{key} input_json")
    return _require_text_value(payload.get(key), key)


def _require_text_value(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OperatorPlanValidationError(f"{label} must be a non-empty string")
    return text


def _reject_extra_keys(payload: dict[str, Any], allowed: set[str], *, label: str) -> None:
    extra_keys = sorted(set(payload) - allowed)
    if extra_keys:
        raise OperatorPlanValidationError(f"{label} has unexpected fields: {', '.join(extra_keys)}")
