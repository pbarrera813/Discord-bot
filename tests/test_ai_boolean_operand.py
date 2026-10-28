from __future__ import annotations

import unittest
from unittest.mock import patch

from services.ai_operation_catalog import OPERAND_DESCRIPTORS, OperandDescriptor, validate_operands


def _find(schema: object, key: str) -> object | None:
    if isinstance(schema, dict):
        if key in schema:
            return schema[key]
        for value in schema.values():
            found = _find(value, key)
            if found is not None:
                return found
    elif isinstance(schema, list):
        for value in schema:
            found = _find(value, key)
            if found is not None:
                return found
    return None


class BooleanOperandValidationTests(unittest.TestCase):
    def test_only_a_real_boolean_or_null_is_accepted(self) -> None:
        for value in (True, False, None):
            with self.subTest(value=value):
                self.assertIsNone(validate_operands("BIRTHDAY", {"birthday_action": "set_ages", "birthday_ages": value}))
        for value in (1, 0, "true", "false", [], {}, 1.0):
            with self.subTest(value=value):
                self.assertEqual(
                    validate_operands("BIRTHDAY", {"birthday_action": "set_ages", "birthday_ages": value}),
                    "task_operand_type_or_bounds_invalid",
                )

    def test_a_non_nullable_boolean_rejects_null(self) -> None:
        with patch.dict(OPERAND_DESCRIPTORS["BIRTHDAY"], {"birthday_ages": OperandDescriptor("boolean", nullable=False)}):
            self.assertEqual(
                validate_operands("BIRTHDAY", {"birthday_action": "set_ages", "birthday_ages": None}),
                "task_operand_type_or_bounds_invalid",
            )
            self.assertIsNone(validate_operands("BIRTHDAY", {"birthday_action": "set_ages", "birthday_ages": False}))


if __name__ == "__main__":
    unittest.main()
