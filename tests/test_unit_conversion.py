from __future__ import annotations

import unittest

from utils.unit_conversion import (
    collect_conversion_units,
    cleanup_target_unit_input,
    extract_target_conversion,
    normalize_requested_unit,
)


class UnitConversionTests(unittest.TestCase):
    def test_spanish_aliases(self) -> None:
        self.assertEqual(normalize_requested_unit("metros"), "meter")
        self.assertEqual(normalize_requested_unit("kilometros"), "kilometer")
        self.assertEqual(normalize_requested_unit("libras"), "pound")

    def test_cleanup_target_prefix(self) -> None:
        self.assertEqual(cleanup_target_unit_input("to kilometer"), "kilometer")
        self.assertEqual(cleanup_target_unit_input("a kilometros"), "kilometros")

    def test_extract_target_from_dict(self) -> None:
        data = {"kilometer": 1.0, "mile": 0.621371}
        value, unit_name = extract_target_conversion(data, "kilometros")
        self.assertEqual(value, 1.0)
        self.assertEqual(unit_name, "kilometer")

    def test_extract_target_from_list(self) -> None:
        data = [
            {"unit": "meter", "value": 1000},
            {"unit": "kilometer", "value": 1},
        ]
        value, unit_name = extract_target_conversion(data, "kilometer")
        self.assertEqual(value, 1.0)
        self.assertEqual(unit_name, "kilometer")

    def test_extract_target_from_conversions_payload(self) -> None:
        data = {
            "type": "length",
            "unit": "meter",
            "amount": 1000,
            "conversions": {
                "millimeter": 1000000.0,
                "centimeter": 100000.0,
                "kilometer": 1.0,
            },
        }
        value, unit_name = extract_target_conversion(data, "kilometros")
        self.assertEqual(value, 1.0)
        self.assertEqual(unit_name, "kilometer")

    def test_collect_conversion_units(self) -> None:
        data = {
            "type": "length",
            "unit": "meter",
            "amount": 3,
            "conversions": {"kilometer": 0.003, "mile": 0.001864},
        }
        units = collect_conversion_units(data)
        self.assertIn("kilometer", units)
        self.assertIn("mile", units)


if __name__ == "__main__":
    unittest.main()
