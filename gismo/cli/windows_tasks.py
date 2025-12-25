"""Windows Task Scheduler helpers for GISMO daemon."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Iterable
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class WindowsTaskConfig:
    name: str
    db_path: str
    python_exe: str
    user: str | None = None
    force: bool = False
    on_startup: bool = False


def build_daemon_command(python_exe: str, db_path: str) -> list[str]:
    return [python_exe, "-m", "gismo.cli.main", "daemon", "--db", db_path]


def build_schtasks_create_args(task_name: str, xml_path: str, force: bool) -> list[str]:
    args = ["schtasks.exe", "/Create", "/TN", task_name, "/XML", xml_path]
    if force:
        args.append("/F")
    return args


def build_schtasks_delete_args(task_name: str) -> list[str]:
    return ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]


def install_windows_task(config: WindowsTaskConfig) -> None:
    _ensure_windows()
    command = build_daemon_command(config.python_exe, config.db_path)
    task_user = _resolve_task_user(config.user)
    task_xml = build_task_xml(command, task_user, on_startup=config.on_startup)
    removal = build_schtasks_delete_args(config.name)
    print(f"Task command: {_format_command(command)}")
    print(f"Remove with: {_format_command(removal)}")
    _run_schtasks_create(config, task_xml)


def uninstall_windows_task(task_name: str) -> None:
    _ensure_windows()
    args = build_schtasks_delete_args(task_name)
    subprocess.run(args, check=True)


def build_task_xml(command: list[str], user: str, *, on_startup: bool) -> str:
    command_path, arguments = _split_exec_command(command)
    now = datetime.now(timezone.utc).isoformat()
    boot_trigger = ""
    if on_startup:
        boot_trigger = """
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>"""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{escape(now)}</Date>
    <Author>GISMO</Author>
  </RegistrationInfo>
  <Triggers>
{boot_trigger}
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command_path)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler commands are only supported on Windows.")


def _resolve_task_user(user: str | None) -> str:
    if user:
        return user
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    if domain:
        return f"{domain}\\{username}"
    return username


def _run_schtasks_create(config: WindowsTaskConfig, task_xml: str) -> None:
    xml_path = _write_task_xml(task_xml, Path(config.db_path))
    try:
        args = build_schtasks_create_args(config.name, str(xml_path), config.force)
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        if stderr:
            print(f"schtasks.exe stderr:\n{stderr}", file=sys.stderr)
        if stdout:
            print(f"schtasks.exe stdout:\n{stdout}", file=sys.stderr)
        hint = (
            "Try running PowerShell as Administrator."
            if not config.on_startup
            else "Re-run without --on-startup."
        )
        print(f"Hint: {hint}", file=sys.stderr)
        raise
    finally:
        xml_path.unlink(missing_ok=True)


def _write_task_xml(task_xml: str, db_path: Path) -> Path:
    directory = db_path.parent if db_path.parent.exists() else Path(".")
    with tempfile.NamedTemporaryFile("w", encoding="utf-16", delete=False, dir=directory, suffix=".xml") as handle:
        handle.write(task_xml)
        return Path(handle.name)


def _split_exec_command(command: list[str]) -> tuple[str, str]:
    if not command:
        raise ValueError("Command cannot be empty")
    command_path = command[0]
    arguments = " ".join(_quote_windows_arg(arg) for arg in command[1:])
    return command_path, arguments


def _quote_windows_arg(value: str) -> str:
    if not value:
        return "\"\""
    if any(ch in value for ch in (" ", "\t", "\"")):
        escaped = value.replace("\"", "\\\"")
        return f"\"{escaped}\""
    return value


def _format_command(args: Iterable[str]) -> str:
    return " ".join(_quote_windows_arg(arg) for arg in args)
