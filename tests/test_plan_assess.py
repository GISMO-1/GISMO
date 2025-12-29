import unittest

from gismo.core.plan_assess import assess_plan


class PlanAssessTest(unittest.TestCase):
    def test_shell_action_sets_flag_and_lowers_confidence(self) -> None:
        actions = [{"type": "enqueue", "command": "shell: dir"}]
        assessment = assess_plan(actions)
        self.assertIn("shell", assessment.risk_flags)
        self.assertNotEqual(assessment.confidence, "high")

    def test_destructive_token_requires_confirmation(self) -> None:
        actions = [{"type": "enqueue", "command": "shell: rm -rf /"}]
        assessment = assess_plan(actions)
        self.assertEqual(assessment.confidence, "low")
        self.assertTrue(assessment.requires_confirmation)


if __name__ == "__main__":
    unittest.main()
