import unittest

from gismo.core.risk import classify_plan_risk


class PlanRiskTest(unittest.TestCase):
    def test_shell_action_sets_high_risk(self) -> None:
        actions = [{"type": "enqueue", "command": "shell: dir"}]
        risk = classify_plan_risk(actions)
        self.assertEqual(risk.risk_level, "HIGH")
        self.assertIn("shell", risk.risk_flags)

    def test_write_action_sets_high_risk(self) -> None:
        actions = [{"type": "enqueue", "command": "note: record"}]
        risk = classify_plan_risk(actions)
        self.assertEqual(risk.risk_level, "HIGH")
        self.assertIn("writes", risk.risk_flags)

    def test_many_actions_is_medium(self) -> None:
        actions = [
            {"type": "enqueue", "command": "echo: one"},
            {"type": "enqueue", "command": "echo: two"},
            {"type": "enqueue", "command": "echo: three"},
            {"type": "enqueue", "command": "echo: four"},
        ]
        risk = classify_plan_risk(actions)
        self.assertEqual(risk.risk_level, "MEDIUM")
        self.assertIn("many_actions", risk.risk_flags)

    def test_memory_modify_is_medium(self) -> None:
        actions = [
            {"type": "enqueue", "command": "gismo memory put --namespace global --key k"}
        ]
        risk = classify_plan_risk(actions)
        self.assertEqual(risk.risk_level, "MEDIUM")
        self.assertIn("memory_modify", risk.risk_flags)
        self.assertNotIn("writes", risk.risk_flags)


if __name__ == "__main__":
    unittest.main()
