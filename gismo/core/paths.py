"""Path helpers derived from the database location."""
from __future__ import annotations

from pathlib import Path


def resolve_exports_dir(db_path: str | Path) -> Path:
    base_dir = Path(db_path).resolve().parent.parent
    exports_dir = base_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


def resolve_devices_config_path(db_path: str | Path | None = None) -> Path:
    if db_path is None:
        return (Path(".gismo") / "devices.json").resolve()

    db_file = Path(db_path).resolve()
    db_dir = db_file.parent
    if db_dir.name == ".gismo":
        return db_dir / "devices.json"

    return db_dir / ".gismo" / "devices.json"
