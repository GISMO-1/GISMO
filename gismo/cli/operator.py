"""Operator command parsing for GISMO."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Set


@dataclass(frozen=True)
class OperatorStep:
    tool_name: str
    input_json: Dict[str, Any]
    title: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "input_json": self.input_json,
            "title": self.title,
        }


def normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


_LIGHT_TARGET_PATTERN = r".+?(?:light|lights|lamp|lamps|bulb|bulbs)\b"
_LIGHT_COLOR_TEMPERATURES = {
    "cool white": "cool_white",
    "cool": "cool",
    "warm white": "warm_white",
    "warm": "warm",
    "soft white": "soft_white",
    "daylight": "daylight",
    "white": "neutral",
}
_LIGHT_RGB_COLORS = {
    "red": {"r": 255, "g": 0, "b": 0, "color_name": "red"},
    "blue": {"r": 0, "g": 0, "b": 255, "color_name": "blue"},
    "green": {"r": 0, "g": 255, "b": 0, "color_name": "green"},
    "purple": {"r": 128, "g": 0, "b": 128, "color_name": "purple"},
    "pink": {"r": 255, "g": 105, "b": 180, "color_name": "pink"},
    "yellow": {"r": 255, "g": 255, "b": 0, "color_name": "yellow"},
    "orange": {"r": 255, "g": 165, "b": 0, "color_name": "orange"},
}
_LIGHT_FILLER_RE = re.compile(
    r"\b(?:and|at|to|it|make|set|the|a|an|please|mode|color|colour|brightness|level|percent)\b",
    re.IGNORECASE,
)


def looks_like_device_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return False
    has_device_target = bool(
        re.search(r"\b(?:device|devices|camera|cameras|light|lights|lamp|lamps|bulb|bulbs)\b", normalized)
    )
    has_device_verb = bool(
        re.search(
            r"\b(?:scan|find|discover|list|show|check|status|turn|switch|power|set|make|dim|brighten|lower|raise)\b",
            normalized,
        )
    )
    return has_device_target and has_device_verb


def parse_command(command: str) -> Dict[str, Any]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Command must be a non-empty string.")

    trimmed = command.strip()

    single_match = re.match(r"(?i)^(echo|note|shell|run_shell|device|calendar)\s*:\s*(.+)$", trimmed)
    if single_match:
        verb = single_match.group(1).lower()
        payload = single_match.group(2).strip()
        if not payload:
            raise ValueError(f"{verb} command requires text after ':'")
        if verb == "device":
            steps = _parse_device_command(payload)
            mode = "single" if len(steps) == 1 else "graph"
            return {"mode": mode, "steps": [step.to_dict() for step in steps]}
        step = _build_step(verb, payload)
        return {"mode": "single", "steps": [step.to_dict()]}

    graph_match = re.match(r"(?i)^graph\s*:\s*(.+)$", trimmed)
    if graph_match:
        remainder = graph_match.group(1).strip()
        if not remainder:
            raise ValueError("graph command requires at least one step")
        raw_steps = [part.strip() for part in remainder.split("->")]
        if any(not part for part in raw_steps):
            raise ValueError("graph command contains an empty step")
        steps: List[OperatorStep] = []
        for raw_step in raw_steps:
            step_match = re.match(r"(?i)^(echo|note)\s+(.+)$", raw_step)
            if not step_match:
                raise ValueError(
                    f"Invalid graph step '{raw_step}'. Expected 'echo TEXT' or 'note TEXT'."
                )
            verb = step_match.group(1).lower()
            payload = step_match.group(2).strip()
            if not payload:
                raise ValueError(f"{verb} step requires text after the verb")
            steps.append(_build_step(verb, payload))
        return {"mode": "graph", "steps": [step.to_dict() for step in steps]}

    raise ValueError("Unsupported command. Use echo:, note:, shell:, device:, calendar:, or graph:.")


def required_tools(plan: Dict[str, Any]) -> Set[str]:
    tools: Set[str] = set()
    for step in plan.get("steps", []):
        tool_name = step.get("tool_name")
        if tool_name:
            tools.add(tool_name)
    return tools


def make_idempotency_key(step: Dict[str, Any], normalized_command: str, index: int) -> str:
    normalized_input = _normalize_payload(step.get("input_json", {}))
    payload = json.dumps(
        {
            "command": normalized_command,
            "tool": step.get("tool_name"),
            "input": normalized_input,
            "index": index,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _build_step(verb: str, payload: str) -> OperatorStep:
    if verb == "echo":
        return OperatorStep(
            tool_name="echo",
            input_json={"message": payload},
            title=f"Echo: {payload}",
        )
    if verb == "note":
        return OperatorStep(
            tool_name="write_note",
            input_json={"note": payload},
            title=f"Note: {payload}",
        )
    if verb in {"shell", "run_shell"}:
        command = _parse_shell_command(payload)
        rendered = " ".join(command)
        return OperatorStep(
            tool_name="run_shell",
            input_json={"command": command},
            title=f"Shell: {rendered}",
        )
    if verb == "device":
        steps = _parse_device_command(payload)
        if len(steps) != 1:
            raise ValueError("device command expands to multiple steps and must be parsed through parse_command")
        return steps[0]
    if verb == "calendar":
        parsed = _parse_calendar_command(payload)
        return OperatorStep(
            tool_name="calendar_control",
            input_json=parsed["input_json"],
            title=parsed["title"],
        )
    raise ValueError(f"Unsupported verb '{verb}'")


def _parse_shell_command(payload: str) -> list[str]:
    try:
        command = shlex.split(payload, posix=True)
    except ValueError as exc:
        raise ValueError("shell command could not be parsed") from exc
    if not command:
        raise ValueError("shell command requires at least one token")
    return command


def _parse_device_command(payload: str) -> list[OperatorStep]:
    text = " ".join(payload.strip().split())
    if not text:
        raise ValueError("device command requires text after ':'")

    lowered = text.lower()
    if lowered in {"scan", "scan devices", "scan network", "scan for devices"}:
        return [_device_step("Devices: Scan network", {"request": text, "action": "scan", "target": "network"})]
    if lowered in {"list", "list devices", "show devices", "show connected devices"}:
        return [
            _device_step(
                "Devices: List connected devices",
                {"request": text, "action": "list", "target": "devices"},
            )
        ]

    light_steps = _parse_light_device_command(text)
    if light_steps is not None:
        return light_steps

    power_match = re.match(r"(?i)^(turn|switch|power)\s+(on|off)\s+(.+)$", text)
    if power_match:
        state = power_match.group(2).lower()
        target = _normalize_device_target(power_match.group(3))
        if not target:
            raise ValueError("device power command requires a target")
        verb = "on" if state == "on" else "off"
        return [
            _device_step(
                f"Devices: Turn {verb} {target}",
                {
                    "request": text,
                    "action": "turn_on" if state == "on" else "turn_off",
                    "target": target,
                },
            )
        ]

    check_match = re.match(
        r"(?i)^(check|show|status(?:\s+of)?|what(?:'s| is)\s+the\s+status\s+of)\s+(.+)$",
        text,
    )
    if check_match:
        target = _normalize_device_target(check_match.group(2))
        if not target:
            raise ValueError("device check command requires a target")
        return [
            _device_step(
                f"Devices: Check {target}",
                {"request": text, "action": "check", "target": target},
            )
        ]

    raise ValueError(
        "Unsupported device command. Use scan, list, check ..., turn on ..., turn off ..., or a light command like set Dad's light to warm white."
    )


def _parse_calendar_command(payload: str) -> Dict[str, Any]:
    text = " ".join(payload.strip().split())
    if not text:
        raise ValueError("calendar command requires text after ':'")

    match = re.match(r"(?i)^(add|update|delete|delete_range|list)\s+(.+)$", text)
    if not match:
        raise ValueError(
            "Unsupported calendar command. Use add, update, delete, delete_range, or list with a JSON payload."
        )
    action = match.group(1).lower()
    raw_payload = match.group(2).strip()
    try:
        payload_json = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("calendar command payload must be valid JSON") from exc
    if not isinstance(payload_json, dict):
        raise ValueError("calendar command payload must be a JSON object")

    title = str(payload_json.get("title") or "").strip()
    if action == "add":
        step_title = f"Calendar: Add {title or 'event'}"
    elif action == "update":
        step_title = f"Calendar: Update {title or 'event'}"
    elif action == "delete":
        step_title = "Calendar: Remove event"
    elif action == "delete_range":
        step_title = "Calendar: Clear range"
    else:
        step_title = "Calendar: List events"

    return {
        "title": step_title,
        "input_json": {"action": action, "payload": payload_json},
    }


def _normalize_device_target(text: str) -> str:
    target = " ".join(text.strip().split())
    target = re.sub(r"^(the|my)\s+", "", target, flags=re.IGNORECASE)
    return target


def _parse_light_device_command(text: str) -> list[OperatorStep] | None:
    power_on_first = re.match(
        rf"(?i)^(?:turn|switch|power)\s+(?P<power>on|off)\s+(?P<target>{_LIGHT_TARGET_PATTERN})(?:\s+(?P<rest>.+))?$",
        text,
    )
    if power_on_first:
        return _build_light_steps(
            text,
            target=power_on_first.group("target"),
            power_state=power_on_first.group("power"),
            descriptor=power_on_first.group("rest") or "",
        )

    power_target_first = re.match(
        rf"(?i)^(?:turn|switch|power)\s+(?P<target>{_LIGHT_TARGET_PATTERN})\s+(?P<power>on|off)(?:\s+(?P<rest>.+))?$",
        text,
    )
    if power_target_first:
        return _build_light_steps(
            text,
            target=power_target_first.group("target"),
            power_state=power_target_first.group("power"),
            descriptor=power_target_first.group("rest") or "",
        )

    set_match = re.match(
        rf"(?i)^(?:set|make)\s+(?P<target>{_LIGHT_TARGET_PATTERN})\s+(?:to\s+)?(?P<rest>.+)$",
        text,
    )
    if set_match:
        return _build_light_steps(
            text,
            target=set_match.group("target"),
            power_state="on",
            descriptor=set_match.group("rest"),
        )

    dim_match = re.match(
        rf"(?i)^(?P<verb>dim|brighten)\s+(?P<target>{_LIGHT_TARGET_PATTERN})(?:\s+(?P<rest>.+))?$",
        text,
    )
    if dim_match:
        return _build_light_steps(
            text,
            target=dim_match.group("target"),
            power_state="on",
            descriptor=dim_match.group("rest") or "",
            require_brightness=True,
        )

    adjust_match = re.match(
        rf"(?i)^(?P<verb>lower|raise)\s+brightness(?:\s+(?:for|on|of))?\s+(?P<target>{_LIGHT_TARGET_PATTERN})(?:\s+(?P<rest>.+))?$",
        text,
    )
    if adjust_match:
        return _build_light_steps(
            text,
            target=adjust_match.group("target"),
            power_state="on",
            descriptor=adjust_match.group("rest") or "",
            require_brightness=True,
        )

    return None


def _build_light_steps(
    request: str,
    *,
    target: str,
    power_state: str | None,
    descriptor: str,
    require_brightness: bool = False,
) -> list[OperatorStep]:
    normalized_target = _normalize_device_target(target)
    if not normalized_target:
        raise ValueError("light command requires a target")

    brightness, color_temp, color_rgb = _parse_light_settings(
        descriptor,
        require_brightness=require_brightness,
    )
    if power_state == "off":
        if brightness is not None or color_temp is not None or color_rgb is not None:
            raise ValueError("Turn-off light commands cannot include color or brightness changes.")
        return [
            _device_step(
                f"Devices: Turn off {normalized_target}",
                {"request": request, "action": "turn_off", "target": normalized_target},
            )
        ]

    steps: list[OperatorStep] = []
    if power_state == "on" or brightness is not None or color_temp is not None or color_rgb is not None:
        steps.append(
            _device_step(
                f"Devices: Turn on {normalized_target}",
                {"request": request, "action": "turn_on", "target": normalized_target},
            )
        )

    if color_temp is not None:
        steps.append(
            _device_step(
                f"Devices: Set {normalized_target} to {_render_color_temp_label(color_temp)}",
                {
                    "request": request,
                    "action": "set_color_temp",
                    "target": normalized_target,
                    "params": {"preset": color_temp},
                },
            )
        )
    if color_rgb is not None:
        steps.append(
            _device_step(
                f"Devices: Set {normalized_target} to {color_rgb['color_name']}",
                {
                    "request": request,
                    "action": "set_color_rgb",
                    "target": normalized_target,
                    "params": dict(color_rgb),
                },
            )
        )
    if brightness is not None:
        steps.append(
            _device_step(
                f"Devices: Set {normalized_target} brightness to {brightness}%",
                {
                    "request": request,
                    "action": "set_brightness",
                    "target": normalized_target,
                    "params": {"brightness": brightness},
                },
            )
        )
    if steps:
        return steps

    raise ValueError(
        "I need a light setting I recognize, like on, off, warm white, cool white, blue, or a brightness percentage."
    )


def _parse_light_settings(
    descriptor: str,
    *,
    require_brightness: bool,
) -> tuple[int | None, str | None, dict[str, int | str] | None]:
    remaining = " ".join((descriptor or "").strip().lower().split())
    brightness: int | None = None
    color_temp: str | None = None
    color_rgb: dict[str, int | str] | None = None

    if remaining:
        brightness, remaining = _extract_brightness(remaining)
        color_temp, remaining = _extract_phrase_value(remaining, _LIGHT_COLOR_TEMPERATURES)
        if color_temp is None:
            color_rgb, remaining = _extract_phrase_value(remaining, _LIGHT_RGB_COLORS)
        else:
            other_color, other_remaining = _extract_phrase_value(remaining, _LIGHT_RGB_COLORS)
            if other_color is not None:
                raise ValueError("Use either a white setting or a color, not both at once.")
            remaining = other_remaining

    if require_brightness and brightness is None:
        raise ValueError("Brightness commands need a percentage from 0 to 100.")
    if color_temp is not None and color_rgb is not None:
        raise ValueError("Use either a white setting or a color, not both at once.")

    remaining = _clean_light_descriptor(remaining)
    if remaining:
        raise ValueError(
            "I could not understand that light setting. Try warm white, cool white, a named color, or a brightness percentage."
        )
    return brightness, color_temp, color_rgb


def _extract_brightness(text: str) -> tuple[int | None, str]:
    patterns = (
        re.compile(r"\b(?:brightness\s+)?(?:to|at)\s+(?P<value>\d{1,3})(?:\s*percent)?\b", re.IGNORECASE),
        re.compile(r"\b(?P<value>\d{1,3})\s*percent\b", re.IGNORECASE),
        re.compile(r"\b(?P<value>\d{1,3})\b", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        value = int(match.group("value"))
        if not 0 <= value <= 100:
            raise ValueError("Brightness must be between 0 and 100.")
        return value, _remove_span(text, match.span())
    return None, text


def _extract_phrase_value(text: str, phrases: dict[str, Any]) -> tuple[Any | None, str]:
    for phrase in sorted(phrases, key=len, reverse=True):
        match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, flags=re.IGNORECASE)
        if match is None:
            continue
        return phrases[phrase], _remove_span(text, match.span())
    return None, text


def _remove_span(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return " ".join((text[:start] + " " + text[end:]).split())


def _clean_light_descriptor(text: str) -> str:
    if not text:
        return ""
    cleaned = _LIGHT_FILLER_RE.sub(" ", text)
    return " ".join(cleaned.split())


def _render_color_temp_label(preset: str) -> str:
    labels = {
        "cool_white": "cool white",
        "warm_white": "warm white",
        "soft_white": "soft white",
    }
    return labels.get(preset, preset.replace("_", " "))


def _device_step(title: str, input_json: dict[str, Any]) -> OperatorStep:
    return OperatorStep(tool_name="device_control", input_json=input_json, title=title)
