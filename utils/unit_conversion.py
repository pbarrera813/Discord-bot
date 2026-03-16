from __future__ import annotations

import unicodedata
from typing import Any


UNIT_ALIASES: dict[str, str] = {
    # Length
    "m": "meter",
    "meter": "meter",
    "meters": "meter",
    "metro": "meter",
    "metros": "meter",
    "km": "kilometer",
    "kilometer": "kilometer",
    "kilometers": "kilometer",
    "kilometro": "kilometer",
    "kilometros": "kilometer",
    "cm": "centimeter",
    "centimeter": "centimeter",
    "centimeters": "centimeter",
    "centimetro": "centimeter",
    "centimetros": "centimeter",
    "mm": "millimeter",
    "millimeter": "millimeter",
    "millimeters": "millimeter",
    "milla": "mile",
    "millas": "mile",
    "mile": "mile",
    "miles": "mile",
    "in": "inch",
    "inch": "inch",
    "inches": "inch",
    "pulgada": "inch",
    "pulgadas": "inch",
    "ft": "foot",
    "foot": "foot",
    "feet": "foot",
    "pie": "foot",
    "pies": "foot",
    "yd": "yard",
    "yard": "yard",
    "yards": "yard",
    # Weight
    "g": "gram",
    "gram": "gram",
    "grams": "gram",
    "gramo": "gram",
    "gramos": "gram",
    "kg": "kilogram",
    "kilogram": "kilogram",
    "kilograms": "kilogram",
    "kilogramo": "kilogram",
    "kilogramos": "kilogram",
    "lb": "pound",
    "lbs": "pound",
    "pound": "pound",
    "pounds": "pound",
    "libra": "pound",
    "libras": "pound",
    "oz": "ounce",
    "ounce": "ounce",
    "ounces": "ounce",
    "onza": "ounce",
    "onzas": "ounce",
    # Volume
    "l": "liter",
    "liter": "liter",
    "liters": "liter",
    "litro": "liter",
    "litros": "liter",
    "ml": "milliliter",
    "milliliter": "milliliter",
    "milliliters": "milliliter",
    "mililitro": "milliliter",
    "mililitros": "milliliter",
    "gal": "gallon",
    "gallon": "gallon",
    "gallons": "gallon",
    "galon": "gallon",
    "galones": "gallon",
    # Temperature
    "c": "celsius",
    "celsius": "celsius",
    "centigrade": "celsius",
    "centigrados": "celsius",
    "f": "fahrenheit",
    "fahrenheit": "fahrenheit",
    "k": "kelvin",
    "kelvin": "kelvin",
    # Time
    "s": "second",
    "sec": "second",
    "second": "second",
    "seconds": "second",
    "segundo": "second",
    "segundos": "second",
    "min": "minute",
    "minute": "minute",
    "minutes": "minute",
    "minuto": "minute",
    "minutos": "minute",
    "h": "hour",
    "hr": "hour",
    "hour": "hour",
    "hours": "hour",
    "hora": "hour",
    "horas": "hour",
    "d": "day",
    "day": "day",
    "days": "day",
    "dia": "day",
    "dias": "day",
}


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


def normalize_unit_name(raw_unit: str) -> str:
    unit = _strip_accents(raw_unit.strip().lower())
    unit = unit.replace("-", " ").replace("/", " per ")
    unit = " ".join(unit.split())
    unit = unit.replace(" per ", "_per_").replace(" ", "_")
    return unit


def normalize_requested_unit(raw_unit: str) -> str:
    unit = normalize_unit_name(raw_unit)
    if unit.startswith("to_"):
        unit = unit[3:]
    if unit.startswith("a_"):
        unit = unit[2:]
    return UNIT_ALIASES.get(unit, unit)


def cleanup_target_unit_input(raw_target: str) -> str:
    target = raw_target.strip()
    lowered = _strip_accents(target.lower()).strip()
    if lowered.startswith("to "):
        return target[3:].strip()
    if lowered.startswith("a "):
        return target[2:].strip()
    return target


def _to_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        compact = value.replace(",", "").strip()
        try:
            return float(compact)
        except ValueError:
            return None
    return None


def extract_target_conversion(data: Any, target_unit: str) -> tuple[float | None, str | None]:
    target = normalize_requested_unit(target_unit)

    if isinstance(data, dict):
        conversions = data.get("conversions")
        if isinstance(conversions, (dict, list)):
            converted, matched = extract_target_conversion(conversions, target)
            if converted is not None:
                return converted, matched

        legacy_value = None
        for value_key in ("new_value", "converted_value", "result", "value"):
            legacy_value = _to_number(data.get(value_key))
            if legacy_value is not None:
                break
        if legacy_value is not None:
            for unit_key in ("to_unit", "new_unit", "target_unit", "to", "name", "unit"):
                unit_value = data.get(unit_key)
                if isinstance(unit_value, str) and normalize_requested_unit(unit_value) == target:
                    return legacy_value, unit_value

        direct = data.get(target)
        if direct is not None:
            direct_num = _to_number(direct)
            if direct_num is not None:
                return direct_num, target

        for key, value in data.items():
            if normalize_requested_unit(str(key)) == target:
                num = _to_number(value)
                if num is not None:
                    return num, str(key)

        dict_candidates: list[dict[str, Any]] = []
        list_candidates: list[list[Any]] = []
        for value in data.values():
            if isinstance(value, dict):
                dict_candidates.append(value)
            elif isinstance(value, list):
                list_candidates.append(value)
        if dict_candidates:
            converted, matched = extract_target_conversion(dict_candidates, target)
            if converted is not None:
                return converted, matched
        for candidate in list_candidates:
            converted, matched = extract_target_conversion(candidate, target)
            if converted is not None:
                return converted, matched

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue

            names: list[str] = []
            for key in ("unit", "name", "to", "target_unit", "abbreviation", "symbol"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    names.append(val.strip())

            if any(normalize_requested_unit(name) == target for name in names):
                for value_key in ("value", "result", "converted", "converted_value", "new_value"):
                    num = _to_number(item.get(value_key))
                    if num is not None:
                        return num, names[0] if names else target

    return None, None


def collect_conversion_units(data: Any) -> list[str]:
    units: set[str] = set()

    if isinstance(data, dict):
        conversions = data.get("conversions")
        if isinstance(conversions, dict):
            for key in conversions.keys():
                normalized = normalize_requested_unit(str(key))
                if normalized:
                    units.add(normalized)
        elif isinstance(conversions, list):
            units.update(collect_conversion_units(conversions))

        for key, value in data.items():
            if key in {"conversions"}:
                continue
            if isinstance(value, dict) or isinstance(value, list):
                units.update(collect_conversion_units(value))
            elif isinstance(value, (int, float, str)):
                normalized_key = normalize_requested_unit(str(key))
                if normalized_key and normalized_key not in {"type", "unit", "amount", "value"}:
                    units.add(normalized_key)

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            for key in ("unit", "name", "to", "target_unit", "abbreviation", "symbol", "to_unit", "new_unit"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    units.add(normalize_requested_unit(val))

    return sorted(unit for unit in units if unit)
