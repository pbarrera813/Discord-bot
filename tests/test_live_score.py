from __future__ import annotations

import unittest

from services.live_score import LiveScoreApiClient


class LiveScoreClientTests(unittest.TestCase):
    def test_legacy_client_is_deprecated(self) -> None:
        with self.assertRaises(RuntimeError):
            LiveScoreApiClient(api_key="x", api_secret="y")


if __name__ == "__main__":
    unittest.main()
