from __future__ import annotations

import unittest

from services.api_football import ApiFootballClient


class ApiFootballClientTests(unittest.TestCase):
    def test_extract_response(self) -> None:
        payload = {"response": [{"id": 1}, {"id": 2}], "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(len(rows), 2)

    def test_extract_errors(self) -> None:
        payload = {"errors": {"requests": "rate limit reached"}}
        msg = ApiFootballClient._extract_errors(payload)
        self.assertIn("rate limit", msg)

    def test_pick_ligamx_league_id(self) -> None:
        leagues = [
            {
                "league": {"id": 262, "name": "Liga MX", "type": "League"},
            }
        ]
        picked = ApiFootballClient._pick_ligamx_league_id(leagues)
        self.assertEqual(picked, 262)

    def test_pick_league_id_premier(self) -> None:
        leagues = [
            {
                "league": {"id": 39, "name": "Premier League", "type": "League"},
            }
        ]
        picked = ApiFootballClient._pick_league_id(leagues, "premier")
        self.assertEqual(picked, 39)

    def test_pick_league_id_concacaf(self) -> None:
        leagues = [
            {
                "league": {"id": 16, "name": "CONCACAF Champions Cup", "type": "League"},
            }
        ]
        picked = ApiFootballClient._pick_league_id(leagues, "concacaf")
        self.assertEqual(picked, 16)

    def test_extract_current_season(self) -> None:
        rows = [
            {
                "seasons": [
                    {"year": 2024, "current": False},
                    {"year": 2025, "current": True},
                ]
            }
        ]
        season = ApiFootballClient._extract_current_season(rows)
        self.assertEqual(season, 2025)


if __name__ == "__main__":
    unittest.main()
