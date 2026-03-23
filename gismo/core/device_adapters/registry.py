"""Adapter registry -- maps device types to their adapter implementations."""
from __future__ import annotations

from typing import Any

from gismo.core.device_adapters.base import DeviceAdapter, AdapterInfo


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, DeviceAdapter] = {}  # name -> adapter

    def register(self, adapter: DeviceAdapter) -> None:
        info = adapter.get_adapter_info()
        self._adapters[info.name] = adapter

    def get_adapter(self, adapter_name: str) -> DeviceAdapter:
        if adapter_name not in self._adapters:
            raise KeyError(f"no adapter registered: {adapter_name!r}")
        return self._adapters[adapter_name]

    def get_adapter_for_device_type(self, device_type: str) -> DeviceAdapter:
        for adapter in self._adapters.values():
            info = adapter.get_adapter_info()
            if device_type in info.device_types:
                return adapter
        raise KeyError(f"no adapter supports device type: {device_type!r}")

    def list_adapters(self) -> list[AdapterInfo]:
        return [a.get_adapter_info() for a in self._adapters.values()]

    def is_registered(self, adapter_name: str) -> bool:
        return adapter_name in self._adapters


# Module-level singleton registry
_registry: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
        _register_defaults(_registry)
    return _registry


def _register_defaults(registry: AdapterRegistry) -> None:
    from gismo.core.device_adapters.tuya_adapter import TuyaAdapter
    from gismo.core.device_adapters.kasa_adapter import KasaAdapter

    try:
        registry.register(TuyaAdapter())
    except Exception:
        pass  # tuya not installed -- adapter silently absent

    try:
        registry.register(KasaAdapter())
    except Exception:
        pass  # kasa not installed -- adapter silently absent
