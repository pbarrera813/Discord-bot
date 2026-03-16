from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.code_runner import CodeRunnerCog


class CodeRunnerHelperTests(unittest.TestCase):
    def test_parse_code_fence_detects_language(self) -> None:
        raw = "```python\nprint('ok')\n```"
        code, detected = CodeRunnerCog._parse_code_input(raw)
        self.assertEqual(code, "print('ok')")
        self.assertEqual(detected, "python")

    def test_parse_code_fence_without_language_detects_none(self) -> None:
        raw = "```\nline1\nline2\n```"
        code, detected = CodeRunnerCog._parse_code_input(raw)
        self.assertEqual(code, "line1\nline2")
        self.assertIsNone(detected)

    def test_parse_single_line_code_fence_detects_language(self) -> None:
        raw = "```python print('ok')```"
        code, detected = CodeRunnerCog._parse_code_input(raw)
        self.assertEqual(code, "print('ok')")
        self.assertEqual(detected, "python")

    def test_strip_code_fence_with_language(self) -> None:
        raw = "```python\nprint('ok')\n```"
        self.assertEqual(CodeRunnerCog._strip_code_fence(raw), "print('ok')")

    def test_strip_code_fence_without_language(self) -> None:
        raw = "```\nline1\nline2\n```"
        self.assertEqual(CodeRunnerCog._strip_code_fence(raw), "line1\nline2")

    def test_resolve_language_alias(self) -> None:
        resolved = CodeRunnerCog._resolve_language("py")
        self.assertEqual(resolved, ("python", "main.py"))

    def test_resolve_language_aliases_from_fenced_labels(self) -> None:
        cases = {
            "c": "c",
            "cpp": "cpp",
            "c++": "cpp",
            "c++23": "cpp",
            "c#": "csharp",
            "cs": "csharp",
            "java": "java",
            "javascript": "javascript",
            "js": "javascript",
            "python": "python",
            "py": "python",
            "rust": "rust",
            "rs": "rust",
        }
        for lang_tag, expected in cases.items():
            with self.subTest(lang_tag=lang_tag):
                raw = f"```{lang_tag}\ncode\n```"
                _code, detected = CodeRunnerCog._parse_code_input(raw)
                self.assertIsNotNone(detected)
                resolved = CodeRunnerCog._resolve_language(detected or "")
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved[0], expected)

    def test_resolve_language_fallback(self) -> None:
        resolved = CodeRunnerCog._resolve_language("elixir")
        self.assertIsNone(resolved)

    def test_resolve_from_extension(self) -> None:
        resolved = CodeRunnerCog._resolve_from_extension("program.cpp")
        self.assertEqual(resolved, ("cpp", "program.cpp"))

    def test_prefix_normalizes_language_and_code(self) -> None:
        cog = CodeRunnerCog(SimpleNamespace(db=None))
        ctx = SimpleNamespace(
            interaction=None,
            message=SimpleNamespace(content="!code python print('ok')"),
            prefix="!",
            invoked_with="code",
        )
        code, language = cog._normalize_prefix_inputs(
            ctx=ctx,
            code=None,
            language=None,
            has_attachment=False,
        )
        self.assertEqual(language, "python")
        self.assertEqual(code, "print('ok')")

    def test_prefix_detects_language_from_fenced_block_even_if_parser_args_are_split(self) -> None:
        cog = CodeRunnerCog(SimpleNamespace(db=None))
        fenced = "```python\nprint('ok')\n```"
        ctx = SimpleNamespace(
            interaction=None,
            message=SimpleNamespace(content=f"!code {fenced}"),
            prefix="!",
            invoked_with="code",
        )
        # Simulate discord.py parser splitting multiline args incorrectly.
        code, language = cog._normalize_prefix_inputs(
            ctx=ctx,
            code="```python",
            language="print('ok')",
            has_attachment=False,
        )
        self.assertEqual(code, "print('ok')")
        self.assertEqual(language, "python")

    def test_prefix_detects_language_from_multiline_fenced_block_after_newline(self) -> None:
        cog = CodeRunnerCog(SimpleNamespace(db=None))
        fenced = "```cpp\n#include <iostream>\nint main(){std::cout << 1;}\n```"
        ctx = SimpleNamespace(
            interaction=None,
            message=SimpleNamespace(content=f"!code\n{fenced}"),
            prefix="!",
            invoked_with="code",
        )
        code, language = cog._normalize_prefix_inputs(
            ctx=ctx,
            code=None,
            language=None,
            has_attachment=False,
        )
        self.assertEqual(language, "cpp")
        self.assertIn("int main()", code or "")

    def test_cpp_fallback_detection(self) -> None:
        self.assertTrue(
            CodeRunnerCog._needs_cpp_runtime_fallback(
                {"stderr": "sh: line 1: g++: command not found.\nExit code: 127"}
            )
        )
        self.assertFalse(CodeRunnerCog._needs_cpp_runtime_fallback({"stderr": "", "error": ""}))


class _FakeResponse:
    def is_done(self) -> bool:
        return True


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeResponse()


class _FakeGlotClient:
    def __init__(self) -> None:
        self.called_with: tuple[str, str, str] | None = None

    async def run_code(
        self,
        *,
        language: str,
        code: str,
        filename: str,
        command: str | None = None,
    ) -> dict[str, str]:
        self.called_with = (language, code, filename)
        return {"stdout": "ok", "stderr": "", "error": ""}


class _FakeCtx:
    def __init__(self, client: _FakeGlotClient) -> None:
        self.guild = None
        self.interaction = _FakeInteraction()
        self.message = None
        self.author = SimpleNamespace(id=1)
        self._client = client
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace()

    async def defer(self):
        return None


class CodeRunnerSlashTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_detects_language_from_fenced_code_when_language_missing(self) -> None:
        glot_client = _FakeGlotClient()
        bot = SimpleNamespace(db=None, glot_client=glot_client)
        cog = CodeRunnerCog(bot)
        ctx = _FakeCtx(glot_client)
        fenced = "```python\nprint('ok')\n```"

        await cog.code_cmd.callback(cog, ctx, code=fenced, language=None, source_file=None)

        self.assertIsNotNone(glot_client.called_with)
        language, code, filename = glot_client.called_with  # type: ignore[misc]
        self.assertEqual(language, "python")
        self.assertEqual(code, "print('ok')")
        self.assertEqual(filename, "main.py")


if __name__ == "__main__":
    unittest.main()
