"""Windows Startup folder helpers for GISMO daemon."""
from __future__ import annotations

import os
from pathlib import Path

from gismo.cli.windows_utils import quote_windows_arg


def get_windows_startup_folder(appdata: str | None = None) -> Path:
    base = appdata or os.environ.get("APPDATA")
    if not base:
        raise RuntimeError("APPDATA is not set; cannot locate Windows Startup folder.")
    return (
        Path(base)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def build_windows_startup_launcher_content(python_exe: str, db_path: str) -> str:
    command = [
        python_exe,
        "-m",
        "gismo.cli.main",
        "daemon",
        "--db",
        db_path,
    ]
    quoted = " ".join(quote_windows_arg(arg) for arg in command)
    return f"@echo off\n{quoted}\n"


def install_windows_startup_launcher(
    name: str,
    db_path: str,
    python_exe: str,
    *,
    force: bool,
    startup_dir: Path | None = None,
) -> Path:
    if startup_dir is None:
        _ensure_windows()
        startup_dir = get_windows_startup_folder()
    startup_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = startup_dir / f"{name}.cmd"
    if launcher_path.exists() and not force:
        print(f"Launcher already exists at {launcher_path}.")
        print("Re-run with --force to overwrite.")
        return launcher_path
    content = build_windows_startup_launcher_content(python_exe, db_path)
    launcher_path.write_text(content, encoding="utf-8")
    return launcher_path


def uninstall_windows_startup_launcher(
    name: str,
    *,
    yes: bool,
    startup_dir: Path | None = None,
) -> Path:
    if startup_dir is None:
        _ensure_windows()
        startup_dir = get_windows_startup_folder()
    launcher_path = startup_dir / f"{name}.cmd"
    if not yes:
        print(f"Dry run: would remove launcher \"{launcher_path}\".")
        print("Re-run with --yes to confirm removal.")
        return launcher_path
    launcher_path.unlink(missing_ok=True)
    return launcher_path


def _ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Startup folder commands are only supported on Windows.")
