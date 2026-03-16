from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
