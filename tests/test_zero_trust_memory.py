import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.state import StateStore
from gismo.memory.store import (
    fetch_item_raw,
    list_prompt_items,
    put_item,
    transition_item_trust,
)


class ZeroTrustMemoryTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_untrusted_model_memory_is_not_used_for_prompt_injection(self) -> None:
        tmpdir = self._tmpdir("trust-memory")
        try:
            db_path = str(tmpdir / "state.db")
            put_item(
                db_path,
                namespace="global",
                key="trusted-note",
                kind="fact",
                value={"text": "operator"},
                tags=[],
                confidence="high",
                source="operator",
                ttl_seconds=None,
                actor="test",
                policy_hash="policy",
            )
            put_item(
                db_path,
                namespace="global",
                key="llm-note",
                kind="fact",
                value={"text": "model"},
                tags=[],
                confidence="high",
                source="llm",
                source_type="model_output",
                verification_status="unverified",
                trust_labels=["gismo_inferred"],
                provenance_json={"source": "test"},
                ttl_seconds=None,
                actor="test",
                policy_hash="policy",
            )

            prompt_items = list_prompt_items(db_path, limit=10)

            self.assertEqual([item.key for item in prompt_items], ["trusted-note"])
            llm_item = fetch_item_raw(db_path, namespace="global", key="llm-note")
            assert llm_item is not None
            self.assertEqual(llm_item.verification_status, "unverified")
            self.assertNotIn("trusted", llm_item.trust_labels)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_quarantine_promotion_requires_explicit_reason_and_logs_transition(self) -> None:
        tmpdir = self._tmpdir("quarantine-promote")
        try:
            db_path = str(tmpdir / "state.db")
            state_store = StateStore(db_path)
            record = state_store.create_quarantine_entry(
                source_kind="memory_snapshot",
                source_ref="snapshot.json",
                origin_type="memory_snapshot",
                content_sha256="abc123",
                actor="test",
                trust_labels=["imported"],
                verification_status="unverified",
                provenance_json={"snapshot": "snapshot.json"},
                metadata_json={"kind": "memory"},
            )

            with self.assertRaises(ValueError):
                state_store.promote_quarantine_record(
                    record.id,
                    namespace="global",
                    key="promoted",
                    kind="fact",
                    value={"text": "from snapshot"},
                    source="import",
                    actor="test",
                    trust_labels=["imported", "verified", "trusted"],
                    verification_status="verified",
                    reason="",
                    policy_hash="policy",
                )

            item = state_store.promote_quarantine_record(
                record.id,
                namespace="global",
                key="promoted",
                kind="fact",
                value={"text": "from snapshot"},
                source="import",
                actor="test",
                trust_labels=["imported", "verified", "trusted"],
                verification_status="verified",
                reason="operator reviewed imported snapshot",
                policy_hash="policy",
            )

            self.assertIn("trusted", item.trust_labels)
            promoted = state_store.get_quarantine_record(record.id)
            assert promoted is not None
            self.assertEqual(promoted.status, "promoted")
            events = state_store.list_security_events(limit=20)
            event_types = [event.event_type for event in events]
            self.assertIn("quarantine_promoted", event_types)
            self.assertIn("trust_transition", event_types)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_memory_trust_transition_is_logged(self) -> None:
        tmpdir = self._tmpdir("memory-transition")
        try:
            db_path = str(tmpdir / "state.db")
            put_item(
                db_path,
                namespace="global",
                key="candidate",
                kind="fact",
                value={"text": "candidate"},
                tags=[],
                confidence="high",
                source="llm",
                source_type="model_output",
                verification_status="unverified",
                trust_labels=["gismo_inferred"],
                provenance_json={"source": "test"},
                ttl_seconds=None,
                actor="test",
                policy_hash="policy",
            )

            updated = transition_item_trust(
                db_path,
                namespace="global",
                key="candidate",
                trust_labels=["gismo_inferred", "verified", "trusted"],
                verification_status="verified",
                actor="test",
                reason="operator confirmed the fact",
                policy_hash="policy",
            )

            self.assertIn("trusted", updated.trust_labels)
            events = StateStore(db_path).list_security_events(limit=10, event_type="trust_transition")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["reason"], "operator confirmed the fact")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
