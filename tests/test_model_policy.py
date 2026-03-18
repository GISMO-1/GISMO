from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from gismo.core.state import StateStore
from gismo.llm import model_policy


def _make_db(tmp: Path) -> str:
    db_path = str(tmp / "state.db")
    StateStore(db_path).close()
    return db_path


class ModelPolicyTest(unittest.TestCase):
    def test_defaults_use_canonical_gismo_identity(self) -> None:
        tmp = Path("tmp") / f"model-policy-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(tmp)
            policy = model_policy.load_model_policy(db)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(policy.primary_assistant_model, "gismo:latest")
        self.assertEqual(policy.planner_model, "gismo:latest")
        self.assertEqual(policy.helper_model, "")
        self.assertFalse(policy.allow_identity_fallback)

    def test_save_policy_rejects_missing_model(self) -> None:
        tmp = Path("tmp") / f"model-policy-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(tmp)
            with mock.patch.object(
                model_policy,
                "discover_models",
                return_value={"installed_models": ["gismo:latest"], "loaded_models": [], "ollama_available": True},
            ):
                with self.assertRaisesRegex(ValueError, "Model is not installed"):
                    model_policy.save_model_policy(db, primary_assistant_model="tinyllama")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_identity_route_degrades_without_explicit_fallback(self) -> None:
        tmp = Path("tmp") / f"model-policy-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(tmp)
            with mock.patch.object(
                model_policy,
                "discover_models",
                return_value={"installed_models": ["tinyllama"], "loaded_models": [], "ollama_available": True},
            ):
                route = model_policy.resolve_model_route(db, purpose="assistant_reply")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertTrue(route.degraded)
        self.assertIsNone(route.selected_model)
        self.assertEqual(route.candidate_models, [])

    def test_identity_route_can_use_explicit_helper_fallback(self) -> None:
        tmp = Path("tmp") / f"model-policy-{uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=False)
        try:
            db = _make_db(tmp)
            with mock.patch.object(
                model_policy,
                "discover_models",
                return_value={"installed_models": ["tinyllama"], "loaded_models": [], "ollama_available": True},
            ):
                model_policy.save_model_policy(
                    db,
                    helper_model="tinyllama",
                    allow_identity_fallback=True,
                )
                route = model_policy.resolve_model_route(db, purpose="assistant_reply")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertFalse(route.degraded)
        self.assertEqual(route.selected_model, "tinyllama")
        self.assertEqual(route.candidate_models, ["tinyllama"])
