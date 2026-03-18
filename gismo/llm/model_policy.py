"""Model policy, discovery, and routing for local GISMO requests."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_PRIMARY_ASSISTANT_MODEL = "gismo:latest"
DEFAULT_PLANNER_MODEL = "gismo:latest"
DEFAULT_HELPER_MODEL = ""
DEFAULT_ALLOW_IDENTITY_FALLBACK = False
DEFAULT_PERFORMANCE_MODE = "auto"
PERFORMANCE_MODES = {
    "auto",
    "prefer_quality",
    "balanced",
    "prefer_responsiveness",
}
MODEL_SETTINGS_NAMESPACE = "gismo:settings"
PRIMARY_MODEL_KEY = "llm.primary_assistant_model"
PLANNER_MODEL_KEY = "llm.planner_model"
HELPER_MODEL_KEY = "llm.helper_model"
ALLOW_IDENTITY_FALLBACK_KEY = "llm.allow_identity_fallback"
PERFORMANCE_MODE_KEY = "llm.performance_mode"
LEGACY_MODEL_KEY = "llm.model"
_DISCOVERY_TTL_SECONDS = 20.0
_FAILURE_WINDOW_SECONDS = 300.0
_CACHE_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[str, Any] = {"timestamp": 0.0, "payload": None}
_RUNTIME_FAILURES: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ModelPolicy:
    primary_assistant_model: str
    planner_model: str
    helper_model: str
    allow_identity_fallback: bool
    performance_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_assistant_model": self.primary_assistant_model,
            "planner_model": self.planner_model,
            "helper_model": self.helper_model,
            "allow_identity_fallback": self.allow_identity_fallback,
            "performance_mode": self.performance_mode,
        }


@dataclass(frozen=True)
class CapabilityPolicy:
    tier: str
    history_messages: int
    assistant_timeout_s: int
    planner_timeout_s: int
    allow_helper_model: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "history_messages": self.history_messages,
            "assistant_timeout_s": self.assistant_timeout_s,
            "planner_timeout_s": self.planner_timeout_s,
            "allow_helper_model": self.allow_helper_model,
        }


@dataclass(frozen=True)
class ModelRouteDecision:
    purpose: str
    selected_model: str | None
    candidate_models: list[str]
    degraded: bool
    degraded_reason: str | None
    capability: CapabilityPolicy
    policy: ModelPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "selected_model": self.selected_model,
            "candidate_models": list(self.candidate_models),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "capability": self.capability.to_dict(),
            "policy": self.policy.to_dict(),
        }


def _memory_get(db_path: str, key: str) -> Any:
    from gismo.memory.store import get_item as memory_get_item

    item = memory_get_item(
        db_path,
        namespace=MODEL_SETTINGS_NAMESPACE,
        key=key,
        include_tombstoned=False,
        actor="model-policy",
        policy_hash="model-policy",
    )
    return None if item is None else item.value


def _memory_put(db_path: str, key: str, value: Any, *, tags: list[str]) -> None:
    from gismo.memory.store import put_item as memory_put_item

    memory_put_item(
        db_path,
        namespace=MODEL_SETTINGS_NAMESPACE,
        key=key,
        kind="preference",
        value=value,
        tags=tags,
        confidence="high",
        source="model-policy",
        ttl_seconds=None,
        actor="model-policy",
        policy_hash="model-policy",
    )


def load_model_policy(db_path: str) -> ModelPolicy:
    primary = str(_memory_get(db_path, PRIMARY_MODEL_KEY) or "").strip()
    planner = str(_memory_get(db_path, PLANNER_MODEL_KEY) or "").strip()
    helper = str(_memory_get(db_path, HELPER_MODEL_KEY) or "").strip()
    allow_identity_fallback = bool(_memory_get(db_path, ALLOW_IDENTITY_FALLBACK_KEY) is True)
    performance_mode = str(_memory_get(db_path, PERFORMANCE_MODE_KEY) or "").strip().lower()
    legacy_model = str(_memory_get(db_path, LEGACY_MODEL_KEY) or "").strip()

    if not primary:
        primary = legacy_model or DEFAULT_PRIMARY_ASSISTANT_MODEL
    if not planner:
        planner = legacy_model or DEFAULT_PLANNER_MODEL
    if performance_mode not in PERFORMANCE_MODES:
        performance_mode = DEFAULT_PERFORMANCE_MODE

    return ModelPolicy(
        primary_assistant_model=primary,
        planner_model=planner,
        helper_model=helper,
        allow_identity_fallback=allow_identity_fallback,
        performance_mode=performance_mode,
    )


def save_model_policy(
    db_path: str,
    *,
    primary_assistant_model: str | None = None,
    planner_model: str | None = None,
    helper_model: str | None = None,
    allow_identity_fallback: bool | None = None,
    performance_mode: str | None = None,
) -> ModelPolicy:
    discovery = discover_models(force_refresh=True)
    installed = set(discovery["installed_models"])

    def _validate(model_name: str | None, *, optional: bool) -> str | None:
        if model_name is None:
            return None
        normalized = model_name.strip()
        if not normalized:
            return "" if optional else None
        if normalized not in installed:
            raise ValueError(f"Model is not installed: {normalized}")
        return normalized

    validated_primary = _validate(primary_assistant_model, optional=False)
    validated_planner = _validate(planner_model, optional=False)
    validated_helper = _validate(helper_model, optional=True)
    if validated_primary:
        _memory_put(db_path, PRIMARY_MODEL_KEY, validated_primary, tags=["llm", "model", "primary"])
        _memory_put(db_path, LEGACY_MODEL_KEY, validated_primary, tags=["llm", "model", "legacy"])
    if validated_planner:
        _memory_put(db_path, PLANNER_MODEL_KEY, validated_planner, tags=["llm", "model", "planner"])
    if helper_model is not None:
        _memory_put(db_path, HELPER_MODEL_KEY, validated_helper or "", tags=["llm", "model", "helper"])
    if allow_identity_fallback is not None:
        _memory_put(
            db_path,
            ALLOW_IDENTITY_FALLBACK_KEY,
            bool(allow_identity_fallback),
            tags=["llm", "routing", "identity"],
        )
    if performance_mode is not None:
        normalized_mode = performance_mode.strip().lower()
        if normalized_mode not in PERFORMANCE_MODES:
            raise ValueError("Unsupported performance mode")
        _memory_put(
            db_path,
            PERFORMANCE_MODE_KEY,
            normalized_mode,
            tags=["llm", "routing", "performance"],
        )
    invalidate_model_discovery()
    return load_model_policy(db_path)


def invalidate_model_discovery() -> None:
    with _CACHE_LOCK:
        _DISCOVERY_CACHE["timestamp"] = 0.0
        _DISCOVERY_CACHE["payload"] = None


def _run_ollama_command(args: list[str], *, timeout: int = 4) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if completed.returncode != 0:
        return False, completed.stdout or ""
    return True, completed.stdout or ""


def _parse_ollama_table(raw: str) -> list[str]:
    models: list[str] = []
    for index, line in enumerate((raw or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if index == 0 and stripped.lower().startswith("name"):
            continue
        model_name = stripped.split()[0].strip()
        if model_name and model_name not in models:
            models.append(model_name)
    return models


def discover_models(*, force_refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get("payload")
        if not force_refresh and cached and (now - float(_DISCOVERY_CACHE.get("timestamp") or 0.0)) < _DISCOVERY_TTL_SECONDS:
            return dict(cached)

    list_ok, list_stdout = _run_ollama_command(["ollama", "list"])
    ps_ok, ps_stdout = _run_ollama_command(["ollama", "ps"])
    payload = {
        "ollama_available": list_ok,
        "installed_models": _parse_ollama_table(list_stdout) if list_ok else [],
        "loaded_models": _parse_ollama_table(ps_stdout) if ps_ok else [],
        "refreshed_at": time.time(),
    }
    with _CACHE_LOCK:
        _DISCOVERY_CACHE["timestamp"] = now
        _DISCOVERY_CACHE["payload"] = dict(payload)
    return payload


def _recent_failure(purpose: str) -> dict[str, Any] | None:
    record = _RUNTIME_FAILURES.get(purpose)
    if not record:
        return None
    if (time.time() - float(record.get("timestamp") or 0.0)) > _FAILURE_WINDOW_SECONDS:
        return None
    return record


def record_model_result(
    *,
    purpose: str,
    model: str | None,
    success: bool,
    error: BaseException | None = None,
) -> None:
    if success:
        _RUNTIME_FAILURES.pop(purpose, None)
        return
    _RUNTIME_FAILURES[purpose] = {
        "timestamp": time.time(),
        "model": model,
        "error_type": type(error).__name__ if error else None,
        "error": str(error) if error else None,
    }


def determine_capability_policy(
    *,
    policy: ModelPolicy,
    discovery: dict[str, Any] | None = None,
) -> CapabilityPolicy:
    import psutil

    discovery = discovery or discover_models()
    loaded = set(discovery.get("loaded_models") or [])
    vm = psutil.virtual_memory()
    total_gb = float(vm.total) / (1024 ** 3)
    available_gb = float(vm.available) / (1024 ** 3)

    if policy.performance_mode == "prefer_quality":
        tier = "high"
    elif policy.performance_mode == "balanced":
        tier = "balanced"
    elif policy.performance_mode == "prefer_responsiveness":
        tier = "constrained"
    else:
        if total_gb >= 24 and available_gb >= 8:
            tier = "high"
        elif total_gb < 12 or available_gb < 3:
            tier = "constrained"
        else:
            tier = "balanced"
        assistant_failure = _recent_failure("assistant_reply")
        if assistant_failure and assistant_failure.get("error_type") in {"MemoryError", "OllamaError"}:
            tier = "constrained"
        elif policy.primary_assistant_model in loaded and tier == "balanced" and available_gb >= 6:
            tier = "high"

    if tier == "high":
        return CapabilityPolicy(
            tier="high",
            history_messages=12,
            assistant_timeout_s=120,
            planner_timeout_s=120,
            allow_helper_model=False,
        )
    if tier == "balanced":
        return CapabilityPolicy(
            tier="balanced",
            history_messages=8,
            assistant_timeout_s=90,
            planner_timeout_s=75,
            allow_helper_model=True,
        )
    return CapabilityPolicy(
        tier="constrained",
        history_messages=4,
        assistant_timeout_s=45,
        planner_timeout_s=45,
        allow_helper_model=True,
    )


def resolve_model_route(db_path: str, *, purpose: str) -> ModelRouteDecision:
    policy = load_model_policy(db_path)
    discovery = discover_models()
    installed = set(discovery.get("installed_models") or [])
    capability = determine_capability_policy(policy=policy, discovery=discovery)

    candidates: list[str] = []
    degraded_reason: str | None = None

    def _append(model_name: str) -> None:
        normalized = model_name.strip()
        if normalized and normalized in installed and normalized not in candidates:
            candidates.append(normalized)

    if purpose == "assistant_reply":
        _append(policy.primary_assistant_model)
        if policy.allow_identity_fallback:
            _append(policy.planner_model)
            _append(policy.helper_model)
        if not candidates:
            degraded_reason = "GISMO's main voice is not available right now."
    elif purpose == "planner":
        _append(policy.planner_model)
        if capability.allow_helper_model:
            _append(policy.helper_model)
        _append(policy.primary_assistant_model)
        if not candidates:
            degraded_reason = "Planning is not available because no configured local model is ready."
    elif purpose == "helper":
        if capability.allow_helper_model:
            _append(policy.helper_model)
        if not candidates:
            degraded_reason = "No helper model is configured."
    else:
        raise ValueError(f"Unsupported model route purpose: {purpose}")

    selected_model = candidates[0] if candidates else None
    degraded = selected_model is None
    if purpose == "assistant_reply" and selected_model != policy.primary_assistant_model and not policy.allow_identity_fallback:
        degraded = True
        degraded_reason = "GISMO's main voice is not available right now."
        candidates = []
        selected_model = None

    decision = ModelRouteDecision(
        purpose=purpose,
        selected_model=selected_model,
        candidate_models=candidates,
        degraded=degraded,
        degraded_reason=degraded_reason,
        capability=capability,
        policy=policy,
    )
    LOGGER.info(
        "model_route purpose=%s selected=%s candidates=%s tier=%s degraded=%s",
        purpose,
        decision.selected_model,
        decision.candidate_models,
        decision.capability.tier,
        decision.degraded,
    )
    return decision


def get_model_health(db_path: str) -> dict[str, Any]:
    discovery = discover_models()
    policy = load_model_policy(db_path)
    assistant_route = resolve_model_route(db_path, purpose="assistant_reply")
    planner_route = resolve_model_route(db_path, purpose="planner")
    issues: list[str] = []
    installed = set(discovery.get("installed_models") or [])
    for role, model_name in [
        ("primary_assistant_model", policy.primary_assistant_model),
        ("planner_model", policy.planner_model),
    ]:
        if model_name not in installed:
            issues.append(f"{role} is not installed")
    if policy.helper_model and policy.helper_model not in installed:
        issues.append("helper_model is not installed")
    if assistant_route.degraded and assistant_route.degraded_reason:
        issues.append(assistant_route.degraded_reason)

    loaded_models = discovery.get("loaded_models") or []
    failures = {
        purpose: failure
        for purpose, failure in _RUNTIME_FAILURES.items()
        if _recent_failure(purpose) is not None
    }
    return {
        "installed_models": discovery.get("installed_models") or [],
        "loaded_models": loaded_models,
        "policy": policy.to_dict(),
        "assistant_route": assistant_route.to_dict(),
        "planner_route": planner_route.to_dict(),
        "degraded_mode": {
            "active": bool(assistant_route.degraded),
            "reason": assistant_route.degraded_reason,
        },
        "issues": issues,
        "runtime_failures": failures,
        "ollama_available": bool(discovery.get("ollama_available")),
    }
