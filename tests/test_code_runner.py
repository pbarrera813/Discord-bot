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
        language, code, filename = glot_client.called_with
        self.assertEqual(language, "python")
        self.assertEqual(code, "print('ok')")
        self.assertEqual(filename, "main.py")


if __name__ == "__main__":
    unittest.main()
