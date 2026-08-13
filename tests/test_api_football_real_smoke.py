from __future__ import annotations

import os
import unittest

from services.api_football import ApiFootballClient
from services.football_operation_service import FootballOperationService, FootballOutcome
from services.football_query_service import compile_football_operation


def _real_smoke_enabled() -> bool:
    return os.environ.get("RUN_API_FOOTBALL_REAL_SMOKE") == "1" and bool(os.environ.get("API_FOOTBALL_KEY"))


@unittest.skipUnless(_real_smoke_enabled(), "set RUN_API_FOOTBALL_REAL_SMOKE=1 and API_FOOTBALL_KEY to run API-Football smoke tests")
class RealApiFootballOperationSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = ApiFootballClient(api_key=str(os.environ["API_FOOTBALL_KEY"]))
        self.service = FootballOperationService(self.client)

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_team_next_fixture_uses_resolved_team_scope(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando juega River Plate",
            {"data_focus": "next_fixtures", "team_candidates": ["River Plate"]},
        )

        result = await self.service.execute(operation, league_id=None, season=None, data_focus="single_next_fixtures")

        self.assertIn(result.outcome, {FootballOutcome.SELECTED, FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND})
        if result.outcome == FootballOutcome.SELECTED:
            self.assertIsNotNone(result.team_context_row)
            self.assertIn("/teams", result.endpoints)
            self.assertIn("/fixtures", result.endpoints)

    async def test_player_recent_stats_locks_identity_before_stats(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "estadisticas recientes de Jude Bellingham jugador ingles del Real Madrid",
            {
                "data_focus": "player_recent_stats",
                "player_candidates": ["Jude Bellingham"],
                "team_candidates": ["Real Madrid"],
                "country_candidates": ["England"],
            },
        )

        result = await self.service.execute(operation, league_id=None, season=None, data_focus="player_recent_stats")

        self.assertIn(result.outcome, {FootballOutcome.SELECTED, FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE})
        self.assertIn("/players/profiles", result.endpoints)
        if result.player_context_row is not None:
            player = result.player_context_row.get("player") if isinstance(result.player_context_row, dict) else {}
            self.assertIsInstance(player.get("id"), int)
            self.assertIn("/players/seasons", result.endpoints)

    async def test_league_standings_resolves_league_and_season(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_TABLE",
            "tabla de la Premier League",
            {"data_focus": "standings", "league_candidates": ["Premier League"]},
        )

        result = await self.service.execute(operation, league_id=None, season=None, data_focus="standings")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn("/standings", result.endpoints)
        self.assertIsInstance(result.standings_table, list)


if __name__ == "__main__":
    unittest.main()
