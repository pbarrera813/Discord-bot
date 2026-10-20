from __future__ import annotations

import itertools
import unittest

from services.memegen import MemeGenClient


class MemeGenClientTests(unittest.TestCase):
    def test_escape_text(self) -> None:
        escaped = MemeGenClient.escape_text("hello world? #1")
        self.assertEqual(escaped, "hello_world~q_~h1")

    def test_build_template_url(self) -> None:
        client = MemeGenClient()
        url = client.build_template_url(
            template_id="drake",
            top_text="yes",
            bottom_text="no",
        )
        self.assertIn("/images/drake/yes/no.png", url)

    def test_build_custom_url(self) -> None:
        client = MemeGenClient()
        url = client.build_custom_url(
            background_url="https://example.com/x.png",
            top_text="hola",
            bottom_text="adios",
        )
        self.assertIn("/images/custom/hola/adios.png", url)
        self.assertIn("background=https%3A%2F%2Fexample.com%2Fx.png", url)

    def test_an_empty_bottom_is_a_blank_line_not_a_literal_underscore(self) -> None:
        client = MemeGenClient()
        for bottom in (None, "", "  "):
            with self.subTest(bottom=bottom):
                self.assertTrue(client.build_template_url(template_id="drake", top_text="hi", bottom_text=bottom).endswith("/hi/_.png"))
                custom = client.build_custom_url(background_url="https://example.com/x.png", top_text="hi", bottom_text=bottom)
                self.assertIn("/images/custom/hi/_.png?", custom)


class _FakeContent:
    def __init__(self, chunks) -> None:
        self._chunks = chunks
        self.yielded = 0

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk


class _FakeResponse:
    def __init__(self, *, status=200, content_type="image/png", chunks=(b"abc",)) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _FakeContent(chunks)


class _FakeGet:
    def __init__(self, resp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    closed = False

    def __init__(self, resp=None) -> None:
        self._resp = resp
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return _FakeGet(self._resp)


class MemeGenFetchImageTests(unittest.IsolatedAsyncioTestCase):
    IMAGE_URL = f"{MemeGenClient.BASE_URL}/images/drake/yes/no.png"
    MAX_BYTES = 8 * 1024 * 1024

    async def test_fetch_image_returns_bytes_and_content_type(self) -> None:
        client = MemeGenClient()
        client._session = _FakeSession(
            _FakeResponse(content_type="IMAGE/PNG; charset=x", chunks=[b"\x89PNG", b"data"])
        )
        data, content_type = await client.fetch_image(self.IMAGE_URL)
        self.assertEqual(data, b"\x89PNGdata")
        self.assertEqual(content_type, "image/png")

    async def test_fetch_image_rejects_foreign_host_without_request(self) -> None:
        client = MemeGenClient()
        session = _FakeSession(_FakeResponse())
        client._session = session
        with self.assertRaisesRegex(ValueError, "api.memegen.link"):
            await client.fetch_image("https://example.com/x.png")
        self.assertEqual(session.urls, [])


    async def test_fetch_image_stops_reading_at_size_cap(self) -> None:
        client = MemeGenClient()
        resp = _FakeResponse(chunks=itertools.repeat(b"x" * 65536))
        client._session = _FakeSession(resp)
        with self.assertRaisesRegex(RuntimeError, "too large"):
            await client.fetch_image(self.IMAGE_URL)
        self.assertLessEqual(resp.content.yielded, self.MAX_BYTES // 65536 + 1)


if __name__ == "__main__":
    unittest.main()
