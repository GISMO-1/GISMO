"""Abstract base for GISMO device adapters.

Every adapter must implement this interface. The adapter layer translates
GISMO's generic device commands into hardware-specific protocol calls.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AdapterInfo:
    name: str
    version: str
    device_types: tuple[str, ...]       # e.g. ("smart_plug", "smart_switch")
    trust_zone: str                      # must be "device_adapter"
    required_permissions: tuple[str, ...]  # e.g. ("device.control", "device.power")
    supports_discovery: bool
    supports_emeter: bool = False


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str          # stable identifier (MAC or IP-based)
    alias: str              # human name ("Living Room Lamp")
    model: str
    device_type: str        # "smart_plug", "smart_switch", etc.
    host: str               # IP address
    is_online: bool
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    device_id: str
    command: str
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    error: str | None = None
    error_type: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


class DeviceAdapter(abc.ABC):
    """Abstract base class all device adapters must implement.

    Adapters translate GISMO's generic device commands into hardware-specific
    protocol calls. All methods are synchronous; adapters that use async
    internally (e.g. python-kasa) must manage their own event loop.
    """

    @abc.abstractmethod
    def get_adapter_info(self) -> AdapterInfo: ...

    @abc.abstractmethod
    def discover(self, *, timeout_seconds: float = 5.0) -> list[DeviceInfo]:
        """Broadcast discovery on the LAN and return found devices."""
        ...

    @abc.abstractmethod
    def get_state(self, device_ref: str) -> dict[str, Any]:
        """Return current state of the device (on/off, energy, etc.)."""
        ...

    @abc.abstractmethod
    def send_command(
        self,
        device_ref: str,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> CommandResult:
        """Send a command to the device and return the result."""
        ...
