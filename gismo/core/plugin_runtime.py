"""Verified plugin runtime execution through the shared execution boundary."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from gismo.core.execution import (
    build_execution_request,
    build_sandbox_profile,
    build_worker_command,
    run_sandboxed_process,
)
from gismo.core.plugin_signing import (
    PluginManifest,
    PluginTrustStore,
    PluginVerificationError,
    load_manifest,
    load_trust_store,
    manifest_from_dict,
    verify_manifest,
)

NETWORK_CAPABILITY_TOKENS = {"network", "http", "https", "socket", "outbound"}


class PluginRuntimeError(RuntimeError):
    """Raised when a verified plugin cannot be executed."""


def execute_verified_plugin(
    manifest_path: str | Path,
    *,
    payload: dict[str, Any] | None = None,
    trust_store_path: str | Path | None = None,
    trust_store: PluginTrustStore | None = None,
    db_path: str | None = None,
    actor: str = "plugin_loader",
    related_run_id: str | None = None,
    related_task_id: str | None = None,
    related_plan_id: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    effective_trust_store = trust_store
    if effective_trust_store is None and trust_store_path is not None:
        effective_trust_store = load_trust_store(trust_store_path)
    verified = verify_manifest(
        manifest,
        trust_store=effective_trust_store,
        db_path=db_path,
        actor=actor,
    )
    _ensure_runtime_constraints(manifest)
    selected_timeout = _runtime_timeout(manifest, override=timeout_s)
    request = build_execution_request(
        component="plugin_runtime",
        action="execute",
        actor=actor,
        resource=f"plugin:{manifest.plugin_id}@{manifest.version}",
        db_path=db_path,
        related_run_id=related_run_id,
        related_task_id=related_task_id,
        related_plan_id=related_plan_id,
    )
    profile = build_sandbox_profile(
        component="plugin_runtime",
        db_path=db_path,
        working_directory=path.parent,
        extra_python_paths=[str(path.parent)],
        extra_env={"GISMO_PLUGIN_ID": manifest.plugin_id},
    )
    result = run_sandboxed_process(
        request,
        worker_command=build_worker_command("plugin"),
        worker_input={
            "manifest": manifest.to_dict(),
            "payload": dict(payload or {}),
        },
        timeout_s=selected_timeout,
        sandbox_profile=profile,
        event_details={
            "plugin_id": manifest.plugin_id,
            "entrypoint": manifest.entrypoint,
            "signer_id": manifest.signer_id,
        },
    )
    return {
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "entrypoint": manifest.entrypoint,
        "manifest_sha256": verified.get("manifest_sha256"),
        "trusted_signer": verified.get("trusted_signer"),
        "output": result.get("output"),
        "execution": dict(result.get("execution") or {}),
    }


def run_plugin_worker(payload: dict[str, Any]) -> dict[str, Any]:
    manifest_payload = payload.get("manifest")
    if not isinstance(manifest_payload, dict):
        raise PluginRuntimeError("plugin manifest payload must be an object")
    manifest = manifest_from_dict(manifest_payload)
    _ensure_runtime_constraints(manifest)
    entrypoint = _resolve_entrypoint(manifest.entrypoint)
    raw_input = payload.get("payload")
    if raw_input is None:
        plugin_input: dict[str, Any] = {}
    elif isinstance(raw_input, dict):
        plugin_input = raw_input
    else:
        raise PluginRuntimeError("plugin payload must be an object")
    try:
        result = entrypoint(plugin_input)
    except Exception as exc:  # noqa: BLE001
        raise PluginRuntimeError(str(exc) or exc.__class__.__name__) from exc
    _ensure_json_safe(result)
    return {
        "ok": True,
        "output": result,
    }


def _ensure_runtime_constraints(manifest: PluginManifest) -> None:
    lowered_caps = {capability.strip().lower() for capability in manifest.capabilities}
    if lowered_caps & NETWORK_CAPABILITY_TOKENS:
        raise PluginVerificationError(
            "Plugin runtime does not grant outbound network capability in this slice."
        )
    network_constraint = manifest.constraints.get("network") if isinstance(manifest.constraints, dict) else None
    if network_constraint not in (None, False, "", "none"):
        raise PluginVerificationError(
            "Plugin runtime does not grant outbound network capability in this slice."
        )


def _runtime_timeout(manifest: PluginManifest, *, override: float | None) -> float:
    if override is not None and override > 0:
        return float(override)
    raw_timeout = manifest.constraints.get("timeout_s") if isinstance(manifest.constraints, dict) else None
    if isinstance(raw_timeout, (int, float)) and float(raw_timeout) > 0:
        return float(raw_timeout)
    return 10.0


def _resolve_entrypoint(entrypoint: str):
    module_name, separator, function_name = entrypoint.partition(":")
    if not separator or not module_name.strip() or not function_name.strip():
        raise PluginRuntimeError("Plugin entrypoint must use module:function format.")
    module = importlib.import_module(module_name.strip())
    target = getattr(module, function_name.strip(), None)
    if not callable(target):
        raise PluginRuntimeError(f"Plugin entrypoint is not callable: {entrypoint}")
    return target


def _ensure_json_safe(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PluginRuntimeError("Plugin output must be JSON-serializable.") from exc
