"""Path helpers derived from the database location."""
from __future__ import annotations

from pathlib import Path


def resolve_exports_dir(db_path: str | Path) -> Path:
    base_dir = Path(db_path).resolve().parent.parent
    exports_dir = base_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir
