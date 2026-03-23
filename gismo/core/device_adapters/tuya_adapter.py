"""Tuya device adapter for Feit Electric bulbs and other local Tuya devices."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from gismo.core.device_adapters.base import (
    AdapterInfo,
    CommandResult,
    DeviceAdapter,
    DeviceInfo,
)
from gismo.core.device_adapters.config import (
    find_configured_device,
    load_configured_devices,
    normalize_platform_name,
)

LOGGER = logging.getLogger(__name__)
_CONFIG_ENV_VAR = "GISMO_DEVICES_CONFIG"

_TUYA_ADAPTER_INFO = AdapterInfo(
    name="tuya",
    version="1.0.0",
    device_types=("smart_bulb", "light", "smart_device"),
    trust_zone="device_adapter",
    required_permissions=("device.control", "device.power"),
    supports_discovery=True,
)

_SUPPORTED_COMMANDS = frozenset(
    {
        "turn_on",
        "turn_off",
        "set_brightness",
        "set_color_temp",
        "set_color_rgb",
        "get_state",
    }
)

_COLOR_TEMP_PRESETS = {
    "warm": 0,
    "warm_white": 0,
    "soft_white": 25,
    "neutral": 50,
    "natural": 50,
    "cool": 75,
    "cool_white": 75,
    "daylight": 100,
}


class DeviceConfigurationError(RuntimeError):
    """Raised when GISMO cannot resolve Tuya credentials for a requested device."""


class TuyaAdapter(DeviceAdapter):
    """Tuya LAN adapter using tinytuya for Feit and other local devices."""

    def __init__(self, *, config_path: str | Path | None = None) -> None:
        self._config_path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else None
        )

    def get_adapter_info(self) -> AdapterInfo:
        return _TUYA_ADAPTER_INFO

    def discover(self, *, timeout_seconds: float = 5.0) -> list[DeviceInfo]:
        tinytuya = _import_tinytuya()
        _, configured = self._load_entries()
        tuya_entries = [entry for entry in configured if _looks_like_tuya_entry(entry)]

        scanned: dict[str, dict[str, Any]] = {}
        try:
            response = tinytuya.deviceScan(
                maxretry=max(1, int(round(timeout_seconds))),
                color=False,
                poll=False,
            )
            if isinstance(response, dict):
                scanned = {
                    str(ip): dict(payload)
                    for ip, payload in response.items()
                    if isinstance(payload, dict)
                }
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("tuya discovery failed: %s", exc)

        devices: list[DeviceInfo] = []
        seen: set[str] = set()

        for entry in tuya_entries:
            ip = str(entry.get("ip") or "").strip()
            if not ip:
                continue
            scan_state = scanned.get(ip)
            alias = str(entry.get("name") or entry.get("alias") or ip).strip() or ip
            model = str(entry.get("model") or entry.get("device_type") or "Tuya Device").strip()
            devices.append(
                DeviceInfo(
                    device_id=str(entry.get("device_id") or ip),
                    alias=alias,
                    model=model,
                    device_type=_configured_device_type(entry),
                    host=ip,
                    is_online=scan_state is not None,
                    state=_normalize_scan_state(scan_state),
                )
            )
            seen.add(ip)

        for ip, payload in scanned.items():
            if ip in seen:
                continue
            devices.append(
                DeviceInfo(
                    device_id=str(payload.get("gwId") or ip),
                    alias=str(payload.get("name") or ip),
                    model=str(payload.get("productKey") or payload.get("version") or "Tuya Device"),
                    device_type="smart_bulb" if _scan_looks_like_bulb(payload) else "smart_device",
                    host=ip,
                    is_online=True,
                    state=_normalize_scan_state(payload),
                )
            )

        return devices

    def get_state(self, device_ref: str) -> dict[str, Any]:
        tinytuya = _import_tinytuya()
        entry = self._resolve_entry(device_ref)
        controller, is_bulb = _build_controller(entry, tinytuya)
        raw_state = _read_state(controller, is_bulb)
        return _normalize_state(controller, raw_state, is_bulb=is_bulb)

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
            tinytuya = _import_tinytuya()
        except ImportError as exc:
            return CommandResult(
                ok=False,
                device_id=device_ref,
                command=command,
                state_before={},
                state_after={},
                error=str(exc),
                error_type="ImportError",
            )

        state_before: dict[str, Any] = {}
        raw_response: Any = {}
        resolved_device_id = device_ref
        try:
            entry = self._resolve_entry(device_ref)
            resolved_device_id = str(entry.get("device_id") or device_ref)
            controller, is_bulb = _build_controller(entry, tinytuya)
            state_before = _normalize_state(
                controller,
                _read_state(controller, is_bulb),
                is_bulb=is_bulb,
            )
            raw_response = self._apply_command(
                controller,
                command,
                params=params or {},
                is_bulb=is_bulb,
            )
            state_after = _normalize_state(
                controller,
                _read_state(controller, is_bulb),
                is_bulb=is_bulb,
            )
            if command == "get_state":
                state_before = dict(state_after)
            return CommandResult(
                ok=True,
                device_id=resolved_device_id,
                command=command,
                state_before=state_before,
                state_after=state_after,
                raw_response=_coerce_raw_response(
                    raw_response,
                    device_ref=device_ref,
                    entry=entry,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult(
                ok=False,
                device_id=resolved_device_id,
                command=command,
                state_before=state_before,
                state_after={},
                error=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
                raw_response=_coerce_raw_response(
                    raw_response,
                    device_ref=device_ref,
                    entry=entry if "entry" in locals() else None,
                ),
            )

    def _apply_command(
        self,
        controller: Any,
        command: str,
        *,
        params: dict[str, Any],
        is_bulb: bool,
    ) -> Any:
        if command == "turn_on":
            return controller.turn_on()
        if command == "turn_off":
            return controller.turn_off()
        if command == "get_state":
            return _read_state(controller, is_bulb)

        if not is_bulb:
            raise NotImplementedError(f"{command} is only supported for Tuya bulbs")

        if command == "set_brightness":
            brightness = _coerce_percentage(
                params,
                field_names=("brightness", "value", "percent", "percentage"),
            )
            return controller.set_brightness_percentage(brightness)
        if command == "set_color_temp":
            color_temp = _coerce_color_temp(params)
            return controller.set_colourtemp_percentage(color_temp)
        if command == "set_color_rgb":
            red, green, blue = _coerce_rgb(params)
            return controller.set_colour(red, green, blue)
        raise ValueError(f"unsupported command: {command!r}")

    def _load_entries(self) -> tuple[Path, list[dict[str, Any]]]:
        return load_configured_devices(config_path=self._config_path or _env_config_path())

    def _resolve_entry(self, device_ref: str) -> dict[str, Any]:
        config_path, devices = self._load_entries()
        entry = find_configured_device(
            [item for item in devices if _looks_like_tuya_entry(item)],
            identifiers=(device_ref,),
        )
        if entry is None:
            raise DeviceConfigurationError(
                f"No configured Tuya device matches {device_ref!r} in {config_path}"
            )

        device_id = str(entry.get("device_id") or "").strip()
        local_key = str(entry.get("local_key") or "").strip()
        ip = str(entry.get("ip") or "").strip()
        if not device_id or not local_key or not ip:
            raise DeviceConfigurationError(
                "Configured Tuya devices require device_id, local_key, and ip."
            )
        version = entry.get("version", 3.3)
        try:
            entry["version"] = float(version)
        except (TypeError, ValueError) as exc:
            raise DeviceConfigurationError(f"Invalid Tuya version for {device_ref!r}: {version!r}") from exc
        return entry


def _build_controller(entry: dict[str, Any], tinytuya: Any) -> tuple[Any, bool]:
    is_bulb = _is_bulb_entry(entry)
    controller_cls = tinytuya.BulbDevice if is_bulb else tinytuya.Device
    controller = controller_cls(
        str(entry["device_id"]),
        address=str(entry["ip"]),
        local_key=str(entry["local_key"]),
        version=float(entry.get("version", 3.3)),
        connection_timeout=5,
    )
    return controller, is_bulb


def _read_state(controller: Any, is_bulb: bool) -> dict[str, Any]:
    raw_state = controller.state() if is_bulb else controller.status()
    if not isinstance(raw_state, dict):
        raise RuntimeError("Tuya device returned a non-dict state payload.")
    if "Error" in raw_state:
        raise RuntimeError(str(raw_state.get("Error") or "Tuya device returned an error state."))
    return raw_state


def _normalize_state(controller: Any, raw_state: dict[str, Any], *, is_bulb: bool) -> dict[str, Any]:
    state: dict[str, Any] = {
        "is_on": bool(raw_state.get("is_on") or raw_state.get("switch")),
    }
    if not is_bulb:
        state["raw_dps"] = dict(raw_state.get("dps") or {})
        return state

    mode = raw_state.get("mode")
    if isinstance(mode, str) and mode.strip():
        state["mode"] = mode.strip()

    try:
        brightness = int(round(float(controller.get_brightness_percentage(state=raw_state))))
    except Exception:  # noqa: BLE001
        brightness = None
    if brightness is not None:
        state["brightness"] = max(0, min(brightness, 100))

    try:
        color_temp = int(round(float(controller.get_colourtemp_percentage(state=raw_state))))
    except Exception:  # noqa: BLE001
        color_temp = None
    if color_temp is not None:
        color_temp = max(0, min(color_temp, 100))
        state["color_temp"] = color_temp
        state["color_temp_preset"] = _preset_for_color_temp(color_temp)

    try:
        rgb = controller.colour_rgb(state=raw_state)
    except Exception:  # noqa: BLE001
        rgb = None
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        state["color_rgb"] = {
            "r": int(rgb[0]),
            "g": int(rgb[1]),
            "b": int(rgb[2]),
        }

    return state


def _normalize_scan_state(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    dps = payload.get("dps")
    if not isinstance(dps, dict):
        return {}
    return {
        "is_on": bool(dps.get("20") or dps.get("1")),
        "raw_dps": dict(dps),
    }


def _coerce_percentage(params: dict[str, Any], *, field_names: tuple[str, ...]) -> int:
    for field_name in field_names:
        value = params.get(field_name)
        if value is None:
            continue
        try:
            percent = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a number between 0 and 100") from exc
        if not 0 <= percent <= 100:
            raise ValueError(f"{field_name} must be between 0 and 100")
        return percent
    joined = ", ".join(field_names)
    raise ValueError(f"One of {joined} is required")


def _coerce_color_temp(params: dict[str, Any]) -> int:
    preset = " ".join(str(params.get("preset") or "").strip().lower().split()).replace(" ", "_")
    if preset:
        if preset not in _COLOR_TEMP_PRESETS:
            raise ValueError(f"Unknown color temperature preset: {preset}")
        return _COLOR_TEMP_PRESETS[preset]
    return _coerce_percentage(
        params,
        field_names=("color_temp", "value", "percent", "percentage"),
    )


def _coerce_rgb(params: dict[str, Any]) -> tuple[int, int, int]:
    rgb = params.get("rgb")
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        values = rgb
    else:
        values = (params.get("r"), params.get("g"), params.get("b"))
    channels: list[int] = []
    for name, value in zip(("r", "g", "b"), values, strict=False):
        try:
            channel = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number between 0 and 255") from exc
        if not 0 <= channel <= 255:
            raise ValueError(f"{name} must be between 0 and 255")
        channels.append(channel)
    return channels[0], channels[1], channels[2]


def _coerce_raw_response(
    payload: Any,
    *,
    device_ref: str,
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(payload, dict):
        raw = dict(payload)
    elif payload is None:
        raw = {}
    else:
        raw = {"response": payload}
    raw.setdefault("device_ref", device_ref)
    if isinstance(entry, dict):
        raw.setdefault("resolved_device_id", str(entry.get("device_id") or ""))
        raw.setdefault("resolved_ip", str(entry.get("ip") or ""))
    return raw


def _configured_device_type(entry: dict[str, Any]) -> str:
    if _is_bulb_entry(entry):
        return "smart_bulb"
    value = str(entry.get("device_type") or "smart_device").strip()
    return value or "smart_device"


def _is_bulb_entry(entry: dict[str, Any]) -> bool:
    text = " ".join(
        str(entry.get(field) or "").strip().lower()
        for field in ("device_type", "controller_type", "name", "platform")
    )
    return any(token in text for token in ("bulb", "light", "lamp", "feit"))


def _looks_like_tuya_entry(entry: dict[str, Any]) -> bool:
    platform = normalize_platform_name(entry)
    if platform in {"tuya", "feit", "feit electric"}:
        return True
    return bool(str(entry.get("device_id") or "").strip()) and bool(str(entry.get("local_key") or "").strip())


def _scan_looks_like_bulb(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(field) or "").strip().lower()
        for field in ("name", "productKey")
    )
    return any(token in text for token in ("bulb", "light", "lamp", "feit"))


def _preset_for_color_temp(color_temp: int) -> str:
    if color_temp <= 10:
        return "warm"
    if color_temp <= 37:
        return "soft_white"
    if color_temp <= 62:
        return "neutral"
    if color_temp <= 87:
        return "cool_white"
    return "daylight"


def _env_config_path() -> Path | None:
    value = os.environ.get(_CONFIG_ENV_VAR)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _import_tinytuya() -> Any:
    try:
        import tinytuya
    except ImportError as exc:
        raise ImportError("tinytuya is not installed; run: pip install tinytuya") from exc
    return tinytuya
