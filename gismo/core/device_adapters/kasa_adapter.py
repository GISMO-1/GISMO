"""Kasa device adapter -- TP-Link Kasa smart plugs and switches over LAN."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from gismo.core.device_adapters.base import (
    AdapterInfo,
    CommandResult,
    DeviceAdapter,
    DeviceInfo,
)

LOGGER = logging.getLogger(__name__)

_KASA_ADAPTER_INFO = AdapterInfo(
    name="kasa",
    version="1.0.0",
    device_types=("smart_plug", "smart_switch", "smart_bulb"),
    trust_zone="device_adapter",
    required_permissions=("device.control", "device.power"),
    supports_discovery=True,
    supports_emeter=True,
)

_SUPPORTED_COMMANDS = frozenset({"turn_on", "turn_off", "toggle", "get_state", "get_energy"})


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously.

    Creates a fresh event loop to avoid conflicts with any outer loop.
    This mirrors the subprocess-based isolation pattern used throughout
    device_runtime.py -- each call is self-contained.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class KasaAdapter(DeviceAdapter):
    """TP-Link Kasa adapter using python-kasa for LAN discovery and control."""

    def get_adapter_info(self) -> AdapterInfo:
        return _KASA_ADAPTER_INFO

    def discover(self, *, timeout_seconds: float = 5.0) -> list[DeviceInfo]:
        try:
            import kasa
        except ImportError:
            raise RuntimeError("python-kasa is not installed; run: pip install python-kasa")

        try:
            found = _run_async(kasa.Discover.discover(timeout=int(timeout_seconds)))
        except Exception as exc:
            LOGGER.warning("kasa discovery failed: %s", exc)
            return []

        devices: list[DeviceInfo] = []
        for host, device in found.items():
            try:
                _run_async(device.update())
                device_id = getattr(device, "mac", None) or host
                alias = getattr(device, "alias", None) or host
                model = getattr(device, "model", "unknown")
                is_on = bool(getattr(device, "is_on", False))
                state: dict[str, Any] = {"is_on": is_on}
                if hasattr(device, "emeter_realtime"):
                    try:
                        emeter = _run_async(device.get_emeter_realtime())
                        state["emeter"] = dict(emeter) if emeter else {}
                    except Exception:
                        pass
                devices.append(DeviceInfo(
                    device_id=device_id,
                    alias=alias,
                    model=model,
                    device_type="smart_plug",
                    host=host,
                    is_online=True,
                    state=state,
                ))
            except Exception as exc:
                LOGGER.warning("failed to update kasa device %s: %s", host, exc)
        return devices

    def get_state(self, device_ref: str) -> dict[str, Any]:
        devices = self.discover()
        for d in devices:
            if d.device_id == device_ref or d.host == device_ref:
                return dict(d.state)
        raise KeyError(f"device not found: {device_ref!r}")

    def send_command(
        self,
        device_ref: str,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> CommandResult:
        if command not in _SUPPORTED_COMMANDS:
            return CommandResult(
                ok=False,
                device_id=device_ref,
                command=command,
                state_before={},
                state_after={},
                error=f"unsupported command: {command!r}",
                error_type="ValueError",
            )
        try:
            import kasa
        except ImportError:
            return CommandResult(
                ok=False,
                device_id=device_ref,
                command=command,
                state_before={},
                state_after={},
                error="python-kasa is not installed",
                error_type="ImportError",
            )

        return _run_async(self._send_command_async(device_ref, command, params or {}, kasa))

    async def _send_command_async(
        self,
        device_ref: str,
        command: str,
        params: dict[str, Any],
        kasa: Any,
    ) -> CommandResult:
        found = await kasa.Discover.discover()
        device = None
        for h, d in found.items():
            await d.update()
            mac = getattr(d, "mac", None)
            if mac == device_ref or h == device_ref:
                device = d
                break

        if device is None:
            return CommandResult(
                ok=False,
                device_id=device_ref,
                command=command,
                state_before={},
                state_after={},
                error=f"device not found: {device_ref!r}",
                error_type="KeyError",
            )

        state_before = {"is_on": bool(getattr(device, "is_on", False))}
        try:
            if command == "turn_on":
                await device.turn_on()
            elif command == "turn_off":
                await device.turn_off()
            elif command == "toggle":
                if getattr(device, "is_on", False):
                    await device.turn_off()
                else:
                    await device.turn_on()
            elif command == "get_state":
                pass  # state already read via update()
            elif command == "get_energy":
                if not hasattr(device, "get_emeter_realtime"):
                    return CommandResult(
                        ok=False,
                        device_id=device_ref,
                        command=command,
                        state_before=state_before,
                        state_after=state_before,
                        error="device does not support energy monitoring",
                        error_type="NotImplementedError",
                    )

            await device.update()
            state_after = {"is_on": bool(getattr(device, "is_on", False))}
            if command == "get_energy" and hasattr(device, "get_emeter_realtime"):
                emeter = await device.get_emeter_realtime()
                state_after["emeter"] = dict(emeter) if emeter else {}

            return CommandResult(
                ok=True,
                device_id=device_ref,
                command=command,
                state_before=state_before,
                state_after=state_after,
                raw_response=state_after,
            )
        except Exception as exc:
            return CommandResult(
                ok=False,
                device_id=device_ref,
                command=command,
                state_before=state_before,
                state_after={},
                error=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
