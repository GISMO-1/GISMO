import argparse
import contextlib
import io
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.cli import main as cli_main
from gismo.cli import security_cli
from gismo.core.plugin_signing import PluginManifest, sign_manifest
from gismo.core.state import StateStore
from gismo.memory.store import put_item


class SecurityCliTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _capture(
        self,
        func,
        args: argparse.Namespace,
    ) -> tuple[str, str, int | None]:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        exit_code: int | None = None
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            try:
                func(args)
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buffer.getvalue(), stderr_buffer.getvalue(), exit_code

    def test_parser_routes_security_commands(self) -> None:
        parser = cli_main.build_parser()

        security_args = parser.parse_args(["security", "events"])
        self.assertIs(security_args.handler, cli_main._handle_security_events)

        zones_args = parser.parse_args(["security", "zones"])
        self.assertIs(zones_args.handler, cli_main._handle_security_zones)

        execution_args = parser.parse_args(["security", "execution"])
        self.assertIs(execution_args.handler, cli_main._handle_security_execution)

        execution_inspect_args = parser.parse_args(["security", "execution", "inspect", "abc"])
        self.assertIs(execution_inspect_args.handler, cli_main._handle_security_execution_inspect)

        quarantine_args = parser.parse_args(["quarantine", "inspect", "abc"])
        self.assertIs(quarantine_args.handler, cli_main._handle_quarantine_inspect)

        trust_args = parser.parse_args(["trust", "inspect-memory", "global/item"])
        self.assertIs(trust_args.handler, cli_main._handle_trust_inspect_memory)

        plugin_args = parser.parse_args(["plugins", "signers"])
        self.assertIs(plugin_args.handler, cli_main._handle_plugins_signers)

    def test_security_zones_cli_reports_component_bindings(self) -> None:
        args = argparse.Namespace(component="run_shell", json=True)
        stdout, stderr, exit_code = self._capture(security_cli.run_security_zones, args)
        self.assertIsNone(exit_code, stderr)
        payload = json.loads(stdout)
        self.assertEqual(len(payload["components"]), 1)
        self.assertEqual(payload["components"][0]["component"], "run_shell")
        self.assertEqual(payload["components"][0]["default_execution_mode"], "isolated_subprocess")

    def test_quarantine_promote_cli_and_duplicate_prevention(self) -> None:
        tmpdir = self._tmpdir("security-cli-promote")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                record = store.create_quarantine_entry(
                    source_kind="web_import",
                    source_ref="https://example.test/item",
                    origin_type="external",
                    content={"title": "hello"},
                    content_sha256="abc123",
                    actor="test",
                    trust_labels=["external"],
                    verification_status="unverified",
                )
            args = argparse.Namespace(
                db_path=db_path,
                record_id=record.id,
                labels=["external", "verified", "trusted"],
                reason="Reviewed by operator",
                namespace="global",
                key="reviewed-item",
                kind="fact",
                source="operator-review",
                verification_status=None,
                value=None,
                value_text=None,
                json=True,
            )

            stdout, stderr, exit_code = self._capture(security_cli.run_quarantine_promote, args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["quarantine"]["status"], "promoted")
            self.assertEqual(payload["memory_item"]["namespace"], "global")
            self.assertEqual(payload["memory_item"]["key"], "reviewed-item")
            self.assertIn("trusted", payload["memory_item"]["trust_labels"])

            stdout, stderr, exit_code = self._capture(security_cli.run_quarantine_promote, args)
            self.assertEqual(exit_code, 2)
            self.assertIn("already promoted", stderr)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_quarantine_reject_cli_prevents_duplicate_rejection(self) -> None:
        tmpdir = self._tmpdir("security-cli-reject")
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
            args = argparse.Namespace(
                db_path=db_path,
                record_id=record.id,
                reason="Rejected by operator",
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_quarantine_reject, args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["decision_reason"], "Rejected by operator")

            stdout, stderr, exit_code = self._capture(security_cli.run_quarantine_reject, args)
            self.assertEqual(exit_code, 2)
            self.assertIn("already rejected", stderr)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_security_events_cli_filters_and_event_detail(self) -> None:
        tmpdir = self._tmpdir("security-cli-events")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                event_a = store.record_security_event(
                    event_type="outbound_denied",
                    actor="operator",
                    action="connect",
                    resource="network:public",
                    payload={"destination": "1.1.1.1"},
                    related_run_id="run-1",
                )
                store.record_security_event(
                    event_type="quarantine_created",
                    actor="operator",
                    action="create",
                    resource="quarantine:item",
                    payload={"source_kind": "web"},
                    related_run_id="run-2",
                )
            list_args = argparse.Namespace(
                db_path=db_path,
                event_type="outbound_denied",
                run_id="run-1",
                task_id=None,
                plan_id=None,
                approval_id=None,
                event_id=None,
                limit=20,
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_security_events, list_args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["id"], event_a.id)

            inspect_args = argparse.Namespace(
                db_path=db_path,
                event_type=None,
                run_id=None,
                task_id=None,
                plan_id=None,
                approval_id=None,
                event_id=event_a.id,
                limit=20,
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_security_events, inspect_args)
            self.assertIsNone(exit_code, stderr)
            detail = json.loads(stdout)
            self.assertEqual(detail["id"], event_a.id)
            self.assertIn("chain", detail)
            self.assertEqual(detail["chain"]["current"]["id"], event_a.id)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_security_execution_cli_lists_and_inspects_execution_records(self) -> None:
        tmpdir = self._tmpdir("security-cli-execution")
        try:
            db_path = str(tmpdir / "state.db")
            execution_id = "11111111-2222-3333-4444-555555555555"
            with StateStore(db_path) as store:
                store.record_security_event(
                    event_type="execution_mode_selected",
                    actor="worker",
                    action="device_status",
                    resource="tool:device_control",
                    payload={
                        "execution_id": execution_id,
                        "component": "device_control",
                        "zone": "device_adapter",
                        "mode": "sandboxed",
                        "action": "device_status",
                        "resource": "tool:device_control",
                    },
                    related_run_id="run-1",
                    related_task_id="task-1",
                )
                store.record_security_event(
                    event_type="isolated_execution_finished",
                    actor="worker",
                    action="device_status",
                    resource="tool:device_control",
                    payload={
                        "execution_id": execution_id,
                        "component": "device_control",
                        "zone": "device_adapter",
                        "mode": "sandboxed",
                        "action": "device_status",
                        "resource": "tool:device_control",
                        "result": "succeeded",
                    },
                    related_run_id="run-1",
                    related_task_id="task-1",
                )

            list_args = argparse.Namespace(
                db_path=db_path,
                recent=10,
                mode="sandboxed",
                zone="device_adapter",
                component="device_control",
                run_id="run-1",
                task_id=None,
                plan_id=None,
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_security_execution, list_args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(len(payload["executions"]), 1)
            self.assertEqual(payload["executions"][0]["execution_id"], execution_id)

            inspect_args = argparse.Namespace(
                db_path=db_path,
                selector=execution_id[:12],
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_security_execution_inspect, inspect_args)
            self.assertIsNone(exit_code, stderr)
            detail = json.loads(stdout)
            self.assertEqual(detail["execution"]["execution_id"], execution_id)
            self.assertEqual(len(detail["events"]), 2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_security_verify_chain_cli_reports_first_broken_event(self) -> None:
        tmpdir = self._tmpdir("security-cli-chain")
        try:
            db_path = str(tmpdir / "state.db")
            with StateStore(db_path) as store:
                first = store.record_security_event(
                    event_type="capability_issued",
                    actor="operator",
                    action="issue",
                    resource="capability:test",
                    payload={"step": 1},
                )
                store.record_security_event(
                    event_type="capability_verified",
                    actor="operator",
                    action="verify",
                    resource="capability:test",
                    payload={"step": 2},
                )
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "UPDATE security_events SET payload_json = ? WHERE seq = 1",
                    (json.dumps({"tampered": True}),),
                )
                connection.commit()
            finally:
                connection.close()
            args = argparse.Namespace(db_path=db_path, json=False)
            stdout, stderr, exit_code = self._capture(security_cli.run_security_verify_chain, args)
            self.assertEqual(exit_code, 2)
            self.assertIn(str(first.id), stdout)
            self.assertIn("failed at seq 1", stdout)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_trust_inspect_memory_reports_trust_eligibility(self) -> None:
        tmpdir = self._tmpdir("security-cli-trust")
        try:
            db_path = str(tmpdir / "state.db")
            put_item(
                db_path,
                namespace="global",
                key="assistant-draft",
                kind="fact",
                value={"text": "draft"},
                tags=[],
                confidence="high",
                source="llm",
                source_type="model_output",
                verification_status="unverified",
                trust_labels=["gismo_inferred"],
                provenance_json={"model": "demo"},
                ttl_seconds=None,
                actor="test",
                policy_hash="test",
            )
            args = argparse.Namespace(
                db_path=db_path,
                selector="assistant-draft",
                include_tombstoned=False,
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_trust_inspect_memory, args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["key"], "assistant-draft")
            self.assertFalse(payload["planning_eligibility"]["eligible"])
            self.assertIn("trust_not_promoted", payload["planning_eligibility"]["reasons"])
            self.assertFalse(payload["execution_eligibility"]["eligible"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_plugins_signers_and_verify_cli(self) -> None:
        tmpdir = self._tmpdir("security-cli-plugins")
        try:
            db_path = str(tmpdir / "state.db")
            StateStore(db_path)
            trust_store_path = tmpdir / "plugin-trust.json"
            trust_store_path.write_text(
                json.dumps(
                    {
                        "signers": {
                            "demo-signer": {
                                "shared_secret": "shared-secret",
                                "trusted": True,
                                "note": "local signer",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = PluginManifest(
                plugin_id="demo.plugin",
                version="1.0.0",
                entrypoint="demo:main",
                signer_id="demo-signer",
                capabilities=["echo"],
            )
            signed_manifest = {
                **manifest.to_dict(),
                "signature": sign_manifest(manifest, shared_secret="shared-secret"),
            }
            unsigned_manifest = {
                "plugin_id": "bad.plugin",
                "version": "0.1.0",
                "entrypoint": "bad:main",
                "signer_id": "demo-signer",
                "capabilities": ["echo"],
            }
            signed_path = tmpdir / "signed.json"
            unsigned_path = tmpdir / "unsigned.json"
            signed_path.write_text(json.dumps(signed_manifest), encoding="utf-8")
            unsigned_path.write_text(json.dumps(unsigned_manifest), encoding="utf-8")

            signers_args = argparse.Namespace(
                db_path=db_path,
                trust_store=str(trust_store_path),
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_plugins_signers, signers_args)
            self.assertIsNone(exit_code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["signers"][0]["signer_id"], "demo-signer")

            verify_args = argparse.Namespace(
                db_path=db_path,
                trust_store=str(trust_store_path),
                manifest=[str(signed_path), str(unsigned_path)],
                manifest_dir=None,
                json=True,
            )
            stdout, stderr, exit_code = self._capture(security_cli.run_plugins_verify, verify_args)
            self.assertEqual(exit_code, 2)
            payload = json.loads(stdout)
            self.assertEqual(payload["summary"]["verified"], 1)
            self.assertEqual(payload["summary"]["failed"], 1)
            reports = {Path(report["manifest_path"]).name: report for report in payload["reports"]}
            self.assertTrue(reports["signed.json"]["verified"])
            self.assertFalse(reports["unsigned.json"]["verified"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
