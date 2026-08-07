"""Security gates for state-changing web control requests."""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
from dataclasses import dataclass
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from gismo.core.state import StateStore

_SETTINGS_NAMESPACE = "gismo:settings"
_REMOTE_CONTROL_KEY = "web.remote_control_enabled"
_TOKEN_COOKIE_NAME = "gismo_control_token"
_CSRF_COOKIE_NAME = "gismo_control_csrf"
_CSRF_HEADER_NAME = "X-GISMO-CSRF-Token"
MAX_CONTROL_BODY_BYTES = 4096
_MAX_STRING_LENGTH = 256
_MAX_PARAM_KEYS = 16
_MAX_PARAM_DEPTH = 3
_MAX_LIST_ITEMS = 16
_CONTROL_ACTIONS = {"turn_on", "turn_off", "set_brightness", "set_color_temp", "set_color_rgb"}


@dataclass(frozen=True)
class ControlSecurityContext:
    db_path: str
    bind_host: str
    remote_control_enabled: bool
    token: str
    csrf_token: str
    token_path: Path


@dataclass(frozen=True)
class ControlRequestApproval:
    body: dict[str, Any]
    auth_mode: str
    client_ip: str


@dataclass(frozen=True)
class ControlRequestRejection:
    status: int
    message: str
    event_type: str
    reason: str
    client_ip: str


def build_context(db_path: str, *, bind_host: str = "127.0.0.1") -> ControlSecurityContext:
    token_path = control_token_path(db_path)
    token = _load_or_create_token(token_path)
    return ControlSecurityContext(
        db_path=db_path,
        bind_host=(bind_host or "127.0.0.1").strip() or "127.0.0.1",
        remote_control_enabled=_load_remote_control_enabled(db_path),
        token=token,
        csrf_token=hmac.new(token.encode("utf-8"), b"gismo-web-control-csrf-v1", sha256).hexdigest(),
        token_path=token_path,
    )


def control_token_path(db_path: str) -> Path:
    db_file = Path(db_path).resolve()
    digest = sha256(str(db_file).encode("utf-8")).hexdigest()[:12]
    return db_file.parent / ".gismo" / f"web-control-{digest}.token"


def response_headers(
    context: ControlSecurityContext,
    *,
    client_ip: str,
) -> list[tuple[str, str]]:
    if not is_loopback_address(client_ip):
        return []
    return [
        (
            "Set-Cookie",
            f"{_TOKEN_COOKIE_NAME}={context.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
        ),
        (
            "Set-Cookie",
            f"{_CSRF_COOKIE_NAME}={context.csrf_token}; Path=/; SameSite=Strict; Max-Age=31536000",
        ),
    ]


def authorize_control_request(
    *,
    context: ControlSecurityContext,
    method: str,
    path: str,
    client_ip: str,
    headers: Mapping[str, str],
    body_bytes: bytes,
) -> ControlRequestApproval | ControlRequestRejection:
    normalized_method = (method or "").upper()
    if normalized_method != "POST":
        return ControlRequestRejection(405, "Use POST for actuator control.", "web_control_unauthorized", "invalid_method", client_ip)

    content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return ControlRequestRejection(415, "Content-Type must be application/json.", "web_control_unauthorized", "invalid_content_type", client_ip)

    if len(body_bytes) > MAX_CONTROL_BODY_BYTES:
        return ControlRequestRejection(413, "Control request is too large.", "web_control_unauthorized", "body_too_large", client_ip)

    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ControlRequestRejection(400, "Request body must be valid JSON.", "web_control_unauthorized", "invalid_json", client_ip)
    if not isinstance(payload, dict):
        return ControlRequestRejection(400, "Control request body must be a JSON object.", "web_control_unauthorized", "invalid_body_type", client_ip)

    validation_error = _validate_payload_shape(payload)
    if validation_error is not None:
        return ControlRequestRejection(400, validation_error, "web_control_unauthorized", "invalid_parameters", client_ip)

    loopback_client = is_loopback_address(client_ip)
    loopback_binding = is_loopback_address(context.bind_host)
    if (not loopback_client or not loopback_binding) and not context.remote_control_enabled:
        return ControlRequestRejection(403, "Remote actuator control is disabled.", "web_control_rejected_exposure", "remote_control_disabled", client_ip)

    presented_token, auth_mode = _extract_presented_token(headers)
    if not presented_token or not hmac.compare_digest(presented_token, context.token):
        return ControlRequestRejection(401, "Actuator control is not authorized.", "web_control_unauthorized", "invalid_token", client_ip)

    csrf_error = _validate_csrf(
        headers=headers,
        path=path,
        client_ip=client_ip,
        auth_mode=auth_mode,
        expected_token=context.csrf_token,
    )
    if csrf_error is not None:
        return ControlRequestRejection(403, csrf_error, "web_control_invalid_csrf", "origin_mismatch", client_ip)

    return ControlRequestApproval(body=payload, auth_mode=auth_mode, client_ip=client_ip)


def record_rejection(context: ControlSecurityContext, rejection: ControlRequestRejection) -> None:
    _record_event(
        context,
        event_type=rejection.event_type,
        client_ip=rejection.client_ip,
        result="rejected",
        reason=rejection.reason,
    )


def record_accepted_request(context: ControlSecurityContext, approval: ControlRequestApproval) -> None:
    _record_event(
        context,
        event_type="web_control_request_accepted",
        client_ip=approval.client_ip,
        result="accepted",
        reason=approval.auth_mode,
    )


def is_loopback_address(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _extract_presented_token(headers: Mapping[str, str]) -> tuple[str | None, str]:
    header_token = (headers.get("X-GISMO-Control-Token") or "").strip()
    if header_token:
        return header_token, "header"
    cookie_header = headers.get("Cookie") or ""
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    token = cookie.get(_TOKEN_COOKIE_NAME)
    if token is not None and token.value.strip():
        return token.value.strip(), "cookie"
    return None, "missing"


def _validate_csrf(
    *,
    headers: Mapping[str, str],
    path: str,
    client_ip: str,
    auth_mode: str,
    expected_token: str,
) -> str | None:
    if auth_mode != "cookie":
        return None
    presented_token = (headers.get(_CSRF_HEADER_NAME) or "").strip()
    if not presented_token or not hmac.compare_digest(presented_token, expected_token):
        return "Actuator control requires a valid CSRF proof."
    request_host = (headers.get("Host") or "").strip().lower()
    source = (headers.get("Origin") or headers.get("Referer") or "").strip()
    if not source:
        return "Actuator control requires a same-origin request."
    parsed = urlparse(source)
    source_host = (parsed.netloc or "").strip().lower()
    if not source_host or not request_host or source_host != request_host:
        return "Actuator control requires a same-origin request."
    return None


def _validate_payload_shape(payload: dict[str, Any]) -> str | None:
    device_ref = payload.get("device_ref")
    if not isinstance(device_ref, str) or not device_ref.strip():
        return "device_ref is required"
    if len(device_ref.strip()) > _MAX_STRING_LENGTH:
        return "device_ref is too long"
    commands = payload.get("commands")
    if commands is not None:
        if set(payload) - {"device_ref", "commands"}:
            return "control request has unexpected fields"
        if not isinstance(commands, list) or not commands or len(commands) > _MAX_LIST_ITEMS:
            return "commands must be a non-empty bounded list"
        for command in commands:
            if not isinstance(command, dict):
                return "each command must be an object"
            error = _validate_action(command)
            if error is not None:
                return error
        return None
    if set(payload) - {"device_ref", "action", "params"}:
        return "control request has unexpected fields"
    return _validate_action(payload)


def _validate_action(payload: dict[str, Any]) -> str | None:
    action = payload.get("action")
    params = payload.get("params", {})
    if not isinstance(action, str) or not action.strip():
        return "action is required"
    if len(action.strip()) > _MAX_STRING_LENGTH:
        return "action is too long"
    if action.strip().lower() not in _CONTROL_ACTIONS:
        return "Unsupported actuator action."
    if not isinstance(params, dict):
        return "params must be an object"
    if len(params) > _MAX_PARAM_KEYS:
        return "params has too many entries"
    if not _value_within_limits(params, depth=1):
        return "params is too large"
    return None


def _value_within_limits(value: Any, *, depth: int) -> bool:
    if depth > _MAX_PARAM_DEPTH:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_STRING_LENGTH
    if isinstance(value, list):
        return len(value) <= _MAX_LIST_ITEMS and all(_value_within_limits(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        if len(value) > _MAX_PARAM_KEYS:
            return False
        for key, nested in value.items():
            if not isinstance(key, str) or len(key) > _MAX_STRING_LENGTH:
                return False
            if not _value_within_limits(nested, depth=depth + 1):
                return False
        return True
    return False


def _load_remote_control_enabled(db_path: str) -> bool:
    from gismo.memory.store import get_item

    item = get_item(
        db_path,
        namespace=_SETTINGS_NAMESPACE,
        key=_REMOTE_CONTROL_KEY,
        include_tombstoned=False,
        actor="web-control",
        policy_hash="web-control",
    )
    return bool(item is not None and item.value is True)


def _load_or_create_token(token_path: Path) -> str:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        return token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_path.write_text(token, encoding="utf-8")
    return token


def _record_event(
    context: ControlSecurityContext,
    *,
    event_type: str,
    client_ip: str,
    result: str,
    reason: str,
) -> None:
    with StateStore(context.db_path) as store:
        store.record_security_event(
            event_type=event_type,
            actor="web_control",
            action="actuator_control",
            resource="/api/actuators/control",
            payload={
                "bind_host": context.bind_host,
                "client_ip": client_ip,
                "client_scope": "loopback" if is_loopback_address(client_ip) else "remote",
                "remote_control_enabled": context.remote_control_enabled,
                "result": result,
                "reason": reason,
            },
        )
