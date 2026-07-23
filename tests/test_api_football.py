from __future__ import annotations

import unittest

from services.api_football import ApiFootballClient, FootballApiError


class ApiFootballClientTests(unittest.TestCase):
    def test_extract_response(self) -> None:
        payload = {"response": [{"id": 1}, {"id": 2}], "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(len(rows), 2)

    def test_extract_response_accepts_single_object(self) -> None:
        payload = {"response": {"team": {"id": 1}}, "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(rows, [{"team": {"id": 1}}])

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

    def test_pick_league_id_champions(self) -> None:
        leagues = [
            {
                "league": {"id": 2, "name": "UEFA Champions League", "type": "Cup"},
            }
        ]
        picked = ApiFootballClient._pick_league_id(leagues, "champions")
        self.assertEqual(picked, 2)

    def test_pick_league_id_worldcup(self) -> None:
        leagues = [
            {
                "league": {"id": 1, "name": "World Cup", "type": "Cup"},
            }
        ]
        picked = ApiFootballClient._pick_league_id(leagues, "worldcup")
        self.assertEqual(picked, 1)

    def test_worldcup_default_id(self) -> None:
        self.assertEqual(ApiFootballClient._LEAGUE_DEFAULT_IDS["worldcup"], 1)

    def test_pick_league_id_expansion_mx(self) -> None:
        leagues = [
            {
                "league": {"id": 263, "name": "Liga de Expansión MX", "type": "League"},
            }
        ]
        picked = ApiFootballClient._pick_league_id(leagues, "expansionmx")
        self.assertEqual(picked, 263)

    def test_expansion_mx_default_id(self) -> None:
        self.assertEqual(ApiFootballClient._LEAGUE_DEFAULT_IDS["expansionmx"], 263)

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

    def test_cache_key_is_stable(self) -> None:
        first = ApiFootballClient._cache_key("/fixtures", {"season": 2025, "league": 262})
        second = ApiFootballClient._cache_key("/fixtures", {"league": 262, "season": 2025})
        self.assertEqual(first, second)

    def test_safe_log_params_excludes_secret_like_keys(self) -> None:
        safe = ApiFootballClient._safe_log_params({"api_key": "secret", "league": 262, "token": "hidden"})

        self.assertEqual(safe, {"league": "262"})

    def test_error_type_keeps_status_and_retryable(self) -> None:
        err = FootballApiError("rate limited", status=429, retryable=True)
        self.assertEqual(err.status, 429)
        self.assertTrue(err.retryable)


if __name__ == "__main__":
    unittest.main()
