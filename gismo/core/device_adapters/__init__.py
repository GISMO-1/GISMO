"""GISMO device adapter layer: pluggable hardware protocol adapters."""
from gismo.core.device_adapters.base import (
    AdapterInfo,
    CommandResult,
    DeviceAdapter,
    DeviceInfo,
)
from gismo.core.device_adapters.kasa_adapter import KasaAdapter
from gismo.core.device_adapters.registry import AdapterRegistry, get_registry
from gismo.core.device_adapters.tuya_adapter import TuyaAdapter

__all__ = [
    "AdapterInfo",
    "AdapterRegistry",
    "CommandResult",
    "DeviceAdapter",
    "DeviceInfo",
    "KasaAdapter",
    "TuyaAdapter",
    "get_registry",
]
