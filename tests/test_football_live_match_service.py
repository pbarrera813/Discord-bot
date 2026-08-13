from __future__ import annotations

import unittest

from services.football_live_match_service import (
    FootballLiveMatchService,
    derive_second_half_statistics,
    match_requested_stat,
    normalize_match_statistics,
    normalize_fixture_players,
    normalize_shootout,
)


def _fixture(
    fixture_id: int,
    *,
    home_id: int = 1,
    away_id: int = 2,
    home: str = "Home",
    away: str = "Away",
    status: str = "FT",
    date_iso: str = "2026-07-25",
) -> dict:
    return {
        "fixture": {"id": fixture_id, "date": f"{date_iso}T20:00:00-06:00", "status": {"short": status, "elapsed": 90}},
        "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
        "goals": {"home": 1, "away": 0},
    }


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.live = []
        self.today = []
        self.last = []
        self.next = []
        self.detail = []
        self.events = []
        self.stats = []
        self.half_stats = []
        self.lineups = []
        self.players = []

    async def get_live_fixtures(self, **_kwargs):  # noqa: ANN202
        self.calls.append("live")
        return self.live

    async def get_fixtures_on_date(self, **_kwargs):  # noqa: ANN202
        self.calls.append("date")
        return self.today

    async def get_last_fixtures(self, **_kwargs):  # noqa: ANN202
        self.calls.append("last")
        return self.last

    async def get_next_fixtures(self, **_kwargs):  # noqa: ANN202
        self.calls.append("next")
        return self.next

    async def get_fixture_by_id(self, **_kwargs):  # noqa: ANN202
        self.calls.append("fixture_by_id")
        return self.detail

    async def get_fixture_events(self, **_kwargs):  # noqa: ANN202
        self.calls.append("events")
        return self.events

    async def get_fixture_statistics(self, **kwargs):  # noqa: ANN202
        self.calls.append("half_stats" if kwargs.get("half") else "stats")
        return self.half_stats if kwargs.get("half") else self.stats

    async def get_fixture_lineups(self, **_kwargs):  # noqa: ANN202
        self.calls.append("lineups")
        return self.lineups

    async def get_fixture_players(self, **_kwargs):  # noqa: ANN202
        self.calls.append("players")
        return self.players


class FootballLiveMatchServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_all_returned_statistics(self) -> None:
        rows = [
            {
                "team": {"id": 1, "name": "Rayados"},
                "statistics": [
                    {"type": "Shots on Goal", "value": 4},
                    {"type": "Ball Possession", "value": "58%"},
                    {"type": "Expected Threat", "value": "1.7"},
                    {"type": "Passes %", "value": "83%"},
                    {"type": "Null Thing", "value": None},
                ],
            }
        ]

        stats = normalize_match_statistics(rows)
        team_stats = stats[1]

        self.assertEqual(team_stats["shots_on_goal"].numeric_value, 4.0)
        self.assertEqual(team_stats["ball_possession"].display_value, "58%")
        self.assertEqual(team_stats["expected_threat"].original_label, "Expected Threat")
        self.assertEqual(team_stats["passes_percent"].numeric_value, 83.0)
        self.assertNotIn("null_thing", team_stats)

    def test_stat_matching_uses_synonyms_then_returned_labels(self) -> None:
        stats = normalize_match_statistics([
            {"team": {"id": 1, "name": "A"}, "statistics": [{"type": "Shots on Goal", "value": 2}, {"type": "Expected Threat", "value": 1.2}]}
        ])

        self.assertEqual(match_requested_stat(stats, "tiros a puerta"), "shots_on_goal")
        self.assertEqual(match_requested_stat(stats, "expected threat"), "expected_threat")
        self.assertIsNone(match_requested_stat(stats, "duelos espaciales"))

    async def test_live_team_lookup_rejects_unrelated_fixtures(self) -> None:
        client = _Client()
        client.live = [_fixture(10, home_id=9, away_id=8), _fixture(11, home_id=1, away_id=2)]

        match = await FootballLiveMatchService(client).find_live_or_today_fixture_for_team(1, "2026-07-26")

        self.assertEqual(match.fixture_id, 11)
        self.assertEqual(client.calls, ["live"])

    async def test_yesterday_lookup_matches_team_and_date_without_live_call(self) -> None:
        client = _Client()
        client.today = [
            _fixture(20, home_id=1, away_id=2, date_iso="2026-07-24"),
            _fixture(21, home_id=1, away_id=3, date_iso="2026-07-25"),
        ]

        match = await FootballLiveMatchService(client).find_recent_fixture_for_team(
            1,
            "yesterday",
            today_iso="2026-07-26",
        )

        self.assertEqual(match.fixture_id, 21)
        self.assertEqual(client.calls, ["date"])

    async def test_last_finished_requires_terminal_status_and_team_pair(self) -> None:
        client = _Client()
        client.last = [
            _fixture(30, home_id=1, away_id=2, status="NS"),
            _fixture(31, home_id=1, away_id=9, status="FT"),
            _fixture(32, home_id=2, away_id=1, status="FT"),
        ]

        match = await FootballLiveMatchService(client).find_last_finished_fixture_for_team(1, opponent_id=2)

        self.assertEqual(match.fixture_id, 32)
        self.assertEqual(match.status_short, "FT")

    async def test_unrelated_past_fixture_returns_mismatch_note(self) -> None:
        client = _Client()
        client.last = [_fixture(40, home_id=8, away_id=9, status="FT")]

        match = await FootballLiveMatchService(client).find_last_finished_fixture_for_team(1, opponent_id=2)

        self.assertIsNone(match.fixture_id)
        self.assertIn("fixture_team_mismatch", match.notes)

    def test_second_half_derivation_only_uses_additive_stats(self) -> None:
        full = [{"team": {"id": 1, "name": "A"}, "statistics": [{"type": "Total Shots", "value": 10}, {"type": "Ball Possession", "value": "60%"}]}]
        first = [{"team": {"id": 1, "name": "A"}, "statistics": [{"type": "Total Shots", "value": 4}, {"type": "Ball Possession", "value": "55%"}]}]

        second = derive_second_half_statistics(full, first)

        self.assertEqual(second[1]["total_shots"].numeric_value, 6.0)
        self.assertTrue(second[1]["total_shots"].derived)
        self.assertNotIn("ball_possession", second[1])

    def test_shootout_attempts_do_not_mix_regulation_penalties(self) -> None:
        fixture = _fixture(50, status="P")
        fixture["score"] = {"penalty": {"home": 1, "away": 1}}
        regulation = {"time": {"elapsed": 80}, "team": {"id": 1, "name": "Home"}, "player": {"id": 9, "name": "Reg"}, "type": "Goal", "detail": "Penalty"}
        attempt = {"time": {"elapsed": 120}, "team": {"id": 1, "name": "Home"}, "player": {"id": 10, "name": "Shooter"}, "type": "Goal", "detail": "Penalty"}
        baseline = {"0|80||1|9|Goal|Penalty"}

        shootout = normalize_shootout(fixture, [regulation, attempt], pre_shootout_event_keys=baseline, live_entered_shootout=True)

        self.assertTrue(shootout.aggregate_available)
        self.assertEqual(len(shootout.attempts), 1)
        self.assertEqual(shootout.attempts[0].player_name, "Shooter")

    def test_historical_pen_exposes_aggregate_without_ambiguous_attempts(self) -> None:
        fixture = _fixture(51, status="PEN")
        fixture["score"] = {"penalty": {"home": 4, "away": 3}}
        events = [{"time": {"elapsed": 90}, "team": {"id": 1}, "player": {"name": "Reg"}, "type": "Goal", "detail": "Penalty"}]

        shootout = normalize_shootout(fixture, events)

        self.assertEqual((shootout.home_penalties, shootout.away_penalties), (4, 3))
        self.assertFalse(shootout.attempts_available)
        self.assertTrue(shootout.attempts_ambiguous)

    def test_fixture_player_performance_preserves_nested_metrics(self) -> None:
        rows = [
            {
                "team": {"id": 1, "name": "A"},
                "players": [
                    {
                        "player": {"id": 9, "name": "Keeper"},
                        "statistics": [{"games": {"minutes": 90, "rating": "7.4"}, "goals": {"saves": 5}, "custom": {"future": 12}}],
                    }
                ],
            }
        ]

        performances = normalize_fixture_players(rows)

        self.assertEqual(performances[0].metrics["games_minutes"], 90)
        self.assertEqual(performances[0].metrics["goals_saves"], 5)
        self.assertEqual(performances[0].metrics["custom_future"], 12)

    async def test_match_center_reuses_embedded_payloads_before_subendpoint_fallback(self) -> None:
        client = _Client()
        client.detail = [
            {
                **_fixture(60, status="1H"),
                "events": [{"id": 1, "type": "Goal", "detail": "Normal Goal"}],
                "statistics": [{"team": {"id": 1, "name": "Home"}, "statistics": [{"type": "Total Shots", "value": 2}]}],
                "players": [{"team": {"id": 1, "name": "Home"}, "players": []}],
            }
        ]

        match = await FootballLiveMatchService(client).get_match_center(60, time_scope="live", include_events=True, include_stats=True, include_players=True)

        self.assertEqual(match.fixture_id, 60)
        self.assertEqual(client.calls, ["fixture_by_id"])
        self.assertEqual(match.normalized_events[0].event_key, "id:1")
        self.assertIn("total_shots", match.stats[1])


if __name__ == "__main__":
    unittest.main()
