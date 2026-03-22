"""Signed plugin and adapter manifest verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gismo.core.security_events import append_security_event


_CANONICAL_KWARGS = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}
DEFAULT_TRUST_STORE_NAME = "plugin-trust.json"


class PluginVerificationError(PermissionError):
    """Raised when a plugin manifest is unsigned or invalid."""


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    entrypoint: str
    signer_id: str
    capabilities: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "signer_id": self.signer_id,
            "capabilities": list(self.capabilities),
            "constraints": dict(self.constraints),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_payload()
        payload["signature"] = self.signature
        return payload


@dataclass(frozen=True)
class TrustedSigner:
    signer_id: str
    shared_secret: str
    trusted: bool = True
    note: str | None = None


@dataclass(frozen=True)
class PluginTrustStore:
    signers: dict[str, TrustedSigner] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PluginTrustStore":
        if not isinstance(payload, dict):
            raise PluginVerificationError("Plugin trust store must be a JSON object.")
        signers_payload = payload.get("signers", payload)
        if not isinstance(signers_payload, dict):
            raise PluginVerificationError("Plugin trust store signers must be an object.")
        signers: dict[str, TrustedSigner] = {}
        for signer_id, raw in signers_payload.items():
            if not isinstance(raw, dict):
                raise PluginVerificationError(f"Signer entry must be an object: {signer_id}")
            signers[signer_id] = TrustedSigner(
                signer_id=signer_id,
                shared_secret=_require_str(raw.get("shared_secret"), f"shared_secret for {signer_id}"),
                trusted=bool(raw.get("trusted", True)),
                note=str(raw.get("note")).strip() if raw.get("note") is not None else None,
            )
        return cls(signers=signers)

    def get_signer(self, signer_id: str) -> TrustedSigner | None:
        signer = self.signers.get(signer_id)
        if signer is None or not signer.trusted:
            return None
        return signer


@dataclass(frozen=True)
class PluginSignaturePolicy:
    trusted_signers: dict[str, str]
    require_signature: bool = True


@dataclass(frozen=True)
class PluginVerificationReport:
    plugin_id: str | None
    version: str | None
    entrypoint: str | None
    signer_id: str | None
    signed: bool
    trusted_signer: bool
    tampered: bool | None
    verified: bool
    reason: str | None = None
    manifest_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "signer_id": self.signer_id,
            "signed": self.signed,
            "trusted_signer": self.trusted_signer,
            "tampered": self.tampered,
            "verified": self.verified,
            "reason": self.reason,
            "manifest_sha256": self.manifest_sha256,
        }


def default_trust_store_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).resolve().parent / DEFAULT_TRUST_STORE_NAME
    return (Path(".gismo") / DEFAULT_TRUST_STORE_NAME).resolve()


def load_trust_store(path: str | Path) -> PluginTrustStore:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return PluginTrustStore.from_dict(payload)


def policy_from_trust_store(trust_store: PluginTrustStore) -> PluginSignaturePolicy:
    return PluginSignaturePolicy(
        trusted_signers={
            signer_id: signer.shared_secret
            for signer_id, signer in trust_store.signers.items()
            if signer.trusted
        },
        require_signature=True,
    )


def load_manifest(path: str | Path) -> PluginManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PluginVerificationError("Plugin manifest must be a JSON object.")
    return manifest_from_dict(payload)


def manifest_from_dict(payload: dict[str, Any]) -> PluginManifest:
    plugin_id = _require_str(payload.get("plugin_id"), "plugin_id")
    version = _require_str(payload.get("version"), "version")
    entrypoint = _require_str(payload.get("entrypoint"), "entrypoint")
    signer_id = _require_str(payload.get("signer_id"), "signer_id")
    capabilities = payload.get("capabilities", [])
    constraints = payload.get("constraints", {})
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise PluginVerificationError("Plugin capabilities must be a list of strings.")
    if not isinstance(constraints, dict):
        raise PluginVerificationError("Plugin constraints must be an object.")
    signature = payload.get("signature")
    if signature is not None and (not isinstance(signature, str) or not signature.strip()):
        raise PluginVerificationError("Plugin signature must be a non-empty string when present.")
    return PluginManifest(
        plugin_id=plugin_id,
        version=version,
        entrypoint=entrypoint,
        signer_id=signer_id,
        capabilities=list(capabilities),
        constraints=dict(constraints),
        signature=signature.strip() if isinstance(signature, str) else None,
    )


def canonical_manifest_json(manifest: PluginManifest) -> str:
    return json.dumps(manifest.unsigned_payload(), **_CANONICAL_KWARGS)


def sign_manifest(manifest: PluginManifest, *, shared_secret: str) -> str:
    return hmac.new(
        shared_secret.encode("utf-8"),
        canonical_manifest_json(manifest).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def inspect_manifest(
    manifest: PluginManifest,
    *,
    trust_store: PluginTrustStore | None = None,
) -> PluginVerificationReport:
    effective_trust_store = trust_store or PluginTrustStore()
    manifest_sha256 = hashlib.sha256(
        canonical_manifest_json(manifest).encode("utf-8")
    ).hexdigest()
    signed = bool(manifest.signature)
    signer = effective_trust_store.get_signer(manifest.signer_id)
    trusted_signer = signer is not None
    if not signed:
        return PluginVerificationReport(
            plugin_id=manifest.plugin_id,
            version=manifest.version,
            entrypoint=manifest.entrypoint,
            signer_id=manifest.signer_id,
            signed=False,
            trusted_signer=trusted_signer,
            tampered=None,
            verified=False,
            reason="Unsigned plugins and adapters may not execute.",
            manifest_sha256=manifest_sha256,
        )
    if signer is None:
        return PluginVerificationReport(
            plugin_id=manifest.plugin_id,
            version=manifest.version,
            entrypoint=manifest.entrypoint,
            signer_id=manifest.signer_id,
            signed=True,
            trusted_signer=False,
            tampered=None,
            verified=False,
            reason=f"Signer is not trusted: {manifest.signer_id}",
            manifest_sha256=manifest_sha256,
        )
    expected_signature = sign_manifest(manifest, shared_secret=signer.shared_secret)
    tampered = not hmac.compare_digest(manifest.signature or "", expected_signature)
    return PluginVerificationReport(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        entrypoint=manifest.entrypoint,
        signer_id=manifest.signer_id,
        signed=True,
        trusted_signer=True,
        tampered=tampered,
        verified=not tampered,
        reason=None if not tampered else "Plugin manifest signature check failed.",
        manifest_sha256=manifest_sha256,
    )


def verify_manifest(
    manifest: PluginManifest,
    *,
    policy: PluginSignaturePolicy | None = None,
    trust_store: PluginTrustStore | None = None,
    db_path: str | None = None,
    actor: str = "plugin_loader",
) -> dict[str, Any]:
    effective_policy = policy or policy_from_trust_store(trust_store or PluginTrustStore())
    payload = manifest.to_dict()
    resource = f"plugin:{manifest.plugin_id}@{manifest.version}"
    try:
        report = inspect_manifest(
            manifest,
            trust_store=trust_store or PluginTrustStore(
                signers={
                    signer_id: TrustedSigner(signer_id=signer_id, shared_secret=secret)
                    for signer_id, secret in effective_policy.trusted_signers.items()
                }
            ),
        )
        signature = manifest.signature
        if effective_policy.require_signature and not signature:
            raise PluginVerificationError("Unsigned plugins and adapters may not execute.")
        if not report.trusted_signer:
            raise PluginVerificationError(f"Signer is not trusted: {manifest.signer_id}")
        if report.tampered:
            raise PluginVerificationError("Plugin manifest signature check failed.")
        payload["signature_valid"] = True
        payload["manifest_sha256"] = report.manifest_sha256
        payload["verification_status"] = "verified"
        payload["trust_labels"] = ["imported", "verified", "trusted"]
        payload["provenance"] = {
            "signer_id": manifest.signer_id,
            "entrypoint": manifest.entrypoint,
        }
        payload["trusted_signer"] = report.trusted_signer
        payload["tampered"] = report.tampered
        _record_plugin_event(
            db_path=db_path,
            event_type="plugin_signature_verified",
            actor=actor,
            resource=resource,
            payload=payload,
        )
        return payload
    except PluginVerificationError as exc:
        _record_plugin_event(
            db_path=db_path,
            event_type="plugin_signature_rejected",
            actor=actor,
            resource=resource,
            payload={
                "manifest": payload,
                "reason": str(exc),
            },
        )
        raise


def describe_signers(trust_store: PluginTrustStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signer_id in sorted(trust_store.signers):
        signer = trust_store.signers[signer_id]
        rows.append(
            {
                "signer_id": signer.signer_id,
                "trusted": signer.trusted,
                "note": signer.note,
                "has_shared_secret": bool(signer.shared_secret),
            }
        )
    return rows


def load_verified_manifest(
    manifest_path: str | Path,
    *,
    trust_store_path: str | Path | None = None,
    trust_store: PluginTrustStore | None = None,
    db_path: str | None = None,
    actor: str = "plugin_loader",
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    effective_trust_store = trust_store
    if effective_trust_store is None and trust_store_path is not None:
        effective_trust_store = load_trust_store(trust_store_path)
    return verify_manifest(
        manifest,
        trust_store=effective_trust_store,
        db_path=db_path,
        actor=actor,
    )


def _record_plugin_event(
    *,
    db_path: str | None,
    event_type: str,
    actor: str,
    resource: str,
    payload: dict[str, Any],
) -> None:
    if not db_path or not str(db_path).strip():
        return
    append_security_event(
        db_path=db_path,
        event_type=event_type,
        actor=actor,
        action="verify",
        resource=resource,
        payload=payload,
    )


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginVerificationError(f"Plugin manifest {field} must be a non-empty string.")
    return value.strip()
