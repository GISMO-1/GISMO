import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.state import StateStore
from gismo.web import api as web_api


class SecurityApiTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_security_events_api_filters_and_selected_event(self) -> None:
        tmpdir = self._tmpdir("security-api-events")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                event = store.record_security_event(
                    event_type="outbound_denied",
                    actor="operator",
                    action="connect",
                    resource="network:public",
                    payload={"destination": "8.8.8.8"},
                    related_run_id="run-1",
                )
                store.record_security_event(
                    event_type="quarantine_created",
                    actor="operator",
                    action="create",
                    resource="quarantine:item",
                    payload={},
                    related_run_id="run-2",
                )
            payload = web_api.get_security_events(
                db_path,
                event_type="outbound_denied",
                related_run_id="run-1",
                event_id=event.id,
                limit=10,
            )
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["id"], event.id)
            self.assertEqual(payload["selected_event"]["id"], event.id)
            self.assertIn("chain", payload["selected_event"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_security_execution_api_filters_and_detail(self) -> None:
        tmpdir = self._tmpdir("security-api-execution")
        try:
            db_path = str(tmpdir / "state.db")
            execution_id = "11111111-2222-3333-4444-555555555555"
            with StateStore(db_path) as store:
                store.record_security_event(
                    event_type="execution_mode_selected",
                    actor="worker",
                    action="execute",
                    resource="tool:plugin_runtime",
                    payload={
                        "execution_id": execution_id,
                        "component": "plugin_runtime",
                        "zone": "plugin_runtime",
                        "mode": "sandboxed",
                        "action": "execute",
                        "resource": "tool:plugin_runtime",
                    },
                    related_run_id="run-9",
                )
                store.record_security_event(
                    event_type="isolated_execution_finished",
                    actor="worker",
                    action="execute",
                    resource="tool:plugin_runtime",
                    payload={
                        "execution_id": execution_id,
                        "component": "plugin_runtime",
                        "zone": "plugin_runtime",
                        "mode": "sandboxed",
                        "action": "execute",
                        "resource": "tool:plugin_runtime",
                        "result": "succeeded",
                    },
                    related_run_id="run-9",
                )
            payload = web_api.get_security_execution(
                db_path,
                mode="sandboxed",
                zone="plugin_runtime",
                component="plugin_runtime",
                related_run_id="run-9",
                recent=10,
            )
            self.assertEqual(len(payload["executions"]), 1)
            self.assertEqual(payload["executions"][0]["execution_id"], execution_id)

            detail = web_api.get_security_execution_detail(db_path, execution_id[:12])
            self.assertEqual(detail["execution"]["execution_id"], execution_id)
            self.assertEqual(len(detail["events"]), 2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_quarantine_api_promote_and_duplicate_prevention(self) -> None:
        tmpdir = self._tmpdir("security-api-promote")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                record = store.create_quarantine_entry(
                    source_kind="web_import",
                    source_ref="https://example.test",
                    origin_type="external",
                    content={"value": "hello"},
                    content_sha256="hash",
                    actor="test",
                    trust_labels=["external"],
                    verification_status="unverified",
                )
            payload = web_api.promote_quarantine_entry(
                db_path,
                record.id,
                {
                    "labels": ["external", "verified", "trusted"],
                    "reason": "Reviewed",
                    "namespace": "global",
                    "key": "reviewed",
                },
            )
            self.assertEqual(payload["quarantine"]["status"], "promoted")
            self.assertEqual(payload["memory_item"]["key"], "reviewed")

            with self.assertRaises(ValueError):
                web_api.promote_quarantine_entry(
                    db_path,
                    record.id,
                    {
                        "labels": ["external", "verified", "trusted"],
                        "reason": "Reviewed again",
                        "namespace": "global",
                        "key": "reviewed-again",
                    },
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_quarantine_api_reject_prevents_duplicate_transition(self) -> None:
        tmpdir = self._tmpdir("security-api-reject")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                record = store.create_quarantine_entry(
                    source_kind="llm_reply",
                    source_ref="model",
                    origin_type="model_output",
                    content="draft",
                    content_sha256="hash",
                    actor="test",
                    trust_labels=["gismo_inferred"],
                    verification_status="unverified",
                )
            payload = web_api.reject_quarantine_entry(
                db_path,
                record.id,
                {"reason": "Rejected"},
            )
            self.assertEqual(payload["status"], "rejected")
            with self.assertRaises(ValueError):
                web_api.reject_quarantine_entry(
                    db_path,
                    record.id,
                    {"reason": "Rejected again"},
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_quarantine_api_promotion_requires_explicit_labels_and_reason(self) -> None:
        tmpdir = self._tmpdir("security-api-required")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                record = store.create_quarantine_entry(
                    source_kind="import",
                    source_ref="file.txt",
                    origin_type="import",
                    content="text",
                    content_sha256="hash",
                    actor="test",
                    trust_labels=["imported"],
                    verification_status="unverified",
                )
            with self.assertRaises(ValueError):
                web_api.promote_quarantine_entry(
                    db_path,
                    record.id,
                    {"reason": "Missing labels", "namespace": "global", "key": "x"},
                )
            with self.assertRaises(ValueError):
                web_api.promote_quarantine_entry(
                    db_path,
                    record.id,
                    {"labels": ["trusted"], "namespace": "global", "key": "x"},
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
