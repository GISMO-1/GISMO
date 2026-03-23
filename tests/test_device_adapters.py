import asyncio
import json
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from uuid import uuid4

from gismo.core import device_runtime
from gismo.core.device_adapters.base import (
    AdapterInfo,
    CommandResult,
    DeviceAdapter,
    DeviceInfo,
)
from gismo.core.device_adapters.registry import AdapterRegistry
from gismo.core.device_adapters.kasa_adapter import KasaAdapter, _SUPPORTED_COMMANDS
from gismo.core.device_adapters.tuya_adapter import TuyaAdapter
from gismo.core.models import ConnectedDevice


def _patch_run_async(result: object) -> mock._patch:
    """Patch _run_async so it closes any abandoned coroutine before returning.

    When send_command is tested, ``self._send_command_async(...)`` creates a
    coroutine that gets passed to ``_run_async``.  If ``_run_async`` is patched
    with a plain ``return_value`` the coroutine is never awaited, triggering
    ``RuntimeWarning: coroutine was never awaited``.  This helper explicitly
    closes the coroutine so Python's GC does not report the leak.
    """
    def _impl(coro: object) -> object:
        if hasattr(coro, "close"):
            coro.close()
        return result

    return mock.patch(
        "gismo.core.device_adapters.kasa_adapter._run_async",
        side_effect=_impl,
    )


class _StubAdapter(DeviceAdapter):
    """Minimal adapter for registry tests."""

    def __init__(self, name: str = "stub", device_types: tuple[str, ...] = ("stub_device",)) -> None:
        self._name = name
        self._device_types = device_types

    def get_adapter_info(self) -> AdapterInfo:
        return AdapterInfo(
            name=self._name,
            version="0.0.1",
            device_types=self._device_types,
            trust_zone="device_adapter",
            required_permissions=("device.control",),
            supports_discovery=False,
        )

    def discover(self, *, timeout_seconds: float = 5.0) -> list[DeviceInfo]:
        return []

    def get_state(self, device_ref: str) -> dict:
        return {}

    def send_command(self, device_ref: str, command: str, params=None) -> CommandResult:
        return CommandResult(ok=True, device_id=device_ref, command=command, state_before={}, state_after={})


class AdapterRegistryTest(unittest.TestCase):
    def test_registry_register_and_lookup(self) -> None:
        registry = AdapterRegistry()
        adapter = _StubAdapter("test_adapter")
        registry.register(adapter)
        self.assertIs(registry.get_adapter("test_adapter"), adapter)

    def test_registry_lookup_unknown_raises(self) -> None:
        registry = AdapterRegistry()
        with self.assertRaises(KeyError):
            registry.get_adapter("nonexistent")

    def test_registry_list_adapters(self) -> None:
        registry = AdapterRegistry()
        registry.register(_StubAdapter("a"))
        registry.register(_StubAdapter("b"))
        infos = registry.list_adapters()
        names = {info.name for info in infos}
        self.assertEqual(names, {"a", "b"})

    def test_registry_get_adapter_for_device_type(self) -> None:
        registry = AdapterRegistry()
        registry.register(_StubAdapter("plug_adapter", ("smart_plug",)))
        registry.register(_StubAdapter("bulb_adapter", ("smart_bulb",)))
        adapter = registry.get_adapter_for_device_type("smart_plug")
        self.assertEqual(adapter.get_adapter_info().name, "plug_adapter")

    def test_registry_get_adapter_for_unknown_device_type_raises(self) -> None:
        registry = AdapterRegistry()
        registry.register(_StubAdapter("x"))
        with self.assertRaises(KeyError):
            registry.get_adapter_for_device_type("unknown_type")

    def test_registry_is_registered(self) -> None:
        registry = AdapterRegistry()
        registry.register(_StubAdapter("present"))
        self.assertTrue(registry.is_registered("present"))
        self.assertFalse(registry.is_registered("absent"))


def _make_mock_kasa_device(*, mac: str = "AA:BB:CC:DD:EE:FF", alias: str = "Test Plug",
                            model: str = "HS103", is_on: bool = False,
                            has_emeter: bool = False) -> MagicMock:
    device = MagicMock()
    device.mac = mac
    device.alias = alias
    device.model = model
    device.is_on = is_on
    device.update = AsyncMock()
    device.turn_on = AsyncMock()
    device.turn_off = AsyncMock()
    if has_emeter:
        device.get_emeter_realtime = AsyncMock(return_value={"power_mw": 1200})
    else:
        if hasattr(device, "get_emeter_realtime"):
            del device.get_emeter_realtime
        device.configure_mock(**{"get_emeter_realtime": None})
        # Remove attribute to simulate device without emeter
        spec_attrs = {k: v for k, v in vars(device).items() if k != "get_emeter_realtime"}
    # For has_emeter=False, we need emeter_realtime to not exist
    if not has_emeter:
        type(device).emeter_realtime = PropertyMock(side_effect=AttributeError)
        try:
            delattr(type(device), "get_emeter_realtime")
        except AttributeError:
            pass
    return device


class KasaAdapterTest(unittest.TestCase):
    def test_adapter_info_is_correct(self) -> None:
        adapter = KasaAdapter()
        info = adapter.get_adapter_info()
        self.assertEqual(info.name, "kasa")
        self.assertEqual(info.trust_zone, "device_adapter")
        self.assertIn("smart_plug", info.device_types)
        self.assertTrue(info.supports_discovery)
        self.assertTrue(info.supports_emeter)

    def test_discover_returns_device_list(self) -> None:
        mock_device = MagicMock()
        mock_device.mac = "AA:BB:CC:DD:EE:FF"
        mock_device.alias = "Living Room Plug"
        mock_device.model = "HS103"
        mock_device.is_on = True
        # Use a plain MagicMock for update — _run_async is patched so the
        # coroutine is never executed; AsyncMock would leave an unawaited
        # coroutine and trigger RuntimeWarning.
        mock_device.update = MagicMock(return_value=None)

        fake_kasa = MagicMock()
        fake_kasa.Discover.discover = AsyncMock(return_value={"192.168.1.50": mock_device})

        adapter = KasaAdapter()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with patch("gismo.core.device_adapters.kasa_adapter._run_async") as run_async:
                # Calls in order: Discover.discover(), device.update(),
                # and potentially device.get_emeter_realtime() if MagicMock
                # reports hasattr as True — provide a safe fallback value.
                run_async.side_effect = [
                    {"192.168.1.50": mock_device},  # Discover.discover()
                    None,                            # device.update()
                    {},                              # get_emeter_realtime() fallback
                ]
                devices = adapter.discover(timeout_seconds=3.0)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].device_id, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(devices[0].alias, "Living Room Plug")
        self.assertTrue(devices[0].is_online)

    def test_discover_handles_empty_result(self) -> None:
        adapter = KasaAdapter()
        with patch.dict("sys.modules", {"kasa": MagicMock()}):
            with patch("gismo.core.device_adapters.kasa_adapter._run_async", return_value={}):
                devices = adapter.discover()
        self.assertEqual(devices, [])

    def test_discover_handles_network_failure_gracefully(self) -> None:
        adapter = KasaAdapter()
        with patch.dict("sys.modules", {"kasa": MagicMock()}):
            with patch("gismo.core.device_adapters.kasa_adapter._run_async", side_effect=OSError("network down")):
                devices = adapter.discover()
        self.assertEqual(devices, [])

    def test_send_command_turn_on(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=True, device_id="AA:BB:CC", command="turn_on",
            state_before={"is_on": False}, state_after={"is_on": True},
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("AA:BB:CC", "turn_on")
        self.assertTrue(result.ok)
        self.assertEqual(result.command, "turn_on")
        self.assertEqual(result.state_after, {"is_on": True})

    def test_send_command_turn_off(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=True, device_id="AA:BB:CC", command="turn_off",
            state_before={"is_on": True}, state_after={"is_on": False},
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("AA:BB:CC", "turn_off")
        self.assertTrue(result.ok)
        self.assertEqual(result.command, "turn_off")
        self.assertEqual(result.state_after, {"is_on": False})

    def test_send_command_toggle_when_on(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=True, device_id="dev1", command="toggle",
            state_before={"is_on": True}, state_after={"is_on": False},
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("dev1", "toggle")
        self.assertTrue(result.ok)
        self.assertEqual(result.state_before, {"is_on": True})
        self.assertEqual(result.state_after, {"is_on": False})

    def test_send_command_toggle_when_off(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=True, device_id="dev1", command="toggle",
            state_before={"is_on": False}, state_after={"is_on": True},
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("dev1", "toggle")
        self.assertTrue(result.ok)
        self.assertEqual(result.state_after, {"is_on": True})

    def test_send_command_unknown_device_returns_error(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=False, device_id="ghost", command="turn_on",
            state_before={}, state_after={},
            error="device not found: 'ghost'", error_type="KeyError",
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("ghost", "turn_on")
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_send_command_unsupported_command_returns_error(self) -> None:
        adapter = KasaAdapter()
        result = adapter.send_command("dev1", "explode")
        self.assertFalse(result.ok)
        self.assertIn("unsupported command", result.error)
        self.assertEqual(result.error_type, "ValueError")

    def test_send_command_device_unreachable_returns_error(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=False, device_id="dev1", command="turn_on",
            state_before={}, state_after={},
            error="Connection timed out", error_type="TimeoutError",
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("dev1", "turn_on")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "TimeoutError")

    def test_get_energy_when_not_supported_returns_error(self) -> None:
        adapter = KasaAdapter()
        result_obj = CommandResult(
            ok=False, device_id="dev1", command="get_energy",
            state_before={"is_on": True}, state_after={"is_on": True},
            error="device does not support energy monitoring",
            error_type="NotImplementedError",
        )
        fake_kasa = MagicMock()
        with patch.dict("sys.modules", {"kasa": fake_kasa}):
            with _patch_run_async(result_obj):
                result = adapter.send_command("dev1", "get_energy")
        self.assertFalse(result.ok)
        self.assertIn("energy monitoring", result.error)

    def test_kasa_not_installed_returns_error(self) -> None:
        adapter = KasaAdapter()
        # Remove kasa from sys.modules to trigger ImportError
        import sys
        saved = sys.modules.get("kasa")
        sys.modules["kasa"] = None  # type: ignore[assignment]
        try:
            # send_command tries to import kasa
            with patch.dict("sys.modules", {"kasa": None}):
                # The import will fail because kasa is None in modules
                with patch("builtins.__import__", side_effect=ImportError("No module named 'kasa'")):
                    result = adapter.send_command("dev1", "turn_on")
            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, "ImportError")
        finally:
            if saved is not None:
                sys.modules["kasa"] = saved


def _write_tuya_config(tmpdir: Path, devices: list[dict]) -> Path:
    config_dir = tmpdir / ".gismo"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "devices.json"
    config_path.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return config_path


class _FakeBulbController:
    def __init__(
        self,
        *,
        is_on: bool = False,
        brightness: int = 0,
        color_temp: int = 0,
        rgb: tuple[int, int, int] = (255, 255, 255),
    ) -> None:
        self.is_on = is_on
        self.brightness = brightness
        self.color_temp = color_temp
        self.rgb = rgb
        self.mode = "colour" if rgb != (255, 255, 255) and is_on else "white"
        self.calls: list[tuple[object, ...]] = []

    def state(self) -> dict[str, object]:
        return {
            "is_on": self.is_on,
            "switch": self.is_on,
            "mode": self.mode,
        }

    def turn_on(self) -> dict[str, object]:
        self.is_on = True
        self.calls.append(("turn_on",))
        return {"ok": True}

    def turn_off(self) -> dict[str, object]:
        self.is_on = False
        self.calls.append(("turn_off",))
        return {"ok": True}

    def set_brightness_percentage(self, brightness: int) -> dict[str, object]:
        self.brightness = brightness
        self.mode = "white"
        self.is_on = True
        self.calls.append(("set_brightness_percentage", brightness))
        return {"ok": True, "brightness": brightness}

    def set_colourtemp_percentage(self, color_temp: int) -> dict[str, object]:
        self.color_temp = color_temp
        self.mode = "white"
        self.is_on = True
        self.calls.append(("set_colourtemp_percentage", color_temp))
        return {"ok": True, "color_temp": color_temp}

    def set_colour(self, red: int, green: int, blue: int) -> dict[str, object]:
        self.rgb = (red, green, blue)
        self.mode = "colour"
        self.is_on = True
        self.calls.append(("set_colour", red, green, blue))
        return {"ok": True, "rgb": [red, green, blue]}

    def get_brightness_percentage(self, state=None) -> int:
        return self.brightness

    def get_colourtemp_percentage(self, state=None) -> int:
        return self.color_temp

    def colour_rgb(self, state=None) -> tuple[int, int, int]:
        return self.rgb


def _make_fake_tinytuya(
    *,
    bulb_controller: object | None = None,
    generic_controller: object | None = None,
    scan_result: dict[str, dict] | None = None,
) -> MagicMock:
    fake_tinytuya = MagicMock()
    fake_tinytuya.BulbDevice = MagicMock(return_value=bulb_controller or _FakeBulbController())
    fake_tinytuya.Device = MagicMock(return_value=generic_controller or MagicMock())
    fake_tinytuya.deviceScan = MagicMock(return_value=scan_result or {})
    return fake_tinytuya


class TuyaAdapterTest(unittest.TestCase):
    def _configured_adapter(self, tmpdir: Path, *, devices: list[dict] | None = None) -> tuple[TuyaAdapter, Path]:
        config_path = _write_tuya_config(
            tmpdir,
            devices
            or [
                {
                    "name": "Dad's Room Light",
                    "platform": "tuya",
                    "device_type": "smart_bulb",
                    "device_id": "tuya-bulb-1",
                    "local_key": "secret-key",
                    "ip": "192.168.1.188",
                    "version": 3.3,
                }
            ],
        )
        return TuyaAdapter(config_path=config_path), config_path

    def test_adapter_info_is_correct(self) -> None:
        adapter = TuyaAdapter(config_path=Path("tmp") / "devices.json")
        info = adapter.get_adapter_info()
        self.assertEqual(info.name, "tuya")
        self.assertEqual(info.trust_zone, "device_adapter")
        self.assertIn("smart_bulb", info.device_types)
        self.assertTrue(info.supports_discovery)

    def test_discover_merges_configured_and_scanned_devices(self) -> None:
        tmpdir = Path("tmp") / f"tuya-discover-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            fake_tinytuya = _make_fake_tinytuya(
                scan_result={
                    "192.168.1.188": {
                        "gwId": "tuya-bulb-1",
                        "name": "Dad's Room Light",
                        "productKey": "feit-bulb",
                        "dps": {"20": True},
                    }
                }
            )
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                devices = adapter.discover(timeout_seconds=2.0)
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].device_id, "tuya-bulb-1")
            self.assertEqual(devices[0].host, "192.168.1.188")
            self.assertTrue(devices[0].is_online)
            self.assertTrue(devices[0].state["is_on"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_send_command_turn_on(self) -> None:
        tmpdir = Path("tmp") / f"tuya-on-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=False)
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command("192.168.1.188", "turn_on")
            self.assertTrue(result.ok)
            self.assertEqual(result.device_id, "tuya-bulb-1")
            self.assertEqual(controller.calls[-1], ("turn_on",))
            self.assertTrue(result.state_after["is_on"])
            self.assertEqual(result.raw_response["device_ref"], "192.168.1.188")
            self.assertEqual(result.raw_response["resolved_device_id"], "tuya-bulb-1")
            self.assertEqual(result.raw_response["resolved_ip"], "192.168.1.188")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_send_command_turn_off(self) -> None:
        tmpdir = Path("tmp") / f"tuya-off-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=True)
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command("192.168.1.188", "turn_off")
            self.assertTrue(result.ok)
            self.assertEqual(controller.calls[-1], ("turn_off",))
            self.assertFalse(result.state_after["is_on"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_brightness_maps_percentage(self) -> None:
        tmpdir = Path("tmp") / f"tuya-bright-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=True)
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command(
                    "192.168.1.188",
                    "set_brightness",
                    {"brightness": 35},
                )
            self.assertTrue(result.ok)
            self.assertIn(("set_brightness_percentage", 35), controller.calls)
            self.assertEqual(result.state_after["brightness"], 35)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_color_temp_maps_presets(self) -> None:
        tmpdir = Path("tmp") / f"tuya-temp-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=True)
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command(
                    "192.168.1.188",
                    "set_color_temp",
                    {"preset": "daylight"},
                )
            self.assertTrue(result.ok)
            self.assertIn(("set_colourtemp_percentage", 100), controller.calls)
            self.assertEqual(result.state_after["color_temp"], 100)
            self.assertEqual(result.state_after["color_temp_preset"], "daylight")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_color_rgb_handles_rgb_values(self) -> None:
        tmpdir = Path("tmp") / f"tuya-rgb-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=True, rgb=(0, 0, 0))
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command(
                    "192.168.1.188",
                    "set_color_rgb",
                    {"r": 12, "g": 34, "b": 56},
                )
            self.assertTrue(result.ok)
            self.assertIn(("set_colour", 12, 34, 56), controller.calls)
            self.assertEqual(result.state_after["color_rgb"], {"r": 12, "g": 34, "b": 56})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_state_returns_normalized_bulb_state(self) -> None:
        tmpdir = Path("tmp") / f"tuya-state-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(
                is_on=True,
                brightness=42,
                color_temp=75,
                rgb=(10, 20, 30),
            )
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                state = adapter.get_state("tuya-bulb-1")
            self.assertTrue(state["is_on"])
            self.assertEqual(state["brightness"], 42)
            self.assertEqual(state["color_temp"], 75)
            self.assertEqual(state["color_rgb"], {"r": 10, "g": 20, "b": 30})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_unknown_device_returns_configuration_error(self) -> None:
        tmpdir = Path("tmp") / f"tuya-missing-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            fake_tinytuya = _make_fake_tinytuya()
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command("192.168.1.199", "turn_on")
            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, "DeviceConfigurationError")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_unreachable_device_returns_error(self) -> None:
        tmpdir = Path("tmp") / f"tuya-unreach-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            adapter, _ = self._configured_adapter(tmpdir)
            controller = _FakeBulbController(is_on=True)
            controller.turn_on = MagicMock(side_effect=TimeoutError("connection timed out"))  # type: ignore[method-assign]
            fake_tinytuya = _make_fake_tinytuya(bulb_controller=controller)
            with patch.dict("sys.modules", {"tinytuya": fake_tinytuya}):
                result = adapter.send_command("192.168.1.188", "turn_on")
            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, "TimeoutError")
            self.assertIn("timed out", result.error)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class KasaAdapterIntegrationTest(unittest.TestCase):
    """Test the adapter wired into the existing device_tool infrastructure."""

    def _build_orchestrator(self, db_path: str):
        from gismo.core.agent import SimpleAgent
        from gismo.core.orchestrator import Orchestrator
        from gismo.core.permissions import PermissionPolicy
        from gismo.core.state import StateStore
        from gismo.core.toolpacks.device_tool import DeviceControlTool
        from gismo.core.tools import ToolRegistry

        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(DeviceControlTool(state_store))
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=PermissionPolicy(allowed_tools={"device_control"}),
            agent=SimpleAgent(registry=registry),
        )
        return state_store, orchestrator

    def test_kasa_command_routed_through_device_tool(self) -> None:
        tmpdir = Path("tmp") / f"kasa-integ-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="kasa-test", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Kasa turn on",
                description="Turn on smart plug via kasa",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "AA:BB:CC:DD:EE:FF",
                        "command": "turn_on",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "AA:BB:CC:DD:EE:FF",
                        "command": "turn_on",
                        "state_before": {"is_on": False},
                        "state_after": {"is_on": True},
                        "error": None,
                        "error_type": None,
                        "raw_response": {"is_on": True},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                from gismo.core.models import TaskStatus
                result = orchestrator.run_tool(
                    run.id, task, "device_control",
                    {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "AA:BB:CC:DD:EE:FF",
                        "command": "turn_on",
                    },
                )
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("Successfully sent turn_on", result.output_json.get("summary", ""))
            runtime.assert_called_once()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_kasa_command_emits_security_event(self) -> None:
        tmpdir = Path("tmp") / f"kasa-event-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="kasa-event", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Kasa turn off",
                description="Turn off smart plug via kasa",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "dev-1",
                        "command": "turn_off",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "dev-1",
                        "command": "turn_off",
                        "state_before": {"is_on": True},
                        "state_after": {"is_on": False},
                        "error": None,
                        "error_type": None,
                        "raw_response": {},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ):
                orchestrator.run_tool(
                    run.id, task, "device_control",
                    {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "dev-1",
                        "command": "turn_off",
                    },
                )
            events = state_store.list_events(limit=20)
            event_types = [e.event_type for e in events]
            self.assertIn("device_command_sent", event_types)
            self.assertIn("device_command_succeeded", event_types)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_kasa_command_executes_in_device_adapter_zone(self) -> None:
        tmpdir = Path("tmp") / f"kasa-zone-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="kasa-zone", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Kasa get state",
                description="Get state via kasa",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "dev-1",
                        "command": "get_state",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "dev-1",
                        "command": "get_state",
                        "state_before": {"is_on": True},
                        "state_after": {"is_on": True},
                        "error": None,
                        "error_type": None,
                        "raw_response": {},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                from gismo.core.models import TaskStatus
                result = orchestrator.run_tool(
                    run.id, task, "device_control",
                    {
                        "action": "kasa_command",
                        "adapter": "kasa",
                        "device_id": "dev-1",
                        "command": "get_state",
                    },
                )
            self.assertEqual(result.output_json["execution"]["zone"], "device_adapter")
            self.assertEqual(result.output_json["execution"]["mode"], "sandboxed")
            # Verify the runtime was called with component="device_control" (device_adapter zone)
            call_kwargs = runtime.call_args
            self.assertEqual(call_kwargs.kwargs.get("component") or call_kwargs[1].get("component", ""), "device_control")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TuyaAdapterIntegrationTest(unittest.TestCase):
    """Test Tuya adapter routing through the generic device command path."""

    def _build_orchestrator(self, db_path: str):
        from gismo.core.agent import SimpleAgent
        from gismo.core.orchestrator import Orchestrator
        from gismo.core.permissions import PermissionPolicy
        from gismo.core.state import StateStore
        from gismo.core.toolpacks.device_tool import DeviceControlTool
        from gismo.core.tools import ToolRegistry

        state_store = StateStore(db_path)
        registry = ToolRegistry()
        registry.register(DeviceControlTool(state_store))
        orchestrator = Orchestrator(
            state_store=state_store,
            registry=registry,
            policy=PermissionPolicy(allowed_tools={"device_control"}),
            agent=SimpleAgent(registry=registry),
        )
        return state_store, orchestrator

    def test_device_command_routed_through_device_tool(self) -> None:
        tmpdir = Path("tmp") / f"tuya-integ-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="tuya-test", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Tuya turn off",
                description="Turn off smart bulb via tuya",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "turn_off",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "192.168.1.188",
                        "command": "turn_off",
                        "state_before": {"is_on": True},
                        "state_after": {"is_on": False},
                        "error": None,
                        "error_type": None,
                        "raw_response": {"ok": True},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                from gismo.core.models import TaskStatus
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "turn_off",
                    },
                )
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("Successfully sent turn_off", result.output_json.get("summary", ""))
            runtime.assert_called_once()
            self.assertEqual(runtime.call_args.kwargs["action"], "device_command")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_device_command_emits_security_event(self) -> None:
        tmpdir = Path("tmp") / f"tuya-event-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="tuya-event", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Tuya state",
                description="Get smart bulb state via tuya",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "get_state",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "192.168.1.188",
                        "command": "get_state",
                        "state_before": {"is_on": True},
                        "state_after": {"is_on": True},
                        "error": None,
                        "error_type": None,
                        "raw_response": {"is_on": True},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ):
                orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "get_state",
                    },
                )
            events = state_store.list_events(limit=20)
            event_types = [event.event_type for event in events]
            self.assertIn("device_command_sent", event_types)
            self.assertIn("device_command_succeeded", event_types)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_device_command_executes_in_device_adapter_zone(self) -> None:
        tmpdir = Path("tmp") / f"tuya-zone-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="tuya-zone", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Tuya brightness",
                description="Adjust brightness via tuya",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "set_brightness",
                        "params": {"brightness": 25},
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_id": "192.168.1.188",
                        "command": "set_brightness",
                        "state_before": {"is_on": True, "brightness": 60},
                        "state_after": {"is_on": True, "brightness": 25},
                        "error": None,
                        "error_type": None,
                        "raw_response": {"ok": True},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                from gismo.core.models import TaskStatus
                result = orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_id": "192.168.1.188",
                        "command": "set_brightness",
                        "params": {"brightness": 25},
                    },
                )
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.output_json["execution"]["zone"], "device_adapter")
            self.assertEqual(result.output_json["execution"]["mode"], "sandboxed")
            self.assertEqual(runtime.call_args.kwargs["component"], "device_control")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_device_command_accepts_device_ref_and_records_canonical_identity(self) -> None:
        tmpdir = Path("tmp") / f"tuya-device-ref-{uuid4().hex}"
        tmpdir.mkdir(parents=True, exist_ok=False)
        try:
            db_path = str(tmpdir / "state.db")
            state_store, orchestrator = self._build_orchestrator(db_path)
            run = state_store.create_run(label="tuya-device-ref", metadata={})
            task = state_store.create_task(
                run_id=run.id,
                title="Tuya turn off",
                description="Turn off smart bulb via device_ref",
                input_json={
                    "tool": "device_control",
                    "payload": {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_ref": "Dad's Room Light",
                        "command": "turn_off",
                    },
                },
            )
            with mock.patch(
                "gismo.core.toolpacks.device_tool.execute_device_runtime_action",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "device_ref": "Dad's Room Light",
                        "device_id": "tuya-bulb-1",
                        "command": "turn_off",
                        "state_before": {"is_on": True},
                        "state_after": {"is_on": False},
                        "error": None,
                        "error_type": None,
                        "raw_response": {"device_ref": "Dad's Room Light", "resolved_device_id": "tuya-bulb-1"},
                    },
                    "execution": {"mode": "sandboxed", "zone": "device_adapter"},
                },
            ) as runtime:
                orchestrator.run_tool(
                    run.id,
                    task,
                    "device_control",
                    {
                        "action": "device_command",
                        "adapter": "tuya",
                        "device_ref": "Dad's Room Light",
                        "command": "turn_off",
                    },
                )
            self.assertEqual(runtime.call_args.kwargs["payload"]["device_ref"], "Dad's Room Light")
            self.assertNotIn("device_id", runtime.call_args.kwargs["payload"])
            sent_event = state_store.list_events_by_type("device_command_sent")[0]
            success_event = state_store.list_events_by_type("device_command_succeeded")[0]
            self.assertEqual(sent_event.json_payload["device_ref"], "Dad's Room Light")
            self.assertEqual(sent_event.json_payload["device_id"], "Dad's Room Light")
            self.assertEqual(success_event.json_payload["device_ref"], "Dad's Room Light")
            self.assertEqual(success_event.json_payload["device_id"], "tuya-bulb-1")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class DeviceRuntimeIdentityTest(unittest.TestCase):
    def test_set_light_power_prefers_stable_device_ref_before_ip(self) -> None:
        device = ConnectedDevice(
            id="saved-device-1",
            ip="192.168.1.188",
            hostname="dad-room-light",
            device_type="light",
            brand="FEIT",
            metadata_json={
                "label": "Dad's Room Light",
                "adapter": "tuya",
                "device_id": "tuya-bulb-1",
                "gismo_device_id": "gismo-dad-room-light",
            },
        )
        attempted_refs: list[str] = []

        def _runtime_device_command(*, device_ref: str, **kwargs: object) -> dict[str, object]:
            attempted_refs.append(device_ref)
            return {
                "result": {
                    "ok": True,
                    "device_id": "tuya-bulb-1",
                    "command": kwargs.get("command"),
                    "error": None,
                    "error_type": None,
                }
            }

        with patch("gismo.core.device_runtime.runtime_device_command", side_effect=_runtime_device_command):
            result = device_runtime._set_light_power(device, turn_on=False)

        self.assertEqual(result["status"], "changed")
        self.assertEqual(attempted_refs, ["tuya-bulb-1"])

    def test_set_light_power_falls_back_to_ip_for_bootstrap(self) -> None:
        device = ConnectedDevice(
            id="saved-device-1",
            ip="192.168.1.188",
            hostname="dad-room-light",
            device_type="light",
            brand="FEIT",
            metadata_json={
                "label": "Dad's Room Light",
                "adapter": "tuya",
            },
        )
        attempted_refs: list[str] = []

        def _runtime_device_command(*, device_ref: str, **kwargs: object) -> dict[str, object]:
            attempted_refs.append(device_ref)
            if device_ref == "saved-device-1":
                return {
                    "result": {
                        "ok": False,
                        "device_id": device_ref,
                        "command": kwargs.get("command"),
                        "error": "No configured Tuya device matches 'saved-device-1' in tmp",
                        "error_type": "DeviceConfigurationError",
                    }
                }
            return {
                "result": {
                    "ok": True,
                    "device_id": "tuya-bulb-1",
                    "command": kwargs.get("command"),
                    "error": None,
                    "error_type": None,
                }
            }

        with patch("gismo.core.device_runtime.runtime_device_command", side_effect=_runtime_device_command):
            result = device_runtime._set_light_power(device, turn_on=False)

        self.assertEqual(result["status"], "changed")
        self.assertEqual(attempted_refs, ["saved-device-1", "192.168.1.188"])

    def test_run_device_worker_accepts_legacy_device_id_payload(self) -> None:
        with patch(
            "gismo.core.device_runtime.runtime_device_command",
            return_value={"ok": True, "result": {"ok": True}},
        ) as runtime:
            device_runtime.run_device_worker(
                {
                    "action": "device_command",
                    "adapter": "tuya",
                    "device_id": "legacy-device-ref",
                    "command": "turn_off",
                    "params": {},
                }
            )

        self.assertEqual(runtime.call_args.kwargs["device_ref"], "legacy-device-ref")


if __name__ == "__main__":
    unittest.main()
