from __future__ import annotations

import base64
import unittest

from cogs.minecraft import MinecraftCog


class MinecraftIconTests(unittest.TestCase):
    def test_extract_inline_icon_bytes(self) -> None:
        raw = b"png-data"
        encoded = base64.b64encode(raw).decode("ascii")
        payload = {"icon": f"data:image/png;base64,{encoded}"}
        extracted = MinecraftCog._extract_inline_icon_bytes(payload)
        self.assertEqual(extracted, raw)

    def test_extract_inline_icon_none_on_invalid(self) -> None:
        payload = {"icon": "invalid"}
        extracted = MinecraftCog._extract_inline_icon_bytes(payload)
        self.assertIsNone(extracted)


if __name__ == "__main__":
    unittest.main()
