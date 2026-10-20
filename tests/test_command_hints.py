from __future__ import annotations

import importlib
import inspect
import pathlib
import unittest

from discord.ext import commands

from services.server_memory import MEMORY_STATUSES, MEMORY_TYPES

COGS_DIR = pathlib.Path(__file__).resolve().parents[1] / "cogs"

FIXED_CHOICES: dict[tuple[str, str], set[str]] = {
    ("language", "language"): {"en", "es"},
    ("birthday mode", "mode"): {"user", "server"},
    ("resetservercontext", "scope"): {"summaries", "memory", "ai_history", "all"},
    ("servercontext reset", "scope"): {"summaries", "memory", "ai_history", "all"},
    ("servercontext list", "status"): set(MEMORY_STATUSES),
    ("servercontext list", "memory_type"): set(MEMORY_TYPES),
    ("servercontext remember", "memory_type"): set(MEMORY_TYPES),
    ("football countries", "filter"): {"name", "code", "search"},
    ("football schedule", "mode"): {"next", "last"},
    ("football top", "category"): {"scorers", "assists", "yellowcards", "redcards"},
}


def _slash_parameters() -> list[tuple[str, str, str, list[str]]]:
    rows: list[tuple[str, str, str, list[str]]] = []
    for path in sorted(COGS_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"cogs.{path.stem}")
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, commands.Cog):
                continue
            stack = [value for value in vars(cls).values() if isinstance(value, commands.Command)]
            while stack:
                command = stack.pop()
                if isinstance(command, commands.Group):
                    stack.extend(command.commands)
                app_command = getattr(command, "app_command", None)
                if app_command is None or not hasattr(app_command, "parameters"):
                    continue
                for parameter in app_command.parameters:
                    rows.append((
                        command.qualified_name,
                        parameter.display_name,
                        parameter.description,
                        [str(choice.value) for choice in (parameter.choices or [])],
                    ))
    return rows


class SlashCommandHintTests(unittest.TestCase):
    def test_every_slash_parameter_has_a_description(self) -> None:
        missing = sorted({f"{command} :: {name}" for command, name, description, _ in _slash_parameters() if description in {"", "…"}})
        self.assertEqual(missing, [])

    def test_fixed_option_parameters_offer_their_options_as_choices(self) -> None:
        rows = {(command, name): choices for command, name, _description, choices in _slash_parameters()}
        for key, expected in FIXED_CHOICES.items():
            with self.subTest(command=key[0], parameter=key[1]):
                self.assertIn(key, rows)
                self.assertEqual(set(rows[key]), expected)


if __name__ == "__main__":
    unittest.main()
