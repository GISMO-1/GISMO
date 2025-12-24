"""Filesystem toolpack with base directory restrictions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from gismo.core.toolpacks.path_utils import resolve_within_base
from gismo.core.tools import Tool


@dataclass
class FileSystemConfig:
    base_dir: Path


class ReadFileTool(Tool):
    def __init__(self, config: FileSystemConfig) -> None:
        super().__init__(
            name="read_file",
            description="Read a file from the allowed base directory",
            schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        self._config = config

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        path = tool_input.get("path")
        resolved = resolve_within_base(self._config.base_dir, path)
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
        content = resolved.read_text(encoding="utf-8")
        return {"path": str(resolved), "content": content}


class WriteFileTool(Tool):
    def __init__(self, config: FileSystemConfig) -> None:
        super().__init__(
            name="write_file",
            description="Write a file within the allowed base directory",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        )
        self._config = config

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        path = tool_input.get("path")
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        resolved = resolve_within_base(self._config.base_dir, path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return {"path": str(resolved), "bytes_written": len(content.encode("utf-8"))}


class ListDirTool(Tool):
    def __init__(self, config: FileSystemConfig) -> None:
        super().__init__(
            name="list_dir",
            description="List directory entries within the allowed base directory",
            schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        self._config = config

    def run(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        path = tool_input.get("path", ".")
        resolved = resolve_within_base(self._config.base_dir, path)
        if not resolved.exists() or not resolved.is_dir():
            raise FileNotFoundError(f"Directory not found: {resolved}")
        entries: List[str] = sorted(entry.name for entry in resolved.iterdir())
        return {"path": str(resolved), "entries": entries}
