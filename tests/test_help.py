from __future__ import annotations

import ast
import pathlib
import unittest

from cogs.admin import AdminCog, HELP_INTENTIONAL_EXCLUSIONS


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


if __name__ == "__main__":
    unittest.main()
