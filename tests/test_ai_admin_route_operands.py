from __future__ import annotations

import unittest

from services.ai_operation_catalog import validate_operands


class IrrelevantNullOperandTests(unittest.TestCase):
    def test_a_null_operand_of_another_family_carries_nothing_and_is_ignored(self) -> None:
        self.assertIsNone(validate_operands("ADMIN", {"admin_action": "user_info", "target_user_candidates": ["x"], "target_language": None}))

    def test_an_unknown_operand_with_a_value_is_still_rejected(self) -> None:
        self.assertEqual(
            validate_operands("ADMIN", {"admin_action": "user_info", "target_language": "japanese"}), "unknown_task_operand"
        )


if __name__ == "__main__":
    unittest.main()
