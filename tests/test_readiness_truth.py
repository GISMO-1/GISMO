from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from gismo.core import readiness


def _status(
    *,
    daemon: dict | None = None,
    queue: dict | None = None,
    approvals: dict | None = None,
    onboarding: dict | None = None,
    models: dict | None = None,
    startup: dict | None = None,
    database: dict | None = None,
    starting_is_stale: bool = False,
) -> dict:
    return readiness._build_gismo_status(
        daemon=daemon
        or {
            "available": True,
            "running": True,
            "stale": False,
            "paused": False,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        },
        queue=queue
        or {"running": 0, "failed": 0, "stale_running": 0},
        approvals=approvals
        or {"pending_count": 0, "detail": "No approvals are waiting."},
        onboarding=onboarding
        or {"needs_onboarding": False},
        models=models
        or {"state": "ready", "required": True, "blocking": False, "detail": "Ready"},
        startup=startup
        or {"recent_failure": False, "recent_start": False},
        database=database or {"ready": True, "state": "ready"},
        starting_is_stale=starting_is_stale,
    )


class TestReadinessTruth(unittest.TestCase):
    @staticmethod
    def _model_policy(*, helper_model: str = "") -> SimpleNamespace:
        values = {
            "primary_assistant_model": "main-model",
            "planner_model": "main-model",
            "helper_model": helper_model,
            "allow_identity_fallback": False,
            "performance_mode": "auto",
        }
        return SimpleNamespace(**values, to_dict=lambda: dict(values))

    def test_healthy_required_components_are_ready(self) -> None:
        result = _status()

        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["ready"])
        self.assertTrue(result["surface_ready"])

    def test_recent_worker_start_is_starting_not_ready(self) -> None:
        result = _status(
            daemon={
                "available": True,
                "running": False,
                "stale": False,
                "paused": False,
                "last_seen": None,
            },
            startup={"recent_failure": False, "recent_start": True},
        )

        self.assertEqual(result["state"], "starting")
        self.assertFalse(result["ready"])
        self.assertFalse(result["surface_ready"])

    def test_stale_worker_is_degraded_not_ready(self) -> None:
        result = _status(
            daemon={
                "available": True,
                "running": True,
                "stale": True,
                "paused": False,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
        )

        self.assertEqual(result["state"], "degraded")
        self.assertFalse(result["ready"])
        self.assertFalse(result["surface_ready"])

    def test_unavailable_worker_status_is_offline(self) -> None:
        result = _status(
            daemon={
                "available": False,
                "running": False,
                "stale": False,
                "paused": False,
                "last_seen": None,
            }
        )

        self.assertEqual(result["state"], "offline")
        self.assertFalse(result["ready"])
        self.assertFalse(result["surface_ready"])

    def test_state_failure_blocks_readiness(self) -> None:
        result = _status(database={"ready": False, "state": "blocked"})

        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["ready"])
        self.assertFalse(result["surface_ready"])

    def test_approval_state_keeps_healthy_surface_available(self) -> None:
        result = _status(
            approvals={"pending_count": 1, "detail": "1 request needs approval."}
        )

        self.assertEqual(result["state"], "approval_needed")
        self.assertFalse(result["ready"])
        self.assertTrue(result["surface_ready"])

    def test_required_model_failure_blocks_surface(self) -> None:
        result = _status(
            models={
                "state": "offline",
                "required": True,
                "blocking": True,
                "detail": "Model service unavailable",
            }
        )

        self.assertEqual(result["state"], "degraded")
        self.assertFalse(result["ready"])
        self.assertFalse(result["surface_ready"])

    def test_optional_model_failure_degrades_without_blocking_surface(self) -> None:
        result = _status(
            models={
                "state": "degraded",
                "required": False,
                "blocking": False,
                "detail": "Optional model is unavailable",
            }
        )

        self.assertEqual(result["state"], "degraded")
        self.assertFalse(result["ready"])
        self.assertTrue(result["surface_ready"])

    def test_missing_optional_helper_model_does_not_block(self) -> None:
        with mock.patch.object(
            readiness,
            "load_model_policy",
            return_value=self._model_policy(helper_model="helper-model"),
        ):
            result = readiness._build_cached_model_status(
                "state.db",
                {
                    "ollama_available": True,
                    "installed_models": ["main-model"],
                    "loaded_models": [],
                },
            )

        self.assertEqual(result["state"], "degraded")
        self.assertTrue(result["required"])
        self.assertFalse(result["blocking"])

    def test_missing_required_primary_model_blocks(self) -> None:
        with mock.patch.object(
            readiness,
            "load_model_policy",
            return_value=self._model_policy(),
        ):
            result = readiness._build_cached_model_status(
                "state.db",
                {
                    "ollama_available": True,
                    "installed_models": [],
                    "loaded_models": [],
                },
            )

        self.assertEqual(result["state"], "degraded")
        self.assertTrue(result["required"])
        self.assertTrue(result["blocking"])

    def test_runtime_status_reports_state_and_worker_access_failures(self) -> None:
        with self.assertLogs(readiness.LOGGER, level="ERROR"), mock.patch.object(
            readiness,
            "StateStore",
            side_effect=RuntimeError("state unavailable"),
        ), mock.patch.object(
            readiness,
            "get_background_worker_status",
            side_effect=RuntimeError("worker unavailable"),
        ), mock.patch.object(
            readiness,
            "_build_model_status",
            return_value={
                "state": "unknown",
                "detail": "Checking availability",
                "required": False,
                "blocking": False,
                "health": None,
            },
        ), mock.patch.object(
            readiness,
            "_build_onboarding_status",
            return_value={
                "completed": False,
                "needs_onboarding": False,
                "operator_name": None,
                "state": "blocked",
                "detail": "Setup state is unavailable",
            },
        ):
            result = readiness.build_runtime_status("unavailable.db")

        self.assertFalse(result["database"]["ready"])
        self.assertFalse(result["api"]["ready"])
        self.assertFalse(result["daemon"]["available"])
        self.assertEqual(result["gismo"]["state"], "blocked")
        self.assertFalse(result["readiness"]["ready"])
        self.assertFalse(result["readiness"]["surface_ready"])

    def test_startup_event_marks_only_recent_start_as_starting(self) -> None:
        event = SimpleNamespace(
            event_type="daemon_autostart_started",
            ts=datetime.now(timezone.utc),
            message="Background service started.",
            json_payload={},
        )

        result = readiness._build_startup_status(
            [event], now=datetime.now(timezone.utc)
        )

        self.assertTrue(result["recent_start"])
        self.assertFalse(result["recent_failure"])


if __name__ == "__main__":
    unittest.main()
