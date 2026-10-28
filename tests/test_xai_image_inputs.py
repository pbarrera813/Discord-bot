import base64
import json
import unittest

from services.xai_client import XAIClient


class _Response:
    status = 200

    def __init__(self, body: bytes = b"edited") -> None:
        self._text = json.dumps({"data": [{"b64_json": base64.b64encode(body).decode("ascii")}]})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self) -> str:
        return self._text


class _Session:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return _Response()


class _Client(XAIClient):
    def __init__(self, session: _Session) -> None:
        super().__init__("key", "grok-test", image_model="grok-imagine-image-quality")
        self.session = session

    async def _get_session(self):
        return self.session


class XAIImageInputTests(unittest.IsolatedAsyncioTestCase):

    async def test_invalid_inline_artifacts_fail_before_provider(self) -> None:
        invalid = (
            "data:image/svg+xml;base64,AAAA",
            "data:image/png;base64,not base64",
            "data:image/png;base64,",
        )
        for source in invalid:
            session = _Session()
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                await _Client(session).edit_image("edit", source)
            self.assertEqual(session.calls, [])

    async def test_oversize_inline_artifact_fails_before_provider(self) -> None:
        encoded = base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode("ascii")
        session = _Session()

        with self.assertRaises(RuntimeError):
            await _Client(session).edit_image("edit", "data:image/png;base64," + encoded)

        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
