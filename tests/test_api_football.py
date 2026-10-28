from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from services.api_football import ApiFootballClient, FootballApiError, FootballApiPage
from services.football_api_request_compiler import InvalidFootballApiRequest, build_coach_search_request, build_injury_request, build_odds_reference_request, build_odds_request, build_league_search_request, build_player_profile_request, build_player_search_request, build_team_search_request
from services.football_query_service import compile_football_operation
from services.football_formatter import fixture_datetime


class _FakeFootballResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return {"response": [], "errors": []}


class _FakeFootballSession:
    closed = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return _FakeFootballResponse()


def _player_slot(name: str):
    return compile_football_operation("FOOTBALL_PLAYER_QUERY", name, {"data_focus": "player", "player_candidates": [name]}).player_slots[0]


def _league_slot(name: str):
    return compile_football_operation("FOOTBALL_LOOKUP", name, {"data_focus": "league_lookup", "league_candidates": [name]}).league_slots[0]


def _team_slot(name: str):
    return compile_football_operation("FOOTBALL_LOOKUP", name, {"data_focus": "next_fixtures", "team_candidates": [name]}).team_slots[0]


def _client_with_fake_transport() -> tuple[ApiFootballClient, _FakeFootballSession]:
    client = ApiFootballClient(api_key="key", base_url="https://example.test", timezone_name="America/Mexico_City")
    session = _FakeFootballSession()

    async def get_session():
        return session

    client._get_session = get_session
    return client, session


class ApiFootballClientTests(unittest.TestCase):
    def test_odds_and_reference_endpoints_send_documented_parameters(self) -> None:
        async def run() -> None:
            client, session = _client_with_fake_transport()
            page = await client.get_odds_page(
                build_odds_request(fixture_id=157140, bookmaker_id=6, bet_id=1, page=2)
            )
            self.assertIsInstance(page, FootballApiPage)
            await client.get_live_odds(build_odds_request(league_id=39, bet_id=101, live=True))
            await client.get_odds_bookmakers(build_odds_reference_request(search="Caliente"))
            await client.get_odds_bets(build_odds_reference_request(search="Winner"))
            await client.get_live_odds_bets(build_odds_reference_request(search="Winner"))
            await client.get_odds_mapping(page=3)
            self.assertEqual(
                [call[0].replace("https://example.test", "") for call in session.calls],
                ["/odds", "/odds/live", "/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/odds/mapping"],
            )
            self.assertEqual(session.calls[0][1], {"fixture": 157140, "bookmaker": 6, "bet": 1, "page": 2, "timezone": "America/Mexico_City"})
            self.assertEqual(session.calls[1][1], {"league": 39, "bet": 101})
            self.assertEqual(session.calls[-1][1], {"page": 3})

        asyncio.run(run())


    def test_worldcup_default_id(self) -> None:
        self.assertEqual(ApiFootballClient._LEAGUE_DEFAULT_IDS["worldcup"], 1)


    def test_expansion_mx_default_id(self) -> None:
        self.assertEqual(ApiFootballClient._LEAGUE_DEFAULT_IDS["expansionmx"], 263)


    def test_error_type_keeps_status_and_retryable(self) -> None:
        err = FootballApiError("rate limited", status=429, retryable=True)
        self.assertEqual(err.status, 429)
        self.assertTrue(err.retryable)

    def test_next_last_fixture_params_allow_team_only_scope(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            await client.get_next_fixtures(next_count=2, team_id=33)
            await client.get_last_fixtures(last_count=2, team_id=33)

        import asyncio

        asyncio.run(_run())

        self.assertEqual(session.calls[0][1], {"next": 2, "timezone": "America/Mexico_City", "team": 33})
        self.assertEqual(session.calls[1][1], {"last": 2, "timezone": "America/Mexico_City", "team": 33})

    def test_fixture_endpoints_send_configured_timezone(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            await client.get_live_fixtures(league_id=262)
            await client.get_fixtures_on_date(league_id=262, season=2026, date_iso="2026-07-25")
            await client.get_fixture_by_id(fixture_id=123)
            await client.get_head_to_head(team_a_id=1, team_b_id=2)

        import asyncio

        asyncio.run(_run())

        self.assertEqual(session.calls[0][1]["timezone"], "America/Mexico_City")
        self.assertEqual(session.calls[1][1]["timezone"], "America/Mexico_City")
        self.assertEqual(session.calls[2][1]["timezone"], "America/Mexico_City")
        self.assertEqual(session.calls[3][1]["timezone"], "America/Mexico_City")
        async def verify_calendar_filters():
            await client.get_fixtures_on_date(league_id=262, season=2025, round_name="Regular Season - 12")
            self.assertEqual(session.calls[-1][1], {"league": 262, "season": 2025,
                "round": "Regular Season - 12", "timezone": "America/Mexico_City"})
            await client.get_fixtures_on_date(league_id=262, season=2025, team_id=1,
                                            date_iso="2025-10-27", round_name="Regular Season - 12")
            self.assertEqual(session.calls[-1][1]["date"], "2025-10-27")
            self.assertEqual(session.calls[-1][1]["team"], 1)
            count = len(session.calls)
            with self.assertRaises(InvalidFootballApiRequest):
                await client.get_fixtures_on_date(league_id=262, season=2025)
            with self.assertRaises(InvalidFootballApiRequest):
                await client.get_fixtures_on_date(round_name="Regular Season - 12")
            self.assertEqual(len(session.calls), count)
        asyncio.run(verify_calendar_filters())
        async def verify_fixture_ttl():
            with patch("services.api_football.time.monotonic",return_value=100) as clock:
                await client.get_fixture_by_id(fixture_id=456)
                count = len(session.calls)
                clock.return_value = 120
                await client.get_fixture_by_id(fixture_id=456,cache_ttl_seconds=30)
                self.assertEqual(len(session.calls),count)
                clock.return_value = 131
                await client.get_fixture_by_id(fixture_id=456,cache_ttl_seconds=30)
                self.assertEqual(len(session.calls),count+1)
                clock.return_value = 200
                await client.get_fixture_by_id(fixture_id=456)
                self.assertEqual(len(session.calls),count+1)
                clock.return_value = 252
                await client.get_fixture_by_id(fixture_id=456)
                self.assertEqual(len(session.calls),count+2)
        asyncio.run(verify_fixture_ttl())
        async def verify_live_scopes():
            scoped_client, transport = _client_with_fake_transport()
            for league, team in [(262,1),(262,2),(262,None),(None,1),(None,None)]:
                await scoped_client.get_live_fixtures(league_id=league,team_id=team)
            self.assertEqual(len(transport.calls),5)
            await scoped_client.get_live_fixtures(league_id=262,team_id=1)
            self.assertEqual(len(transport.calls),5)
        asyncio.run(verify_live_scopes())



    def test_today_iso_uses_configured_football_timezone(self) -> None:
        client = ApiFootballClient(api_key="key", base_url="https://example.test", timezone_name="America/Mexico_City")

        self.assertRegex(client.today_iso(), r"^20\d{2}-\d{2}-\d{2}$")

    def test_fixture_datetime_formats_cst_offset(self) -> None:
        item = {"fixture": {"date": "2026-07-25T03:00:00-06:00"}}

        self.assertEqual(fixture_datetime(item), "2026-07-25 03:00 CST")

    def test_search_leagues_uses_league_endpoint_without_api_key_in_params(self) -> None:
        client, session = _client_with_fake_transport()

        import asyncio

        request = build_league_search_request(search=_league_slot("Brasileirao"), current=True)
        asyncio.run(client.search_leagues(request))

        self.assertEqual(session.calls, [("https://example.test/leagues", {"search": "Brasileirao", "current": "true"}, {"x-apisports-key": "key"})])


    def test_search_teams_supports_typed_search_fallback(self) -> None:
        client, session = _client_with_fake_transport()

        import asyncio

        request = build_team_search_request(_team_slot("Club America"), search=True)
        asyncio.run(client.search_teams(request))

        self.assertEqual(session.calls[0][0], "https://example.test/teams")
        self.assertEqual(session.calls[0][1], {"search": "Club America"})

        for scope in ({"league_id": 71}, {"season": 2026}, {"league_id": 71, "season": 2026}):
            with self.subTest(scope=scope), self.assertRaises(InvalidFootballApiRequest):
                build_team_search_request(_team_slot("Northbank"), search=True, **scope)
        self.assertEqual(len(session.calls), 1)


    def test_player_profile_wrapper_uses_profiles_endpoint_with_validated_lastname(self) -> None:
        client, session = _client_with_fake_transport()

        import asyncio

        request = build_player_profile_request(_player_slot("Julian Brandt"))
        asyncio.run(client.search_player_profiles(request))

        self.assertEqual(session.calls[0][0], "https://example.test/players/profiles")
        self.assertEqual(session.calls[0][1], {"search": "Brandt"})


    def test_documented_scoped_requests_require_season(self) -> None:
        with self.assertRaises(InvalidFootballApiRequest):
            build_injury_request(player_id=278)
        with self.assertRaises(InvalidFootballApiRequest):
            build_injury_request(team_id=529)
        with self.assertRaises(InvalidFootballApiRequest):
            build_injury_request(league_id=262)
        build_injury_request(fixture_id=123)

    def test_search_requests_enforce_documented_minimum_lengths(self) -> None:
        with self.assertRaises(InvalidFootballApiRequest):
            build_player_profile_request(_player_slot("Al"))
        with self.assertRaises(InvalidFootballApiRequest):
            build_player_search_request(_player_slot("Al"))
        with self.assertRaises(InvalidFootballApiRequest):
            build_coach_search_request(search="Al")
        with self.assertRaises(InvalidFootballApiRequest):
            build_odds_reference_request(search="Al")

    def test_league_scoped_fixture_calls_require_season_with_zero_http_calls(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            invalid_calls = [
                lambda: client.get_fixtures_on_date(league_id=140, season=None, date_iso="2026-08-06"),
                lambda: client.get_next_fixtures(league_id=140, season=None, next_count=1),
                lambda: client.get_last_fixtures(league_id=140, season=None, last_count=1),
            ]
            for call in invalid_calls:
                with self.assertRaises(InvalidFootballApiRequest):
                    await call()

        import asyncio

        asyncio.run(_run())
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
