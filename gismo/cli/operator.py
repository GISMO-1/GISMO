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


def parse_command(command: str) -> Dict[str, Any]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Command must be a non-empty string.")

    trimmed = command.strip()

    single_match = re.match(r"(?i)^(echo|note|shell|run_shell|device)\s*:\s*(.+)$", trimmed)
    if single_match:
        verb = single_match.group(1).lower()
        payload = single_match.group(2).strip()
        if not payload:
            raise ValueError(f"{verb} command requires text after ':'")
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

    raise ValueError("Unsupported command. Use echo:, note:, shell:, device:, or graph:.")


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
        parsed = _parse_device_command(payload)
        return OperatorStep(
            tool_name="device_control",
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


def _parse_device_command(payload: str) -> Dict[str, Any]:
    text = " ".join(payload.strip().split())
    if not text:
        raise ValueError("device command requires text after ':'")

    lowered = text.lower()
    if lowered in {"scan", "scan devices", "scan network", "scan for devices"}:
        return {
            "title": "Devices: Scan network",
            "input_json": {"request": text, "action": "scan", "target": "network"},
        }
    if lowered in {"list", "list devices", "show devices", "show connected devices"}:
        return {
            "title": "Devices: List connected devices",
            "input_json": {"request": text, "action": "list", "target": "devices"},
        }

    power_match = re.match(r"(?i)^(turn|switch|power)\s+(on|off)\s+(.+)$", text)
    if power_match:
        state = power_match.group(2).lower()
        target = _normalize_device_target(power_match.group(3))
        if not target:
            raise ValueError("device power command requires a target")
        verb = "on" if state == "on" else "off"
        return {
            "title": f"Devices: Turn {verb} {target}",
            "input_json": {
                "request": text,
                "action": "turn_on" if state == "on" else "turn_off",
                "target": target,
            },
        }

    check_match = re.match(r"(?i)^(check|show|status(?:\s+of)?)\s+(.+)$", text)
    if check_match:
        target = _normalize_device_target(check_match.group(2))
        if not target:
            raise ValueError("device check command requires a target")
        return {
            "title": f"Devices: Check {target}",
            "input_json": {"request": text, "action": "check", "target": target},
        }

    raise ValueError(
        "Unsupported device command. Use scan, list, check ..., turn on ..., or turn off ...."
    )


def _normalize_device_target(text: str) -> str:
    target = " ".join(text.strip().split())
    target = re.sub(r"^(the|my)\s+", "", target, flags=re.IGNORECASE)
    return target
