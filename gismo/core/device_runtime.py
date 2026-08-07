"""Marshaled device runtime helpers for isolated adapter execution."""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from gismo.core.execution import (
    build_execution_request,
    build_sandbox_profile,
    build_worker_command,
    run_sandboxed_process,
)
from gismo.core.device_adapters.config import (
    find_configured_device,
    load_configured_devices,
    normalize_platform_name,
    ordered_identifiers,
)
from gismo.core.models import ConnectedDevice
from gismo.core.paths import resolve_devices_config_path


def serialize_device(device: ConnectedDevice) -> dict[str, Any]:
    return {
        "id": device.id,
        "ip": device.ip,
        "hostname": device.hostname,
        "device_type": device.device_type,
        "brand": device.brand,
        "rtsp_url": device.rtsp_url,
        "snapshot_url": device.snapshot_url,
        "metadata_json": dict(device.metadata_json),
    }


def deserialize_device(payload: dict[str, Any]) -> ConnectedDevice:
    return ConnectedDevice(
        id=str(payload.get("id") or ConnectedDevice(ip="0.0.0.0", device_type="device", brand="Unknown").id),
        ip=str(payload.get("ip") or ""),
        hostname=str(payload.get("hostname") or "") or None,
        device_type=str(payload.get("device_type") or "smart device"),
        brand=str(payload.get("brand") or "Unknown"),
        rtsp_url=str(payload.get("rtsp_url") or "") or None,
        snapshot_url=str(payload.get("snapshot_url") or "") or None,
        metadata_json=(
            dict(payload.get("metadata_json"))
            if isinstance(payload.get("metadata_json"), dict)
            else {}
        ),
    )


def execute_device_runtime_action(
    *,
    component: str,
    action: str,
    actor: str,
    db_path: str | None,
    payload: dict[str, Any],
    timeout_s: float,
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
) -> dict[str, Any]:
    request = build_execution_request(
        component=component,
        action=action,
        actor=actor,
        db_path=db_path,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
    )
    profile = build_sandbox_profile(
        component=component,
        db_path=db_path,
        extra_env={
            "GISMO_DEVICES_CONFIG": str(resolve_devices_config_path(db_path)),
        }
        if db_path
        else None,
    )
    return run_sandboxed_process(
        request,
        worker_command=build_worker_command("device"),
        worker_input={
            "action": action,
            **payload,
        },
        timeout_s=timeout_s,
        sandbox_profile=profile,
        event_details={"runtime": "device_adapter"},
    )


def run_device_worker(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    if action == "scan_network":
        saved_devices = _load_devices(payload.get("saved_devices"))
        timeout_seconds = _require_timeout(payload.get("timeout_seconds"))
        return {
            "ok": True,
            "devices": runtime_scan_network(saved_devices, timeout_seconds=timeout_seconds),
        }
    if action == "device_status":
        devices = _load_devices(payload.get("devices"))
        return {
            "ok": True,
            "devices": runtime_device_status(devices),
        }
    if action == "device_power":
        devices = _load_devices(payload.get("devices"))
        turn_on = bool(payload.get("turn_on"))
        return {
            "ok": True,
            **runtime_set_power(devices, turn_on=turn_on),
        }
    if action == "device_target_command":
        devices = _load_devices(payload.get("devices"))
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return {
            "ok": True,
            **runtime_set_light_command(
                devices,
                command=str(payload.get("command") or ""),
                params=params,
            ),
        }
    if action in {"device_command", "kasa_command"}:
        return runtime_device_command(
            adapter_name=str(payload.get("adapter") or ("kasa" if action == "kasa_command" else "")).strip(),
            device_ref=str(payload.get("device_ref") or payload.get("device_id") or ""),
            command=str(payload.get("command") or ""),
            params=payload.get("params") or {},
        )
    raise ValueError(f"Unsupported device runtime action: {action}")


def runtime_scan_network(
    saved_devices: list[ConnectedDevice],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    saved = {device.ip: device for device in saved_devices}
    merged: dict[str, dict[str, Any]] = {}
    networks = _local_networks()

    for ip in _read_arp_table(timeout_seconds=min(2.0, max(0.2, deadline - time.monotonic()))):
        if not _is_local_ip(ip, networks):
            continue
        merged[ip] = _infer_device_identity(ip, _safe_hostname(ip))

    if time.monotonic() < deadline and networks:
        for ip in _ping_sweep(networks, deadline):
            current = merged.get(ip, {})
            current.update(_infer_device_identity(ip, current.get("hostname") or _safe_hostname(ip)))
            merged[ip] = current

    results: list[dict[str, Any]] = []
    for ip, item in sorted(merged.items()):
        saved_device = saved.get(ip)
        saved_meta = saved_device.metadata_json if saved_device else {}
        results.append(
            {
                "ip": ip,
                "hostname": item.get("hostname") or (saved_device.hostname if saved_device else ip) or ip,
                "device_type": item.get("device_type") or (saved_device.device_type if saved_device else "smart device"),
                "brand": item.get("brand") or (saved_device.brand if saved_device else "Unknown"),
                "rtsp_url": item.get("rtsp_url") or (saved_device.rtsp_url if saved_device else None),
                "snapshot_url": item.get("snapshot_url") or (saved_device.snapshot_url if saved_device else None),
                "open_ports": item.get("open_ports") or (saved_meta.get("open_ports", []) if isinstance(saved_meta, dict) else []),
                "saved": saved_device is not None,
                "saved_id": saved_device.id if saved_device else None,
            }
        )
    return results


def runtime_device_status(devices: list[ConnectedDevice]) -> list[dict[str, Any]]:
    return [_device_snapshot(device) for device in devices]


def runtime_set_power(
    devices: list[ConnectedDevice],
    *,
    turn_on: bool,
) -> dict[str, Any]:
    changed: list[str] = []
    needs_setup: list[str] = []
    unsupported: list[str] = []
    failed: list[str] = []
    details: list[dict[str, Any]] = []

    for device in devices:
        snapshot = _device_snapshot(device)
        if not _looks_like_light(device):
            unsupported.append(snapshot["name"])
            snapshot["control"] = "unsupported"
            details.append(snapshot)
            continue
        outcome = _set_light_power(device, turn_on=turn_on)
        snapshot["control"] = outcome["status"]
        snapshot["confirmed"] = bool(outcome.get("confirmed"))
        if isinstance(outcome.get("verified_state"), dict):
            snapshot["verified_state"] = dict(outcome["verified_state"])
        if outcome["status"] == "changed":
            changed.append(snapshot["name"])
        elif outcome["status"] == "needs_setup":
            needs_setup.append(snapshot["name"])
        else:
            failed.append(outcome["message"] or snapshot["name"])
        details.append(snapshot)

    return {
        "devices": details,
        "changed": changed,
        "needs_setup": needs_setup,
        "failed": failed,
        "unsupported": unsupported,
    }


def runtime_set_light_command(
    devices: list[ConnectedDevice],
    *,
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    changed: list[str] = []
    needs_setup: list[str] = []
    unsupported: list[str] = []
    failed: list[str] = []
    details: list[dict[str, Any]] = []

    for device in devices:
        snapshot = _device_snapshot(device)
        if not _looks_like_light(device):
            unsupported.append(snapshot["name"])
            snapshot["control"] = "unsupported"
            details.append(snapshot)
            continue
        outcome = _run_light_command(device, command=command, params=params)
        snapshot["control"] = outcome["status"]
        snapshot["confirmed"] = bool(outcome.get("confirmed"))
        if isinstance(outcome.get("verified_state"), dict):
            snapshot["verified_state"] = dict(outcome["verified_state"])
        if outcome["status"] == "changed":
            changed.append(snapshot["name"])
        elif outcome["status"] == "needs_setup":
            needs_setup.append(snapshot["name"])
        else:
            failed.append(outcome["message"] or snapshot["name"])
        details.append(snapshot)

    return {
        "devices": details,
        "changed": changed,
        "needs_setup": needs_setup,
        "failed": failed,
        "unsupported": unsupported,
    }


def runtime_device_command(
    *,
    adapter_name: str,
    device_ref: str,
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Execute a command through the adapter registry (runs inside worker subprocess)."""
    from gismo.core.device_adapters.registry import get_registry

    if not adapter_name:
        raise ValueError("adapter is required for device commands")
    registry = get_registry()
    adapter = registry.get_adapter(adapter_name)
    result = adapter.send_command(device_ref, command, params)
    return {
        "ok": result.ok,
        "result": {
            "ok": result.ok,
            "device_ref": device_ref,
            "device_id": result.device_id,
            "command": result.command,
            "state_before": result.state_before,
            "state_after": result.state_after,
            "error": result.error,
            "error_type": result.error_type,
            "raw_response": result.raw_response,
        },
    }


def runtime_kasa_command(
    *,
    adapter_name: str,
    device_ref: str,
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    return runtime_device_command(
        adapter_name=adapter_name,
        device_ref=device_ref,
        command=command,
        params=params,
    )


def _load_devices(raw: Any) -> list[ConnectedDevice]:
    if not isinstance(raw, list):
        raise ValueError("devices must be a list")
    return [
        deserialize_device(item)
        for item in raw
        if isinstance(item, dict)
    ]


def _require_timeout(value: Any) -> float:
    if not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError("timeout_seconds must be > 0")
    return float(value)


def _device_snapshot(device: ConnectedDevice) -> dict[str, Any]:
    status = "online" if _device_is_online(device) else "offline"
    actions = ["check"]
    if "camera" in device.device_type.lower():
        actions.append("view")
    if _looks_like_light(device):
        actions.extend(["turn_on", "turn_off"])
    return {
        "id": device.id,
        "ip": device.ip,
        "name": _device_name(device),
        "device_type": device.device_type,
        "brand": device.brand,
        "status": status,
        "actions": actions,
    }


def _device_is_online(device: ConnectedDevice) -> bool:
    ports = device.metadata_json.get("open_ports")
    if not isinstance(ports, list) or not ports:
        if "camera" in device.device_type.lower():
            ports = [554, 8554, 80, 443]
        elif _looks_like_light(device):
            ports = [6668, 80, 443]
        else:
            ports = [80, 443, 1883]
    for port in ports[:4]:
        try:
            value = int(port)
        except (TypeError, ValueError):
            continue
        if _scan_port(device.ip, value):
            return True
    return False


def _scan_port(ip: str, port: int, timeout: float = 0.25) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _looks_like_light(device: ConnectedDevice) -> bool:
    text = _device_search_text(device)
    return any(token in text for token in ("light", "lamp", "bulb", "tuya", "feit"))


def _set_light_power(device: ConnectedDevice, *, turn_on: bool) -> dict[str, Any]:
    return _run_light_command(
        device,
        command="turn_on" if turn_on else "turn_off",
        params={},
    )


def _run_light_command(
    device: ConnectedDevice,
    *,
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    adapter_name = _resolve_adapter_name(device)
    if not adapter_name:
        return {
            "status": "needs_setup",
            "message": f"{_device_name(device)} is missing local control details.",
        }

    setup_error = f"{_device_name(device)} is missing local control details."
    for device_ref in _device_ref_candidates(device):
        try:
            result = runtime_device_command(
                adapter_name=adapter_name,
                device_ref=device_ref,
                command=command,
                params=params,
            ).get("result") or {}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "message": f"{_device_name(device)}: {exc}",
            }
        if bool(result.get("ok")):
            verified_state = _verified_light_state(
                command=command,
                params=params,
                state_after=result.get("state_after"),
            )
            return {
                "status": "changed",
                "message": _device_name(device),
                "confirmed": verified_state is not None,
                "verified_state": verified_state,
            }

        error = str(result.get("error") or "").strip()
        if _is_setup_error(result):
            if error:
                setup_error = error
            continue

        return {
            "status": "failed",
            "message": f"{_device_name(device)}: {error or 'unknown error'}",
        }

    return {
        "status": "needs_setup",
        "message": setup_error,
    }


def _verified_light_state(
    *,
    command: str,
    params: dict[str, Any],
    state_after: Any,
) -> dict[str, Any] | None:
    if not isinstance(state_after, dict) or not state_after:
        return None
    if command == "turn_on" and state_after.get("is_on") is True:
        return dict(state_after)
    if command == "turn_off" and state_after.get("is_on") is False:
        return dict(state_after)
    if command == "set_brightness":
        try:
            if int(state_after.get("brightness")) == int(params.get("brightness")):
                return dict(state_after)
        except (TypeError, ValueError):
            return None
    if command == "set_color_temp":
        expected = str(params.get("preset") or "").strip().lower()
        actual = str(state_after.get("color_temp_preset") or "").strip().lower()
        if expected and actual == expected:
            return dict(state_after)
    if command == "set_color_rgb":
        actual = state_after.get("color_rgb")
        if isinstance(actual, dict):
            try:
                expected_rgb = tuple(int(params.get(channel)) for channel in ("r", "g", "b"))
                actual_rgb = tuple(int(actual.get(channel)) for channel in ("r", "g", "b"))
            except (TypeError, ValueError):
                return None
            if actual_rgb == expected_rgb:
                return dict(state_after)
    return None


def _device_name(device: ConnectedDevice) -> str:
    label = device.metadata_json.get("label") if isinstance(device.metadata_json, dict) else None
    if isinstance(label, str) and label.strip():
        return label.strip()
    if device.hostname and device.hostname != device.ip:
        return device.hostname
    return f"{device.brand} {device.device_type}".strip() or device.ip


def _device_search_text(device: ConnectedDevice) -> str:
    return " ".join(
        _normalize_text(value)
        for value in (
            _device_name(device),
            device.hostname or "",
            device.ip,
            device.brand,
            device.device_type,
        )
        if value
    )


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _device_ref_candidates(device: ConnectedDevice) -> list[str]:
    metadata = device.metadata_json if isinstance(device.metadata_json, dict) else {}
    return ordered_identifiers(
        metadata.get("device_id"),
        metadata.get("gismo_device_id"),
        device.id,
        device.ip,
    )


def _resolve_adapter_name(device: ConnectedDevice) -> str | None:
    metadata = device.metadata_json if isinstance(device.metadata_json, dict) else {}
    configured_device = _resolve_configured_device(device)
    tuya_ready = False
    if configured_device is not None:
        platform = normalize_platform_name(configured_device)
        tuya_ready = bool(
            str(configured_device.get("device_id") or "").strip()
            and str(configured_device.get("local_key") or "").strip()
        )
        if platform in {"tuya", "feit", "feit electric"} and tuya_ready:
            return "tuya"
        if platform == "kasa":
            return "kasa"
        if tuya_ready:
            return "tuya"

    adapter_name = _normalize_text(metadata.get("adapter"))
    if adapter_name:
        if adapter_name in {"tuya", "feit", "feit electric"} and not tuya_ready:
            return None
        return adapter_name

    controller = _normalize_text(metadata.get("controller"))
    if controller in {"tuya", "kasa"}:
        if controller == "tuya" and not tuya_ready:
            return None
        return controller

    platform_text = " ".join(
        _normalize_text(value)
        for value in (
            metadata.get("platform"),
            device.brand,
            device.device_type,
            metadata.get("label"),
        )
        if value
    )
    if "kasa" in platform_text:
        return "kasa"
    return None


def _resolve_configured_device(device: ConnectedDevice) -> dict[str, Any] | None:
    metadata = device.metadata_json if isinstance(device.metadata_json, dict) else {}
    explicit_config = str(os.environ.get("GISMO_DEVICES_CONFIG") or "").strip()
    try:
        _, configured = load_configured_devices(
            config_path=explicit_config or None,
        )
    except RuntimeError:
        return None
    return find_configured_device(
        configured,
        identifiers=(
            device.ip,
            device.hostname or "",
            str(metadata.get("label") or ""),
            str(metadata.get("gismo_device_id") or ""),
            str(metadata.get("device_id") or metadata.get("dev_id") or ""),
            device.id,
        ),
    )


def _is_setup_error(result: dict[str, Any]) -> bool:
    error_type = str(result.get("error_type") or "").strip()
    error = _normalize_text(result.get("error") or "")
    if error_type.endswith("ConfigurationError"):
        return True
    return "configured tuya" in error or "local control details" in error or "missing local control" in error


def _local_ipv4_addresses() -> list[str]:
    found: set[str] = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            found.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if "." in addr and not addr.startswith("127."):
                found.add(addr)
    except OSError:
        pass
    return sorted(found)


def _local_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    for local_ip in _local_ipv4_addresses():
        try:
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
        except ValueError:
            continue
        if network not in networks:
            networks.append(network)
    return networks


def _is_local_ip(ip: str, networks: list[ipaddress.IPv4Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def _safe_hostname(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return None


def _infer_device_identity(ip: str, hostname: str | None) -> dict[str, Any]:
    host = (hostname or "").lower()
    device_type = "smart device"
    brand = "Network"
    rtsp_url = None
    snapshot_url = None
    if any(token in host for token in ("tapo", "camera", "cam", "rtsp")):
        device_type = "camera"
        brand = "Tapo" if "tapo" in host else "Camera"
        rtsp_url = f"rtsp://{ip}:554/stream1"
        snapshot_url = f"http://{ip}/snapshot.jpg"
    elif any(token in host for token in ("tuya", "feit", "light", "lamp", "bulb")):
        device_type = "light"
        brand = "FEIT" if "feit" in host else "Tuya"
    elif any(token in host for token in ("mqtt", "hub", "bridge")):
        device_type = "hub"
        brand = "MQTT"
    return {
        "ip": ip,
        "hostname": hostname or ip,
        "device_type": device_type,
        "brand": brand,
        "open_ports": [],
        "rtsp_url": rtsp_url,
        "snapshot_url": snapshot_url,
    }


def _read_arp_table(timeout_seconds: float = 2.0) -> list[str]:
    commands = []
    if sys.platform.startswith("win"):
        commands.append(["arp", "-a"])
    else:
        commands.extend((["ip", "neigh"], ["arp", "-an"]))
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = f"{result.stdout}\n{result.stderr}"
        ips: list[str] = []
        for match in _extract_ipv4s(output):
            try:
                ipaddress.ip_address(match)
            except ValueError:
                continue
            if match not in ips:
                ips.append(match)
        if ips:
            return ips
    return []


def _extract_ipv4s(text: str) -> list[str]:
    found: list[str] = []
    current = ""
    for char in text:
        if char.isdigit() or char == ".":
            current += char
            continue
        if current.count(".") == 3:
            found.append(current)
        current = ""
    if current.count(".") == 3:
        found.append(current)
    return found


def _ping_host(ip: str, timeout_ms: int = 700) -> bool:
    if sys.platform.startswith("win"):
        command = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        wait_seconds = max(1, int(timeout_ms / 1000))
        command = ["ping", "-c", "1", "-W", str(wait_seconds), ip]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=max(1.0, timeout_ms / 1000 + 0.5),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ping_sweep(networks: list[ipaddress.IPv4Network], deadline: float) -> list[str]:
    local_ips = set(_local_ipv4_addresses())
    targets: list[str] = []
    for network in networks:
        for host in network.hosts():
            ip = str(host)
            if ip not in local_ips:
                targets.append(ip)
    if not targets:
        return []

    discovered: list[str] = []
    max_workers = min(64, max(8, len(targets)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {executor.submit(_ping_host, ip): ip for ip in targets}
        while futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                future = next(as_completed(futures, timeout=min(remaining, 0.5)))
            except FuturesTimeoutError:
                continue
            ip = futures.pop(future)
            try:
                if future.result():
                    discovered.append(ip)
            except Exception:
                continue
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return discovered
