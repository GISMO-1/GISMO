"""Operator command parsing for GISMO."""
from __future__ import annotations

import hashlib
import json
import re
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

    single_match = re.match(r"(?i)^(echo|note)\s*:\s*(.+)$", trimmed)
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

    raise ValueError("Unsupported command. Use echo:, note:, or graph:.")


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
    raise ValueError(f"Unsupported verb '{verb}'")
