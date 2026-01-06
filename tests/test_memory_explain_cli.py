import argparse
import contextlib
import gc
import io
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.cli import memory_explain as memory_explain_cli
from gismo.core.models import EVENT_TYPE_LLM_PLAN
from gismo.core.state import StateStore
from gismo.memory.store import (
    MEMORY_SELECTION_TRACE_CAP,
    create_profile as memory_create_profile,
    list_prompt_items as memory_list_prompt_items,
    list_selection_traces,
    policy_hash_for_path as memory_policy_hash_for_path,
    put_item as memory_put_item,
    record_prompt_selection_trace,
)


class MemoryExplainCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.policy_path = self.repo_root / "policy" / "dev-safe.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_ask(self, *args: str) -> None:
        response = json.dumps(
            {
                "intent": "memory",
                "assumptions": [],
                "actions": [],
                "notes": [],
            }
        )
        env = {
            "GISMO_OLLAMA_MODEL": "",
            "GISMO_OLLAMA_TIMEOUT_S": "",
            "GISMO_OLLAMA_URL": "",
            "GISMO_LLM_MODEL": "",
            "OLLAMA_HOST": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(cli_main, "ollama_chat", return_value=response):
                cli_main.run_ask(
                    str(self.db_path),
                    "explain memory",
                    model=None,
                    host=None,
                    timeout_s=None,
                    enqueue=False,
                    dry_run=True,
                    max_actions=10,
                    yes=False,
                    explain=False,
                    use_memory=True if "--memory" in args else False,
                    memory_profile=self._extract_arg("--memory-profile", args),
                )

    @staticmethod
    def _extract_arg(flag: str, args: tuple[str, ...]) -> str | None:
        if flag not in args:
            return None
        index = args.index(flag)
        if index + 1 >= len(args):
            return None
        return args[index + 1]

    def _latest_plan_event_id(self) -> str:
        state_store = StateStore(str(self.db_path))
        try:
            events = state_store.list_events()
            for event in events:
                if event.event_type == EVENT_TYPE_LLM_PLAN:
                    return event.id
        finally:
            state_store.close()
        raise AssertionError("No plan event recorded.")

    def _seed_item(
        self,
        *,
        namespace: str,
        key: str,
        kind: str,
        confidence: str = "high",
    ) -> None:
        memory_put_item(
            str(self.db_path),
            namespace=namespace,
            key=key,
            kind=kind,
            value={"value": key},
            tags=None,
            confidence=confidence,
            source="operator",
            ttl_seconds=None,
            actor="test",
            policy_hash=memory_policy_hash_for_path(str(self.policy_path)),
        )

    def test_explain_profile_selection_reasons(self) -> None:
        profile = memory_create_profile(
            str(self.db_path),
            name="operators",
            description=None,
            include_namespaces=["global"],
            exclude_namespaces=None,
            include_kinds=["fact"],
            exclude_kinds=["procedure"],
            max_items=None,
        )
        self._seed_item(namespace="global", key="alpha", kind="fact")
        self._seed_item(namespace="global", key="beta", kind="procedure")
        self._seed_item(namespace="local", key="gamma", kind="fact")

        self._run_ask("--memory-profile", profile.name)
        plan_id = self._latest_plan_event_id()

        args = argparse.Namespace(
            db_path=str(self.db_path),
            run=None,
            plan=plan_id,
            limit=memory_explain_cli.DEFAULT_EXPLAIN_LIMIT,
            json=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            memory_explain_cli.run_memory_explain(args)
        payload = json.loads(buffer.getvalue())

        included = {item["key"]: item for item in payload["included"]}
        excluded = {item["key"]: item for item in payload["excluded"]}
        self.assertIn("alpha", included)
        self.assertEqual(included["alpha"]["reasons"][0]["code"], "include.profile")
        self.assertIn("beta", excluded)
        self.assertEqual(excluded["beta"]["reasons"][0]["code"], "exclude.kind")
        self.assertIn("gamma", excluded)
        self.assertEqual(excluded["gamma"]["reasons"][0]["code"], "exclude.profile")

    def test_explain_json_schema_stability(self) -> None:
        self._seed_item(namespace="global", key="alpha", kind="fact")
        self._run_ask("--memory")
        plan_id = self._latest_plan_event_id()

        args = argparse.Namespace(
            db_path=str(self.db_path),
            run=None,
            plan=plan_id,
            limit=memory_explain_cli.DEFAULT_EXPLAIN_LIMIT,
            json=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            memory_explain_cli.run_memory_explain(args)
        payload = json.loads(buffer.getvalue())

        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("counts", payload)
        self.assertIn("included", payload)
        self.assertIn("excluded", payload)
        self.assertIn("limit", payload)
        self.assertIn("agent_role", payload)
        self.assertEqual(payload["plan_id"], plan_id)
        self.assertIsNone(payload["run_id"])
        if payload["included"]:
            sample = payload["included"][0]
            for key in (
                "trace_id",
                "run_id",
                "plan_id",
                "key",
                "namespace",
                "kind",
                "decision",
                "reasons",
                "created_at",
            ):
                self.assertIn(key, sample)

    def test_selection_trace_cap(self) -> None:
        for index in range(MEMORY_SELECTION_TRACE_CAP + 60):
            self._seed_item(
                namespace="global",
                key=f"item-{index}",
                kind="fact",
            )
        selected = memory_list_prompt_items(str(self.db_path), limit=20)
        record_prompt_selection_trace(
            str(self.db_path),
            selected_items=selected,
            run_id=None,
            plan_id="plan-cap-test",
            trace_limit=MEMORY_SELECTION_TRACE_CAP + 50,
        )
        traces = list_selection_traces(
            str(self.db_path),
            run_id=None,
            plan_id="plan-cap-test",
            limit=MEMORY_SELECTION_TRACE_CAP + 200,
        )
        self.assertEqual(len(traces), MEMORY_SELECTION_TRACE_CAP)

    def test_memory_explain_releases_db_handle(self) -> None:
        plan_id = "plan-handle-test"
        state_store = StateStore(str(self.db_path))
        try:
            state_store.record_event(
                actor="test",
                event_type=EVENT_TYPE_LLM_PLAN,
                message="Plan",
                json_payload={},
                event_id=plan_id,
            )
        finally:
            state_store.close()

        args = [
            "--db",
            str(self.db_path),
            "memory",
            "explain",
            "--plan",
            plan_id,
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with mock.patch.object(sys, "argv", ["gismo", *args]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(io.StringIO()):
                        cli_main.main()
            gc.collect()
            try:
                os.remove(self.db_path)
            except OSError as exc:
                self.fail(f"Expected DB path to be deletable, got error: {exc}")
            self.assertFalse(self.db_path.exists())
