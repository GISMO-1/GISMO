"""Prompt helpers for LLM planning."""
from __future__ import annotations


def build_system_prompt() -> str:
    return (
        "You are a planning assistant for GISMO. "
        "You must output a single JSON object and nothing else. "
        "Do not include markdown or extra text. "
        "The JSON schema is: "
        "{\n"
        "  \"intent\": \"string\",\n"
        "  \"assumptions\": [\"string\"],\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"type\": \"enqueue\",\n"
        "      \"command\": \"string\",\n"
        "      \"timeout_seconds\": 30,\n"
        "      \"retries\": 0,\n"
        "      \"why\": \"string\",\n"
        "      \"risk\": \"low|medium|high\"\n"
        "    }\n"
        "  ],\n"
        "  \"notes\": [\"string\"]\n"
        "}\n"
        "Rules: command must be a GISMO operator command string (echo:, note:, or graph:). "
        "Keep actions small and sequenced. "
        "If the user request is unsafe or unsupported, return actions as an empty array "
        "and explain why in notes."
    )


def build_user_prompt(user_text: str) -> str:
    return f"User request: {user_text}".strip()
