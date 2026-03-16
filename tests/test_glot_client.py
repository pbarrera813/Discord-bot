from __future__ import annotations

import unittest

from services.glot import GlotClient


class _DummyGlotClient(GlotClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, str, dict]] = []

    async def _request(self, *, method: str, path: str, json_payload=None):  # type: ignore[override]
        self.calls.append((method, path, json_payload or {}))
        return {"stdout": "ok", "stderr": "", "error": ""}


class _FallbackGlotClient(GlotClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def _request(self, *, method: str, path: str, json_payload=None):  # type: ignore[override]
        from services.glot import GlotRequestError

        self.calls.append(path)
        if path in {"/run/python", "/run/python/"}:
            raise GlotRequestError(status=405, message="Method Not Supported", path=path)
        if path in {"/run", "/run/"}:
            return {"stdout": "ok", "stderr": "", "error": ""}
        raise GlotRequestError(status=404, message="Not Found", path=path)


class GlotClientModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_api_mode_uses_languages_route(self) -> None:
        client = _DummyGlotClient("token", mode="run_api")
        await client.run_code(language="python", code="print(1)", filename="main.py")
        self.assertEqual(len(client.calls), 1)
        method, path, payload = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/languages/python/latest")
        self.assertEqual(payload["files"][0]["name"], "main.py")

    async def test_docker_run_mode_uses_run_route(self) -> None:
        client = _DummyGlotClient("token", mode="docker_run")
        await client.run_code(language="python", code="print(1)", filename="main.py")
        self.assertEqual(len(client.calls), 1)
        method, path, payload = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/run")
        self.assertEqual(payload["language"], "python")
        self.assertEqual(payload["files"][0]["name"], "main.py")

    async def test_glot_io_mode_uses_run_language_route(self) -> None:
        client = _DummyGlotClient("token", base_url="https://glot.io/api", mode="glot_io")
        await client.run_code(language="python", code="print(1)", filename="main.py")
        self.assertEqual(len(client.calls), 1)
        method, path, payload = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/run/python")
        self.assertNotIn("language", payload)

    async def test_auto_mode_detects_glot_io(self) -> None:
        client = _DummyGlotClient("token", base_url="https://glot.io/api", mode="auto")
        await client.run_code(language="python", code="print(1)", filename="main.py")
        self.assertEqual(len(client.calls), 1)
        _, path, _ = client.calls[0]
        self.assertEqual(path, "/run/python")

    async def test_glot_io_fallbacks_from_run_language_to_run(self) -> None:
        client = _FallbackGlotClient("token", base_url="https://glot.io/api", mode="glot_io")
        result = await client.run_code(language="python", code="print(1)", filename="main.py")
        self.assertEqual(result.get("stdout"), "ok")
        self.assertEqual(client.calls[0], "/run/python")
        self.assertEqual(client.calls[1], "/run/python/")
        self.assertEqual(client.calls[2], "/run")


if __name__ == "__main__":
    unittest.main()
