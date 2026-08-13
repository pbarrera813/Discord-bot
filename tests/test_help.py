from __future__ import annotations

import ast
import pathlib
import unittest
from types import SimpleNamespace

from cogs.admin import AdminCog, HELP_CAPABILITIES, HELP_COMMANDS, HELP_INTENTIONAL_EXCLUSIONS
from services.voice_messages import ALLOWED_TTS_TAGS


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _literal_keyword(node: ast.Call, name: str) -> str | None:
    for keyword in node.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _literal_aliases(node: ast.Call) -> set[str]:
    for keyword in node.keywords:
        if keyword.arg != "aliases" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
            continue
        return {
            item.value
            for item in keyword.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return set()


def _runtime_command_surface() -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    aliases: set[str] = set()

    for path in sorted((ROOT / "cogs").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for class_node in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            groups: dict[str, str] = {}
            functions = [node for node in class_node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for func in functions:
                for decorator in func.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = _call_name(decorator)
                    if name.endswith("hybrid_group") or name.endswith("commands.group"):
                        command_name = _literal_keyword(decorator, "name") or func.name
                        groups[func.name] = command_name
                        paths.add(command_name)
                        fallback = _literal_keyword(decorator, "fallback")
                        if fallback:
                            paths.add(f"{command_name} {fallback}")

            for func in functions:
                for decorator in func.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = _call_name(decorator)
                    command_name = _literal_keyword(decorator, "name") or func.name
                    if name.endswith("hybrid_command") or name.endswith("commands.command"):
                        paths.add(command_name)
                        aliases.update(_literal_aliases(decorator))
                        continue
                    if name.endswith(".command"):
                        group_ref = name.rsplit(".", 1)[0]
                        group_name = groups.get(group_ref)
                        if group_name:
                            paths.add(f"{group_name} {command_name}")

    return paths, aliases


def _help_text(pages) -> str:  # noqa: ANN001, ANN202
    parts: list[str] = []
    for embed in pages:
        parts.append(str(embed.title or ""))
        parts.append(str(embed.description or ""))
        for field in embed.fields:
            parts.append(str(field.name))
            parts.append(str(field.value))
    return "\n".join(parts)


class HelpCoverageTests(unittest.TestCase):
    def test_registered_user_facing_commands_are_documented_or_excluded(self) -> None:
        runtime_paths, runtime_aliases = _runtime_command_surface()
        documented = AdminCog.documented_help_command_paths()
        excluded = set(HELP_INTENTIONAL_EXCLUSIONS)

        missing = sorted(runtime_paths - documented - excluded)
        self.assertEqual(missing, [])

        undocumented_aliases = sorted(runtime_aliases - AdminCog.documented_help_aliases() - excluded)
        self.assertEqual(undocumented_aliases, [])

    def test_help_registry_does_not_advertise_nonexistent_commands(self) -> None:
        runtime_paths, runtime_aliases = _runtime_command_surface()
        runtime = runtime_paths | runtime_aliases
        extra = sorted(AdminCog.documented_help_command_paths() - runtime - set(HELP_INTENTIONAL_EXCLUSIONS))
        self.assertEqual(extra, [])

    def test_say_voice_contract_is_documented(self) -> None:
        say = next(item for item in HELP_COMMANDS if item.path == "say")
        self.assertIn("mensaje", say.material_options)
        self.assertIn("modo", say.material_options)
        self.assertIn("modo=text|voice", say.material_choices)
        self.assertEqual(say.access, "manage_messages")
        self.assertTrue(say.show_to_all)

        pages = AdminCog.__new__(AdminCog)._build_help_pages("en", member=None)
        text = _help_text(pages)
        self.assertIn("/say mensaje:<text> modo:<text|voice>", text)
        self.assertIn("Requires Manage Messages", text)

    def test_non_command_capabilities_are_documented(self) -> None:
        keys = {item.key for item in HELP_CAPABILITIES}
        self.assertTrue(
            {
                "ai_conversation",
                "voice_conversation",
                "voice_tts",
                "voice_tags",
                "football_ai",
                "football_watch",
                "image_context",
                "web_lookup",
            }.issubset(keys)
        )

        pages = AdminCog.__new__(AdminCog)._build_help_pages("en", member=None)
        text = _help_text(pages)
        self.assertIn("one response only", text)
        self.assertIn("native Discord voice messages", text)
        self.assertIn("Iris with es-MX", text)
        for tag in ALLOWED_TTS_TAGS:
            self.assertIn(f"[{tag}]", text)

    def test_help_does_not_advertise_removed_command_paths(self) -> None:
        admin_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=True,
                manage_guild=True,
                manage_messages=True,
                manage_channels=True,
                manage_roles=True,
                moderate_members=True,
                kick_members=True,
                ban_members=True,
                manage_nicknames=True,
            )
        )
        pages = AdminCog.__new__(AdminCog)._build_help_pages("en", member=admin_member)
        text = _help_text(pages)
        self.assertNotIn("/utility say", text)
        self.assertNotIn("/config modlog", text)
        self.assertNotIn("/config prefix", text)
        self.assertNotIn("/config language", text)
        self.assertIn("/setmodlog", text)
        self.assertIn("/setprefix", text)
        self.assertIn("/language <en|es>", text)

    def test_permission_sensitive_pages_are_not_free_for_normal_users(self) -> None:
        normal_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_messages=False,
                manage_channels=False,
                manage_roles=False,
                moderate_members=False,
                kick_members=False,
                ban_members=False,
                manage_nicknames=False,
            )
        )
        text = _help_text(AdminCog.__new__(AdminCog)._build_help_pages("en", member=normal_member))
        self.assertNotIn("/user ban", text)
        self.assertNotIn("/aichannel add", text)
        self.assertIn("/say mensaje:<text> modo:<text|voice>", text)
        self.assertIn("Requires Manage Messages", text)

    def test_touched_spanish_help_strings_are_valid_unicode(self) -> None:
        pages = AdminCog.__new__(AdminCog)._build_help_pages("es", member=None)
        text = _help_text(pages)
        self.assertIn("Cumpleaños", text)
        self.assertIn("Próximos", text)
        self.assertIn("configuración", text.casefold())
        for marker in ("Ã", "Â", "â€", "ï¿½"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
