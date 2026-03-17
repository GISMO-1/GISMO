"""GISMO web API — pure data layer (no HTTP).

All functions return JSON-serialisable dicts/lists and raise
``ValueError`` for bad input (404-class) or ``RuntimeError`` for
state errors.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gismo.core.models import ConnectedDevice, QueueStatus
from gismo.core.state import StateStore
from gismo.memory.store import list_namespaces, list_items_for_snapshot


# ── helpers ────────────────────────────────────────────────────────────────


def _dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _status_val(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


# ── status / worker ────────────────────────────────────────────────────────


def get_status(db_path: str) -> dict[str, Any]:
    """Return background worker status + queue stats."""
    with StateStore(db_path) as store:
        hb = store.get_daemon_heartbeat()
        paused = store.get_daemon_paused()
        stats = store.queue_stats()

    now = datetime.now(timezone.utc)
    daemon: dict[str, Any]
    if hb is None:
        daemon = {"running": False, "paused": paused, "stale": False, "state": "ready"}
    else:
        last_seen = hb.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_secs = max(0, int((now - last_seen).total_seconds()))
        stale = age_secs > 30
        daemon = {
            "running": not stale,
            "paused": paused,
            "stale": stale,
            "state": "online" if not stale else "ready",
            "pid": hb.pid,
            "started_at": _dt(hb.started_at),
            "last_seen": _dt(hb.last_seen),
            "age_secs": age_secs,
        }
    return {"daemon": daemon, "queue": stats}


def get_queue_stats(db_path: str) -> dict[str, Any]:
    """Return queue summary statistics only."""
    with StateStore(db_path) as store:
        return store.queue_stats()


def set_daemon_paused(db_path: str, paused: bool) -> dict[str, Any]:
    with StateStore(db_path) as store:
        store.set_daemon_paused(paused)
    return {"paused": paused}


# ── queue ──────────────────────────────────────────────────────────────────


def get_queue(db_path: str, limit: int = 100) -> list[dict[str, Any]]:
    with StateStore(db_path) as store:
        items = store.list_queue_items(limit=limit, newest_first=True)
    result = []
    for item in items:
        result.append({
            "id": item.id,
            "status": _status_val(item.status),
            "command_text": item.command_text,
            "attempt_count": item.attempt_count,
            "max_retries": item.max_retries,
            "created_at": _dt(item.created_at),
            "updated_at": _dt(item.updated_at),
            "started_at": _dt(item.started_at),
            "finished_at": _dt(item.finished_at),
            "last_error": item.last_error,
            "cancel_requested": item.cancel_requested,
            "run_id": item.run_id,
        })
    return result


def cancel_queue_item(db_path: str, item_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        item = store.request_queue_item_cancel(item_id)
    if item is None:
        raise ValueError(f"Queue item not found: {item_id}")
    return {"id": item.id, "status": _status_val(item.status)}


def purge_failed(db_path: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        count = store.delete_queue_items_by_status(QueueStatus.FAILED)
    return {"deleted": count}


# ── runs ───────────────────────────────────────────────────────────────────


def get_runs(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    with StateStore(db_path) as store:
        runs = list(store.list_runs(limit=limit, newest_first=True))
        task_map = {run.id: list(store.list_tasks(run.id)) for run in runs}

    result = []
    for run in runs:
        tasks = task_map.get(run.id, [])
        statuses = [_status_val(t.status) for t in tasks]
        total = len(tasks)
        succ = statuses.count("SUCCEEDED")
        fail = statuses.count("FAILED")
        running = statuses.count("RUNNING")

        if fail:
            run_status = "failed"
        elif running:
            run_status = "running"
        elif total and succ == total:
            run_status = "succeeded"
        else:
            run_status = "pending"

        result.append({
            "id": run.id,
            "label": run.label or "",
            "status": run_status,
            "created_at": _dt(run.created_at),
            "task_total": total,
            "task_succeeded": succ,
            "task_failed": fail,
            "task_running": running,
        })
    return result


def get_run_detail(db_path: str, run_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        runs = [r for r in store.list_runs(limit=10000) if r.id == run_id]
        if not runs:
            raise ValueError(f"Run not found: {run_id}")
        run = runs[0]
        tasks = list(store.list_tasks(run_id))
        tool_calls = list(store.list_tool_calls(run_id))

    return {
        "id": run.id,
        "label": run.label or "",
        "created_at": _dt(run.created_at),
        "metadata": run.metadata_json,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": _status_val(t.status),
                "created_at": _dt(t.created_at),
                "updated_at": _dt(t.updated_at),
                "error": t.error,
                "failure_type": _status_val(t.failure_type),
            }
            for t in tasks
        ],
        "tool_calls": [
            {
                "id": tc.id,
                "tool_name": tc.tool_name,
                "status": _status_val(tc.status),
                "started_at": _dt(tc.started_at),
                "finished_at": _dt(tc.finished_at),
                "error": tc.error,
            }
            for tc in tool_calls
        ],
    }


# ── memory ─────────────────────────────────────────────────────────────────


def get_memory(db_path: str) -> dict[str, Any]:
    namespaces = list_namespaces(db_path)
    items = list_items_for_snapshot(db_path, namespace=None, namespace_prefix=None)

    items_by_ns: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.is_tombstoned:
            continue
        entry = {
            "id": item.id,
            "key": item.key,
            "kind": item.kind,
            "value": item.value,
            "confidence": item.confidence,
            "source": item.source,
            "tags": item.tags,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        items_by_ns.setdefault(item.namespace, []).append(entry)

    ns_list = [
        {
            "namespace": ns.namespace,
            "item_count": ns.item_count,
            "tombstone_count": ns.tombstone_count,
            "last_write_at": ns.last_write_at,
            "retired": ns.retired,
        }
        for ns in namespaces
    ]
    return {"namespaces": ns_list, "items": items_by_ns}


# ── Plan approval ─────────────────────────────────────────────────────────


def get_plans(
    db_path: str,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from gismo.core.models import PlanStatus

    status_filter = None
    if status:
        try:
            status_filter = PlanStatus(status.upper())
        except ValueError:
            pass

    with StateStore(db_path) as store:
        plans = store.list_pending_plans(status=status_filter, limit=limit)

    return [
        {
            "id": p.id,
            "status": p.status.value,
            "risk_level": p.risk_level,
            "risk_flags": p.risk_json.get("risk_flags", []),
            "intent": p.intent,
            "user_text": p.user_text,
            "actor": p.actor,
            "created_at": _dt(p.created_at),
            "updated_at": _dt(p.updated_at),
            "action_count": len(p.plan_json.get("actions", [])),
            "rejection_reason": p.rejection_reason,
            "approved_at": _dt(p.approved_at),
            "rejected_at": _dt(p.rejected_at),
        }
        for p in plans
    ]


def get_plan_detail(db_path: str, plan_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
    if plan is None:
        raise ValueError(f"Plan not found: {plan_id}")
    return {
        "id": plan.id,
        "status": plan.status.value,
        "risk_level": plan.risk_level,
        "risk": plan.risk_json,
        "explain": plan.explain_json,
        "intent": plan.intent,
        "user_text": plan.user_text,
        "actor": plan.actor,
        "created_at": _dt(plan.created_at),
        "updated_at": _dt(plan.updated_at),
        "plan": plan.plan_json,
        "rejection_reason": plan.rejection_reason,
        "approved_at": _dt(plan.approved_at),
        "rejected_at": _dt(plan.rejected_at),
    }


def approve_plan(db_path: str, plan_id: str) -> dict[str, Any]:
    from gismo.core.models import PlanStatus
    from gismo.core.plan_store import enqueue_plan_actions

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is already {plan.status.value.lower()}")
        enqueued_ids, skipped = enqueue_plan_actions(store, plan.plan_json)
        store.approve_pending_plan(plan_id)

    return {"id": plan_id, "status": "APPROVED", "enqueued_ids": enqueued_ids, "skipped": skipped}


def reject_plan(db_path: str, plan_id: str, reason: str | None = None) -> dict[str, Any]:
    from gismo.core.models import PlanStatus

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is already {plan.status.value.lower()}")
        store.reject_pending_plan(plan_id, reason=reason)

    return {"id": plan_id, "status": "REJECTED", "reason": reason}


def patch_plan(
    db_path: str,
    plan_id: str,
    *,
    action_index: int | None = None,
    new_command: str | None = None,
    remove_action: bool = False,
) -> dict[str, Any]:
    from gismo.core.models import PlanStatus

    with StateStore(db_path) as store:
        plan = store.get_pending_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")
        if plan.status != PlanStatus.PENDING:
            raise ValueError(f"Plan is {plan.status.value.lower()} and cannot be edited")

        if action_index is None:
            raise ValueError("action_index is required")

        new_plan = dict(plan.plan_json)
        actions = list(new_plan.get("actions", []))

        if action_index < 0 or action_index >= len(actions):
            raise ValueError(
                f"action_index {action_index} out of range (plan has {len(actions)} actions)"
            )

        if remove_action:
            actions.pop(action_index)
        elif new_command is not None:
            actions[action_index] = dict(actions[action_index])
            actions[action_index]["command"] = new_command
        else:
            raise ValueError("Provide new_command or remove_action=true")

        new_plan["actions"] = actions
        updated = store.update_pending_plan_json(plan_id, new_plan)

    return {
        "id": plan_id,
        "action_count": len(new_plan["actions"]),
        "plan": updated.plan_json if updated else new_plan,
    }


# ── Chat ───────────────────────────────────────────────────────────────────

_CHAT_HISTORY_FILE = Path(".gismo") / "chat_history.jsonl"


def _append_chat_record(message: str, reply: str) -> None:
    """Append a single user/assistant exchange to the JSONL history file."""
    import json

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": message,
        "assistant": reply,
    }
    try:
        _CHAT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _CHAT_HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # never let logging failures break the chat


def _clean_reply(text: str) -> str:
    """Strip model artefacts from a chat reply before display."""
    import re
    # Remove special tokens like <|end|>, <|assistant|>, <|im_end|>, etc.
    text = re.sub(r"<\|[^|>]*\|>", "", text)
    return text.strip()


def _build_chat_system(db_path: str) -> str:
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path) or "Operator"
    return (
        "I am GISMO, a local-first, policy-controlled personal AI assistant built by Mike Burns. "
        "I run entirely on your hardware — no cloud services, no silent actions, and a full audit trail of everything I do. "
        f"The operator's name is {name}. Address them as {name} when appropriate. "
        "My job is to help you manage tasks, queues, plans, runs, and memory on your own machine. "
        "I speak directly and concisely. I do not output JSON unless you ask for it. "
        "I never take actions outside what your operator policy explicitly permits."
    )


def chat_message(
    db_path: str,
    message: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    """Send a message to the local LLM and return the reply."""
    from gismo.llm.ollama import ollama_freeform_chat, OllamaError

    messages = list(history) + [{"role": "user", "content": message}]
    try:
        reply = ollama_freeform_chat(messages, system=_build_chat_system(db_path), model="gismo")
    except OllamaError as exc:
        raise RuntimeError(str(exc)) from exc
    reply = _clean_reply(reply)
    _append_chat_record(message, reply)
    return {"reply": reply}


# ── Onboarding ──────────────────────────────────────────────────────────────


def get_onboarding_status(db_path: str) -> dict[str, Any]:
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path)
    return {"needs_onboarding": name is None, "operator_name": name}


def complete_onboarding(db_path: str, name: str, voice_id: str) -> dict[str, Any]:
    from gismo.onboarding import set_operator_name
    from gismo.tts.prefs import set_voice
    from gismo.tts.voices import validate_voice

    name = name.strip() or "Operator"
    validate_voice(voice_id)
    set_operator_name(db_path, name)
    set_voice(db_path, voice_id)
    return {"ok": True, "name": name, "voice": voice_id}


# ── System health ──────────────────────────────────────────────────────────


def get_system_health() -> dict[str, Any]:
    """Return CPU and RAM usage percentages."""
    import psutil

    internet_connected = False
    internet_latency_ms = None
    for host in ("1.1.1.1", "8.8.8.8"):
        started = time.monotonic()
        try:
            with socket.create_connection((host, 53), timeout=1.5):
                internet_connected = True
                internet_latency_ms = round((time.monotonic() - started) * 1000)
                break
        except OSError:
            continue

    return {
        "cpu_percent": psutil.cpu_percent(),
        "virtual_memory": psutil.virtual_memory().percent,
        "internet_connected": internet_connected,
        "internet_latency_ms": internet_latency_ms,
    }


# ── Devices ────────────────────────────────────────────────────────────────


_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _serialize_device(device: ConnectedDevice, *, status: str | None = None) -> dict[str, Any]:
    label = device.hostname or device.metadata_json.get("label") or f"{device.brand} {device.device_type}"
    stream_url = f"/api/devices/{device.id}/stream" if "camera" in device.device_type else None
    return {
        "id": device.id,
        "ip": device.ip,
        "hostname": device.hostname or device.ip,
        "name": label,
        "device_type": device.device_type,
        "brand": device.brand,
        "status": status or "online",
        "rtsp_url": device.rtsp_url,
        "snapshot_url": device.snapshot_url,
        "thumbnail_url": stream_url,
        "stream_url": stream_url,
        "created_at": _dt(device.created_at),
        "updated_at": _dt(device.updated_at),
        "metadata": device.metadata_json,
    }


def _device_is_online(device: ConnectedDevice) -> bool:
    ports = device.metadata_json.get("open_ports")
    if not isinstance(ports, list) or not ports:
        ports = [554, 8554, 6668, 1883, 80, 443]
    return any(_scan_port(device.ip, int(port), timeout=0.15) for port in ports[:4])


def _local_ipv4_addresses() -> list[str]:
    found: set[str] = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.add(probe.getsockname()[0])
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


def _scan_port(ip: str, port: int, timeout: float = 0.2) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


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
        for match in _IP_RE.findall(output):
            try:
                ipaddress.ip_address(match)
            except ValueError:
                continue
            if match not in ips:
                ips.append(match)
        if ips:
            return ips
    return []


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


def scan_devices(db_path: str, timeout_seconds: float = 10.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    saved = {device.ip: device for device in _list_device_models(db_path)}
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

    results = []
    for ip, item in sorted(merged.items()):
        saved_device = saved.get(ip)
        results.append({
            "ip": ip,
            "hostname": item.get("hostname") or (saved_device.hostname if saved_device else ip) or ip,
            "device_type": item.get("device_type") or (saved_device.device_type if saved_device else "smart device"),
            "brand": item.get("brand") or (saved_device.brand if saved_device else "Unknown"),
            "rtsp_url": item.get("rtsp_url") or (saved_device.rtsp_url if saved_device else None),
            "snapshot_url": item.get("snapshot_url") or (saved_device.snapshot_url if saved_device else None),
            "open_ports": item.get("open_ports") or (saved_device.metadata_json.get("open_ports", []) if saved_device else []),
            "saved": saved_device is not None,
            "saved_id": saved_device.id if saved_device else None,
        })
    return results


def _list_device_models(db_path: str) -> list[ConnectedDevice]:
    with StateStore(db_path) as store:
        return store.list_devices()


def list_devices(db_path: str) -> list[dict[str, Any]]:
    return [
        _serialize_device(device, status="online" if _device_is_online(device) else "offline")
        for device in _list_device_models(db_path)
    ]


def get_devices(db_path: str) -> list[dict[str, Any]]:
    """Backward-compatible alias for saved devices."""
    return list_devices(db_path)


def _device_name(hostname: str | None, brand: str, device_type: str, ip: str) -> str:
    if hostname and hostname != ip:
        return hostname
    return f"{brand} {device_type}".strip()


def add_device(
    db_path: str,
    ip: str,
    hostname: str | None,
    device_type: str,
    brand: str,
    *,
    rtsp_url: str | None = None,
    snapshot_url: str | None = None,
    open_ports: list[int] | None = None,
) -> dict[str, Any]:
    with StateStore(db_path) as store:
        existing = next((device for device in store.list_devices() if device.ip == ip), None)
        device = ConnectedDevice(
            id=existing.id if existing else ConnectedDevice(ip=ip, device_type=device_type, brand=brand).id,
            ip=ip,
            hostname=hostname or ip,
            device_type=device_type,
            brand=brand,
            rtsp_url=rtsp_url,
            snapshot_url=snapshot_url,
            metadata_json={
                "label": _device_name(hostname, brand, device_type, ip),
                "open_ports": open_ports or [],
            },
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
        )
        stored = store.upsert_device(device)
        store.record_event(
            actor="web",
            event_type="device_added",
            message=f"Connected {stored.brand} {stored.device_type} at {stored.ip}",
            json_payload={"device_id": stored.id, "ip": stored.ip},
        )
    return _serialize_device(stored, status="online" if _device_is_online(stored) else "offline")


def remove_device(db_path: str, device_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        device = store.get_device(device_id)
        if device is None:
            raise ValueError(f"Device not found: {device_id}")
        deleted = store.delete_device(device_id)
        if deleted:
            store.record_event(
                actor="web",
                event_type="device_removed",
                message=f"Removed {device.brand} {device.device_type} at {device.ip}",
                json_payload={"device_id": device.id, "ip": device.ip},
            )
    return {"ok": deleted, "id": device_id}


def get_device_stream_payload(db_path: str, device_id: str) -> dict[str, Any]:
    with StateStore(db_path) as store:
        device = store.get_device(device_id)
    if device is None:
        raise ValueError(f"Device not found: {device_id}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and device.rtsp_url:
        return {
            "kind": "mjpeg",
            "content_type": "multipart/x-mixed-replace; boundary=frame",
            "ffmpeg_args": [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                device.rtsp_url,
                "-f",
                "image2pipe",
                "-vf",
                "fps=4,scale=480:-1",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "6",
                "pipe:1",
            ],
        }

    if device.snapshot_url:
        try:
            with urllib.request.urlopen(device.snapshot_url, timeout=4) as response:
                body = response.read()
            return {"kind": "snapshot", "content_type": "image/jpeg", "body": body}
        except Exception:
            pass

    label = _device_name(device.hostname, device.brand, device.device_type, device.ip)
    body = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'>"
        "<rect width='100%' height='100%' fill='#141417'/>"
        "<text x='50%' y='45%' text-anchor='middle' fill='#e2e2e8' font-size='24' "
        "font-family='Arial, sans-serif'>Live preview unavailable</text>"
        f"<text x='50%' y='58%' text-anchor='middle' fill='#64647a' font-size='16' "
        f"font-family='Arial, sans-serif'>{label} · {device.ip}</text>"
        "</svg>"
    ).encode("utf-8")
    return {"kind": "snapshot", "content_type": "image/svg+xml", "body": body}


# ── Settings ───────────────────────────────────────────────────────────────


def get_settings(db_path: str) -> dict[str, Any]:
    from gismo.onboarding import get_operator_name
    from gismo.tts.prefs import get_voice

    voices = [
        voice for voice in get_voices(db_path)["voices"]
        if voice.get("engine") == "kokoro"
    ]
    return {
        "operator_name": get_operator_name(db_path) or "",
        "voice": get_voice(db_path),
        "voices": voices,
        "theme": "Coming soon",
    }


def save_settings(
    db_path: str,
    *,
    operator_name: str | None = None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    from gismo.onboarding import set_operator_name
    from gismo.tts.prefs import set_voice

    name = (operator_name or "").strip()
    if name:
        set_operator_name(db_path, name)
    if voice_id:
        set_voice(db_path, voice_id)
    return get_settings(db_path)


# ── Activity feed ──────────────────────────────────────────────────────────


def get_activity_feed(db_path: str, limit: int = 40) -> list[dict[str, Any]]:
    """Merge recent queue items, runs, and chat history into a unified activity timeline."""
    import json as _json

    _Q_COLORS = {
        "QUEUED": "blue", "IN_PROGRESS": "teal",
        "SUCCEEDED": "green", "FAILED": "red", "CANCELLED": "gray",
    }
    _R_COLORS = {"succeeded": "green", "failed": "red", "running": "teal", "pending": "blue"}

    events: list[dict[str, Any]] = []

    for item in get_queue(db_path, limit=25):
        st = item["status"]
        label = (item.get("command_text") or "")[:60] or f"item/{item['id'][:8]}"
        events.append({
            "type": "queue",
            "label": label,
            "status": st,
            "color": _Q_COLORS.get(st, "gray"),
            "timestamp": item.get("updated_at") or item.get("created_at"),
        })

    for run in get_runs(db_path, limit=20):
        st = run["status"]
        events.append({
            "type": "run",
            "label": run.get("label") or f"run/{run['id'][:8]}",
            "status": st.upper(),
            "color": _R_COLORS.get(st, "gray"),
            "timestamp": run.get("created_at"),
        })

    # Pull recent chat exchanges from history file
    try:
        hist_path = _CHAT_HISTORY_FILE
        if hist_path.exists():
            lines = hist_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines[-20:]):
                rec = _json.loads(line)
                snippet = (rec.get("user") or "")[:50]
                events.append({
                    "type": "chat",
                    "label": snippet or "(empty)",
                    "status": "CHAT",
                    "color": "teal",
                    "timestamp": rec.get("timestamp"),
                })
    except Exception:
        pass

    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return events[:limit]


# ── Morning briefing ───────────────────────────────────────────────────────


def get_briefing(db_path: str) -> dict[str, Any]:
    """Generate a text briefing from current system state."""
    from gismo.onboarding import get_operator_name

    name = get_operator_name(db_path) or "Operator"
    data = get_status(db_path)
    daemon = data.get("daemon", {})
    by_status = (data.get("queue") or {}).get("by_status", {})

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

    parts = [f"{greeting}, {name}."]
    queued  = by_status.get("QUEUED", 0)
    running = by_status.get("IN_PROGRESS", 0)
    failed  = by_status.get("FAILED", 0)
    done    = by_status.get("SUCCEEDED", 0)

    if running:
        parts.append(f"{running} task{'s' if running != 1 else ''} currently running.")
    if queued:
        parts.append(f"{queued} item{'s' if queued != 1 else ''} queued.")
    if failed:
        parts.append(f"\u26a0 {failed} failed item{'s' if failed != 1 else ''} need attention.")
    if not running and not queued and done:
        parts.append(f"All clear \u2014 {done} completed.")

    if daemon.get("paused"):
        parts.append("GISMO is paused.")
    elif running:
        parts.append("GISMO is handling tasks.")
    else:
        parts.append("GISMO is ready.")

    try:
        pending = get_plans(db_path, status="PENDING")
        if pending:
            parts.append(f"{len(pending)} plan{'s' if len(pending) != 1 else ''} awaiting approval.")
    except Exception:
        pass

    return {"name": name, "briefing": " ".join(parts)}


# ── TTS ────────────────────────────────────────────────────────────────────


def get_voices(db_path: str) -> dict[str, Any]:
    """Return available voices with download status and current preference."""
    from gismo.tts.voices import VOICES, DEFAULT_VOICE, is_downloaded
    from gismo.tts.prefs import get_voice

    current = get_voice(db_path)
    voice_list = [
        {
            "id": vid,
            "name": info["name"],
            "lang": info["lang"],
            "quality": info["quality"],
            "description": info["description"],
            "engine": info.get("engine", "piper"),
            "downloaded": is_downloaded(vid),
            "is_default": vid == DEFAULT_VOICE,
            "is_selected": vid == current,
        }
        for vid, info in VOICES.items()
    ]
    return {"voices": voice_list, "current": current}


def set_voice_preference(db_path: str, voice_id: str) -> dict[str, Any]:
    from gismo.tts.prefs import set_voice

    set_voice(db_path, voice_id)
    return {"voice": voice_id}


def tts_synthesize(db_path: str, text: str, voice_id: str | None = None) -> bytes:
    """Synthesize text and return WAV bytes. Downloads model if needed."""
    from gismo.tts.prefs import get_voice
    from gismo.tts.engine import synthesize

    if not voice_id:
        voice_id = get_voice(db_path)
    return synthesize(text, voice_id)


def tts_preview(db_path: str, voice_id: str | None = None) -> bytes:
    """Return a short voice preview clip."""
    return tts_synthesize(
        db_path,
        "Hello. This is how I sound.",
        voice_id=voice_id,
    )
