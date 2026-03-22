import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.plugin_signing import (
    PluginManifest,
    PluginTrustStore,
    PluginVerificationError,
    sign_manifest,
    verify_manifest,
)
from gismo.core.state import StateStore


class PluginSigningTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _trust_store(self, signer_id: str = "demo-signer", secret: str = "shared-secret") -> PluginTrustStore:
        return PluginTrustStore.from_dict(
            {
                "signers": {
                    signer_id: {
                        "shared_secret": secret,
                        "trusted": True,
                    }
                }
            }
        )

    def test_unsigned_manifest_is_rejected_and_logged(self) -> None:
        tmpdir = self._tmpdir("plugin-unsigned")
        try:
            db_path = str(tmpdir / "state.db")
            StateStore(db_path)
            manifest = PluginManifest(
                plugin_id="demo.plugin",
                version="1.0.0",
                entrypoint="demo:main",
                signer_id="demo-signer",
                capabilities=["echo"],
            )
            with self.assertRaises(PluginVerificationError):
                verify_manifest(manifest, trust_store=self._trust_store(), db_path=db_path)
            events = StateStore(db_path).list_security_events(limit=10, event_type="plugin_signature_rejected")
            self.assertEqual(len(events), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tampered_manifest_is_rejected(self) -> None:
        manifest = PluginManifest(
            plugin_id="demo.plugin",
            version="1.0.0",
            entrypoint="demo:main",
            signer_id="demo-signer",
            capabilities=["echo"],
        )
        secret = "shared-secret"
        signed = PluginManifest(
            plugin_id=manifest.plugin_id,
            version="1.0.1",
            entrypoint=manifest.entrypoint,
            signer_id=manifest.signer_id,
            capabilities=manifest.capabilities,
            constraints=manifest.constraints,
            signature=sign_manifest(manifest, shared_secret=secret),
        )
        with self.assertRaises(PluginVerificationError):
            verify_manifest(signed, trust_store=self._trust_store(secret=secret))

    def test_untrusted_signer_is_rejected(self) -> None:
        manifest = PluginManifest(
            plugin_id="demo.plugin",
            version="1.0.0",
            entrypoint="demo:main",
            signer_id="other-signer",
            capabilities=["echo"],
            signature="abc",
        )
        with self.assertRaises(PluginVerificationError):
            verify_manifest(manifest, trust_store=self._trust_store())

    def test_signed_manifest_is_accepted_and_logged(self) -> None:
        tmpdir = self._tmpdir("plugin-signed")
        try:
            db_path = str(tmpdir / "state.db")
            StateStore(db_path)
            manifest = PluginManifest(
                plugin_id="demo.plugin",
                version="1.0.0",
                entrypoint="demo:main",
                signer_id="demo-signer",
                capabilities=["echo"],
            )
            secret = "shared-secret"
            signed = PluginManifest(
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                entrypoint=manifest.entrypoint,
                signer_id=manifest.signer_id,
                capabilities=manifest.capabilities,
                constraints=manifest.constraints,
                signature=sign_manifest(manifest, shared_secret=secret),
            )
            payload = verify_manifest(
                signed,
                trust_store=self._trust_store(secret=secret),
                db_path=db_path,
            )

            self.assertTrue(payload["signature_valid"])
            self.assertEqual(payload["plugin_id"], "demo.plugin")
            self.assertIn("trusted", payload["trust_labels"])
            events = StateStore(db_path).list_security_events(limit=10, event_type="plugin_signature_verified")
            self.assertEqual(len(events), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
