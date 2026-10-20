from __future__ import annotations

import unittest

from utils.unit_conversion import collect_conversion_units, extract_target_conversion, normalize_requested_unit


class UnitConversionTests(unittest.TestCase):
    def test_spanish_aliases(self) -> None:
        self.assertEqual(normalize_requested_unit("metros"), "meter")
        self.assertEqual(normalize_requested_unit("kilometros"), "kilometer")
        self.assertEqual(normalize_requested_unit("libras"), "pound")


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
        value, unit_name = extract_target_conversion(data, "kilometer")
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
