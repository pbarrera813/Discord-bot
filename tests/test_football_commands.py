from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cogs.football import FootballCardView, FootballCog, LEAGUE_CODES, LEAGUE_HELP_TEXT
from services.api_football import FootballApiError
from services import football_cards, football_formatter, football_resolver
from services.football_live_match_service import build_match_data
from services.football_operation_service import FootballOperationService, FootballOutcome, FootballRetrievalResult, football_web_fallback_eligible
from services.football_query_service import compile_football_operation
from services.xai_client import XAIClient


def _value(item):
    return getattr(item, "value", item)


def _player_slot(name: str):
    return compile_football_operation("FOOTBALL_PLAYER_QUERY", name, {"data_focus": "player", "player_candidates": [name]}).player_slots[0]


def _league_slot(name: str):
    return compile_football_operation("FOOTBALL_LOOKUP", name, {"data_focus": "league_lookup", "league_candidates": [name]}).league_slots[0]


def _league_request_kwargs(request):
    return {
        "name": _value(getattr(request, "name", None)),
        "country": _value(getattr(request, "country", None)),
        "search": _value(getattr(request, "search", None)),
        "current": getattr(request, "current", None),
    }


def _team_request_kwargs(request):
    return {
        "name": _value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
    }


def _player_request_kwargs(request):
    return {
        "name": _value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _player_stats_kwargs(request):
    return {
        "player_id": getattr(request, "player_id", None),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _player_seasons_kwargs(request):
    return {"player_id": getattr(request, "player_id", None)}


def _injury_request_kwargs(request):
    return {
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
        "player_id": getattr(request, "player_id", None),
        "fixture_id": getattr(request, "fixture_id", None),
    }


def _transfer_request_kwargs(request):
    return {"team_id": getattr(request, "team_id", None), "player_id": getattr(request, "player_id", None)}


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


class FootballQueryCompilerTests(unittest.TestCase):


    def test_compiles_command_equivalent_operations_from_varied_prompts(self) -> None:
        cases = [
            ("cuando juega pumas", "FOOTBALL_LOOKUP", {"team_candidates": ["Pumas"]}, "fixture_next"),
            ("proximos juegos del america", "FOOTBALL_LOOKUP", {"team_candidates": ["America"]}, "fixture_next"),
            ("ultimos partidos del madrid", "FOOTBALL_LOOKUP", {"team_candidates": ["Madrid"], "league_candidates": ["LaLiga"]}, "fixture_last"),
            ("como va la tabla de la premier", "FOOTBALL_LOOKUP", {"league_candidates": ["Premier League"]}, "standings"),
            ("quien va de goleador en liga mx", "FOOTBALL_LOOKUP", {"league_candidates": ["Liga MX"]}, "top_scorers"),
            ("estadisticas de haaland en el mundial", "FOOTBALL_PLAYER_QUERY", {"player_candidates": ["Haaland"], "league_candidates": ["World Cup"]}, "player_recent_stats"),
            ("lesiones del barca", "FOOTBALL_LOOKUP", {"team_candidates": ["Barcelona"]}, "team_injuries"),
            ("transferencias del america", "FOOTBALL_LOOKUP", {"team_candidates": ["America"]}, "team_transfers"),
            ("historial argentina vs suiza", "FOOTBALL_COMPARISON", {"team_candidates": ["Argentina", "Switzerland"]}, "h2h"),
            ("manda minuto a minuto de este partido", "FOOTBALL_LIVE_WATCH_START", {"fixture_focus": "este partido", "live": True}, "live_watch_start"),
        ]
        for request, action, plan, expected in cases:
            with self.subTest(request=request):
                operation = compile_football_operation(action, request, plan)
                self.assertEqual(operation.operation_type, expected)
                self.assertIn(operation.operation_type, {
                    "fixture_next",
                    "fixture_last",
                    "standings",
                    "top_scorers",
                    "player_profile",
                    "player_recent_stats",
                    "team_injuries",
                    "team_transfers",
                    "h2h",
                    "live_watch_start",
                })

    def test_fixture_stat_event_lineup_player_payloads_compile_to_canonical_operations(self) -> None:
        cases = [
            (
                "estadisticas completas del ultimo partido de Toluca",
                {"data_focus": "statistics", "team_candidates": ["Toluca"]},
                "fixture_statistics",
            ),
            (
                "quien metio los goles de Cruz Azul vs Puebla",
                {"stat_focus": "goals", "team_candidates": ["Cruz Azul", "Puebla"]},
                "fixture_events",
            ),
            (
                "tarjetas y cambios del America vs Pumas",
                {"stat_focus": "cards", "team_candidates": ["America", "Pumas"]},
                "fixture_events",
            ),
            (
                "alineacion inicial de Tigres",
                {"data_focus": "lineups", "team_candidates": ["Tigres"]},
                "fixture_lineups",
            ),
            (
                "jugadores que participaron en Necaxa vs Rayados",
                {"data_focus": "fixture_players", "team_candidates": ["Necaxa", "Rayados"]},
                "player_match_stats",
            ),
        ]
        for request, plan, expected in cases:
            with self.subTest(request=request):
                operation = compile_football_operation("FOOTBALL_MATCH_CENTER", request, plan)
                self.assertEqual(operation.operation_type, expected)
                self.assertFalse(operation.player_slots)


    def test_date_scoped_played_question_compiles_to_result_not_next(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_FIXTURE_QUERY",
            "jugaron los pumas hoy leagues cup?",
            {"operation": "fixture_next", "league_candidates": ["Leagues Cup"], "team_candidates": ["Pumas", "Pumas UNAM"]},
        )

        self.assertEqual(operation.operation_type, "fixture_result")
        self.assertEqual(operation.data_focus, "summary")
        self.assertEqual(operation.time_scope, "today")
        self.assertEqual(tuple(slot.name for slot in operation.team_slots), ("Pumas", "Pumas UNAM"))
        self.assertEqual(tuple(slot.name for slot in operation.league_slots), ("Leagues Cup",))


    def test_standings_and_last_match_do_not_create_incompatible_slots(self) -> None:
        standings = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "clasificacion LaLiga",
            {"data_focus": "standings", "league_candidates": ["LaLiga"], "team_candidates": ["LaLiga"]},
        )
        self.assertEqual(standings.operation_type, "standings")
        self.assertTrue(standings.league_slots)
        self.assertFalse(standings.team_slots)
        self.assertFalse(standings.player_slots)

        last_stats = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "estadisticas del ultimo partido de Toluca",
            {"data_focus": "statistics", "team_candidates": ["Toluca"], "player_candidates": ["ultimo partido"]},
        )
        self.assertEqual(last_stats.operation_type, "fixture_statistics")
        self.assertTrue(last_stats.team_slots)
        self.assertFalse(last_stats.player_slots)


    def test_football_web_fallback_uses_only_typed_provider_state(self) -> None:
        def result(outcome: FootballOutcome, *, resolved: bool = True, ambiguous: bool = False) -> FootballRetrievalResult:
            return FootballRetrievalResult(
                outcome=outcome,
                requested_scope={"request_plan": {"required_slots": ["team"]}},
                resolved_entities={"team": {"id": 28343}} if resolved else {},
                ambiguity_candidates=[{"team": {"id": 1}}] if ambiguous else [],
            )

        self.assertTrue(football_web_fallback_eligible(result(FootballOutcome.NOT_FOUND)))
        self.assertTrue(football_web_fallback_eligible(result(FootballOutcome.NO_DATA_FOR_SCOPE)))
        self.assertTrue(football_web_fallback_eligible(result(FootballOutcome.UNSUPPORTED_BY_COVERAGE)))
        self.assertTrue(football_web_fallback_eligible(result(FootballOutcome.PROVIDER_FAILURE)))
        self.assertFalse(football_web_fallback_eligible(result(FootballOutcome.SELECTED)))
        self.assertFalse(football_web_fallback_eligible(result(FootballOutcome.NOT_FOUND, resolved=False)))
        self.assertFalse(football_web_fallback_eligible(result(FootballOutcome.NOT_FOUND, ambiguous=True)))

    def test_provider_failure_preserves_resolved_entities_before_provider_call(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "Synthetic Club standings",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["Synthetic League"],
                "team_candidates": ["Synthetic Club"],
            },
        )

        class _FailingClient:
            async def search_leagues(self, _request):
                return [{"league": {"id": 40, "name": "Synthetic League", "type": "League", "season": 2026}}]

            async def get_standings(self, **_kwargs):
                raise FootballApiError("synthetic provider failure", status=503, retryable=True)

        service = FootballOperationService(_FailingClient())

        async def resolve_team_rows(_operation, *, league_id, season, max_teams):
            return [{"team": {"id": 28343, "name": "Synthetic Club"}}], FootballOutcome.SELECTED

        service.resolve_team_rows = resolve_team_rows
        result = asyncio.run(service.execute(operation, league_id=40, season=2026, data_focus="standings"))

        self.assertEqual(result.outcome, FootballOutcome.PROVIDER_FAILURE)
        self.assertEqual(result.resolved_entities["team"]["id"], 28343)
        self.assertEqual(result.resolved_entities["league"]["id"], 40)
        self.assertIn("provider_error_status=503", result.notes)


    def test_odds_operation_preserves_provider_operands_and_paging(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "pre-match odds",
            {
                "semantic_source": "router",
                "data_focus": "pre_match_odds",
                "fixture_focus": "157140",
                "bookmaker_candidates": ["Caliente"],
                "bet_candidates": ["Match Winner"],
                "reference_page": 2,
            },
        )
        self.assertEqual(operation.operation_type, "fixture_odds_pre_match")
        self.assertEqual(operation.odds_bookmaker_candidates, ("Caliente",))
        self.assertEqual(operation.odds_bet_candidates, ("Match Winner",))
        self.assertEqual(operation.reference_page, 2)

        class _OddsClient:
            def __init__(self) -> None:
                self.calls = []

            async def get_odds_bookmakers(self, request):
                self.calls.append(("bookmakers", request.search.value))
                return [{"id": 6, "name": "Caliente"}]

            async def get_odds_bets(self, request):
                self.calls.append(("bets", request.search.value))
                return [{"id": 1, "name": "Match Winner"}]

            async def get_odds_page(self, request):
                self.calls.append(("odds", request))
                return SimpleNamespace(rows=[{"fixture": {"id": 157140}}], current=2, total=4)

        async def run() -> None:
            client = _OddsClient()
            result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="pre_match_odds")
            self.assertEqual(result.outcome, FootballOutcome.SELECTED)
            request = next(value for name, value in client.calls if name == "odds")
            self.assertEqual(request.fixture_id, 157140)
            self.assertEqual(request.bookmaker_id, 6)
            self.assertEqual(request.bet_id, 1)
            self.assertEqual(request.page, 2)
            self.assertEqual(result.requested_scope["paging"], {"current": 2, "total": 4})

        asyncio.run(run())


    def test_router_invalid_temporal_fields_fail_before_provider_fixture_calls(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "",
            {
                "semantic_source": "router",
                "data_focus": "summary",
                "team_candidates": ["Synthetic Club"],
                "date_hint": "today",
            },
        )
        client = _FakeFootballClient()

        async def run() -> None:
            result = await FootballOperationService(client).execute(
                operation,
                league_id=None,
                season=None,
                data_focus="summary",
            )
            self.assertEqual(result.outcome, FootballOutcome.RESOLUTION_FAILED)
            self.assertIn("invalid_router_date_hint", result.notes)

        asyncio.run(run())
        self.assertEqual(client.calls, [])


    def test_live_odds_uses_live_market_reference_and_never_pre_match_lookup(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "live odds",
            {
                "semantic_source": "router",
                "data_focus": "live_odds",
                "fixture_focus": "157140",
                "bet_candidates": ["Live Winner"],
            },
        )

        class _LiveOddsClient:
            def __init__(self) -> None:
                self.calls = []

            async def get_live_odds_bets(self, request):
                self.calls.append(("live_bets", request.search.value))
                return [{"id": 101, "name": "Live Winner"}]

            async def get_live_odds(self, request):
                self.calls.append(("live_odds", request))
                return [{"fixture": {"id": 157140}}]

        async def run() -> None:
            client = _LiveOddsClient()
            result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="live_odds")
            self.assertEqual(result.outcome, FootballOutcome.SELECTED)
            self.assertEqual([name for name, _value in client.calls], ["live_bets", "live_odds"])
            request = client.calls[-1][1]
            self.assertTrue(request.live)
            self.assertEqual(request.bet_id, 101)

        asyncio.run(run())


    def test_reference_catalog_cannot_satisfy_typed_match_odds_scope(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "pre-match odds for Alpha FC versus Beta FC on 2026-09-04",
            {
                "semantic_source": "router",
                "operation_family": "odds",
                "data_focus": "odds_bets",
                "fixture_focus": "pre_match",
                "date_hint": "2026-09-04",
                "team_candidates": ["Alpha FC", "Beta FC"],
            },
        )

        class _NoReferenceCallClient:
            def __init__(self) -> None:
                self.calls = []

            async def get_odds_bets(self, request):
                self.calls.append(request)
                return [{"id": 1, "name": "Match Winner"}]

        client = _NoReferenceCallClient()
        result = asyncio.run(
            FootballOperationService(client).execute(
                operation,
                league_id=None,
                season=2026,
                data_focus="odds_bets",
            )
        )

        self.assertEqual(result.outcome, FootballOutcome.UNSUPPORTED)
        self.assertIn("capability_recipe_mismatch=reference_with_match_scope", result.notes)
        self.assertEqual(client.calls, [])


    def test_compiles_player_follow_up_from_validated_context(self) -> None:
        operation = compile_football_operation(
            "CHAT",
            "cual fue su ultimo equipo? ahora donde juega?",
            {},
            prior_context='{"entity_type":"player","player_id":123,"player_name":"Julian Brandt"}',
        )

        self.assertEqual(operation.operation_type, "player_previous_team")
        self.assertEqual(operation.route_action, "FOOTBALL_PLAYER_QUERY")
        self.assertEqual(operation.player_candidates, ("Julian Brandt",))


    def test_rayados_alias_resolves_to_monterrey_seed(self) -> None:
        self.assertEqual(football_resolver.canonical_team_query("Rayados"), "Monterrey")
        self.assertEqual(football_resolver.canonical_team_query("Rayados de Monterrey"), "Monterrey")
        self.assertEqual(football_resolver.canonical_team_query("Tigres"), "Tigres UANL")

    def test_compiler_rejects_raw_sentence_entities_and_keeps_clean_candidates(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando empieza la temporada de LaLiga",
            {
                "data_focus": "season_start",
                "league_candidates": ["cuando empieza la temporada de LaLiga", "LaLiga"],
                "team_candidates": ["cuando empieza la temporada de LaLiga"],
            },
        )

        self.assertEqual(operation.operation_type, "competition_structure")
        self.assertEqual(operation.league_candidates, ("LaLiga",))
        self.assertEqual(operation.team_candidates, ())
        self.assertEqual(operation.capability_intent.operation_family, "competition_structure")
        self.assertEqual(operation.capability_intent.data_focus, "season_start")


    def test_planner_cannot_inject_ungrounded_entity_or_capability(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "standings for LEAGUE_A",
            {
                "data_focus": "next_fixtures",
                "league_candidates": ["LEAGUE_B", "LEAGUE_A"],
                "team_candidates": ["TEAM_B"],
            },
        )

        self.assertEqual(operation.operation_type, "standings")
        self.assertEqual(operation.league_candidates, ("LEAGUE_A",))
        self.assertFalse(operation.team_candidates)
        self.assertEqual(operation.capability_intent.operation_family, "standings")


    def test_compiler_does_not_let_liga_alias_override_laliga(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando empieza la temporada de LaLiga",
            {"data_focus": "season_start"},
        )

        self.assertIn("laliga", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])
        self.assertNotIn("ligamx", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])
        self.assertEqual(operation.operation_type, "competition_structure")


class _FakeCtx:
    def __init__(self, client: object) -> None:
        self.guild = SimpleNamespace(id=1)
        self.interaction = None
        self.author = SimpleNamespace(id=42)
        self.sent: list[dict[str, object]] = []
        self.bot = SimpleNamespace(
            api_football_client=client,
            db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en"))),
        )

    async def send(self, content: str | None = None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.sent))


class _FakeFootballClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, call_name: str, **kwargs):
        self.calls.append((call_name, dict(kwargs)))

    async def resolve_league_id(self, league_key):
        self._record("resolve_league_id", league_key=league_key)
        return 262

    async def get_current_season(self, league_id):
        self._record("get_current_season", league_id=league_id)
        return 2026

    async def search_leagues(self, request):
        kwargs = _league_request_kwargs(request)
        self._record("search_leagues", **kwargs)
        return [{"league": {"id": 71, "name": str(kwargs.get("search") or kwargs.get("name") or "League"), "type": "League"}}]

    async def get_live_fixtures(self, **kwargs):
        self._record("get_live_fixtures", **kwargs)
        return [_fixture()]

    async def get_fixtures_on_date(self, **kwargs):
        self._record("get_fixtures_on_date", **kwargs)
        return [_fixture()]

    async def get_next_fixtures(self, **kwargs):
        self._record("get_next_fixtures", **kwargs)
        return [_fixture()]

    async def get_last_fixtures(self, **kwargs):
        self._record("get_last_fixtures", **kwargs)
        return [_fixture()]

    async def get_standings(self, **kwargs):
        self._record("get_standings", **kwargs)
        return [{"league": {"standings": [[{"rank": 1, "points": 3, "goalsDiff": 1, "team": {"id": 1, "name": "Mexico"}, "all": {"played": 1}}]]}}]

    async def search_teams(self, request):
        kwargs = _team_request_kwargs(request)
        self._record("search_teams", **kwargs)
        name = str(kwargs.get("name", "Team"))
        team_id = 1 if "switzerland" not in name.casefold() else 2
        return [{"team": {"id": team_id, "name": name.title()}}]

    async def get_top_scorers(self, **kwargs):
        self._record("get_top_scorers", **kwargs)
        return [_player()]

    async def get_top_assists(self, **kwargs):
        self._record("get_top_assists", **kwargs)
        return [_player()]

    async def get_top_yellow_cards(self, **kwargs):
        self._record("get_top_yellow_cards", **kwargs)
        return [_player()]

    async def get_top_red_cards(self, **kwargs):
        self._record("get_top_red_cards", **kwargs)
        return [_player()]

    async def search_players(self, request):
        kwargs = _player_request_kwargs(request)
        self._record("search_players", **kwargs)
        return [_player(name=str(kwargs.get("name", "Erling Haaland")))]

    async def search_player_profiles(self, request):
        lastname = _value(getattr(request, "lastname", None))
        self._record("search_player_profiles", search=lastname)
        key = str(lastname or "").casefold()
        if key == "haaland":
            return [_player(name="Erling Haaland")]
        if key == "brandt":
            return [_player(name="Julian Brandt", player_id=11)]
        return []

    async def get_player_stats(self, request):
        kwargs = _player_stats_kwargs(request)
        self._record("get_player_stats", **kwargs)
        row = _player(player_id=kwargs["player_id"])
        row["statistics"][0]["league"] = {"id": kwargs["league_id"] or 39, "name": "Provider League", "season": kwargs["season"]}
        return [row]

    async def get_player_seasons(self, request):
        kwargs = _player_seasons_kwargs(request)
        self._record("get_player_seasons", **kwargs)
        return [{"season": 2026}, {"season": 2025}]

    async def get_fixture_by_id(self, **kwargs):
        self._record("get_fixture_by_id", **kwargs)
        return [_fixture()]

    async def get_fixture_events(self, **kwargs):
        self._record("get_fixture_events", **kwargs)
        return [{"time": {"elapsed": 10}, "team": {"name": "Mexico"}, "player": {"name": "A"}, "type": "Goal", "detail": "Normal Goal"}]

    async def get_fixture_statistics(self, **kwargs):
        self._record("get_fixture_statistics", **kwargs)
        return [{"team": {"name": "Mexico"}, "statistics": [{"type": "Shots", "value": 4}]}]

    async def get_fixture_lineups(self, **kwargs):
        self._record("get_fixture_lineups", **kwargs)
        return [{"team": {"name": "Mexico"}, "coach": {"name": "Coach"}, "formation": "4-3-3"}]

    async def get_fixture_rounds(self, request):
        self._record("get_fixture_rounds", league_id=getattr(request, "league_id", None), season=_value(getattr(request, "season", None)), current=getattr(request, "current", None), include_dates=getattr(request, "include_dates", None))
        return [{"round": "Regular Season - 1", "dates": ["2026-08-01"]}]

    async def get_team_statistics(self, request=None, **kwargs):
        if request is not None:
            kwargs = {"league_id": getattr(request, "league_id", None), "season": _value(getattr(request, "season", None)), "team_id": getattr(request, "team_id", None)}
        self._record("get_team_statistics", **kwargs)
        return [{"team": {"id": kwargs.get("team_id"), "name": "Mexico"}, "fixtures": {"played": {"total": 1}}, "goals": {"for": {"total": {"total": 2}}}}]

    async def get_player_squads(self, request):
        self._record("get_player_squads", player_id=getattr(request, "player_id", None), team_id=getattr(request, "team_id", None))
        return [{"team": {"id": getattr(request, "team_id", None), "name": "Mexico"}, "players": []}]

    async def get_player_teams(self, request):
        self._record("get_player_teams", player_id=getattr(request, "player_id", None))
        return [{"team": {"id": 5, "name": "Manchester City"}, "seasons": [2026]}]

    async def search_coaches(self, request):
        self._record("search_coaches", coach_id=getattr(request, "coach_id", None), team_id=getattr(request, "team_id", None), search=_value(getattr(request, "search", None)))
        return [{"coach": {"id": 3, "name": "Coach"}, "team": {"id": getattr(request, "team_id", None), "name": "Mexico"}, "career": []}]

    async def search_venues(self, request):
        self._record("search_venues", name=_value(getattr(request, "name", None)), search=_value(getattr(request, "search", None)))
        return [{"id": 10, "name": str(_value(getattr(request, "search", None)) or "Stadium"), "city": "City", "capacity": 50000}]

    async def get_trophies(self, request):
        self._record("get_trophies", player_id=getattr(request, "player_id", None), coach_id=getattr(request, "coach_id", None))
        return [{"league": "League", "place": "Winner"}]

    async def get_sidelined(self, request):
        self._record("get_sidelined", player_id=getattr(request, "player_id", None), coach_id=getattr(request, "coach_id", None))
        return [{"type": "Injury", "start": "2026-01-01", "end": "2026-01-10"}]

    async def get_predictions(self, request):
        self._record("get_predictions", fixture_id=getattr(request, "fixture_id", None))
        return [{"predictions": {"winner": {"name": "Mexico"}, "percent": {"home": "45%", "draw": "30%", "away": "25%"}}}]

    async def get_odds(self, request):
        self._record("get_odds", fixture_id=getattr(request, "fixture_id", None), league_id=getattr(request, "league_id", None))
        return [{"bookmakers": [{"name": "Book", "bets": []}]}]

    async def get_live_odds(self, request):
        self._record("get_live_odds", fixture_id=getattr(request, "fixture_id", None), league_id=getattr(request, "league_id", None))
        return [{"fixture": {"id": getattr(request, "fixture_id", None)}, "odds": []}]

    async def get_league_by_id(self, **kwargs):
        self._record("get_league_by_id", **kwargs)
        year = kwargs.get("season") if kwargs.get("season") is not None else 2026
        return [
            {
                "league": {"id": kwargs.get("league_id")},
                "seasons": [
                    {
                        "year": year,
                        "start": f"{int(year)}-01-01",
                        "end": f"{int(year)}-12-31",
                        "coverage": {"predictions": True, "odds": True},
                    }
                ],
            }
        ]

    async def get_injuries(self, request):
        kwargs = _injury_request_kwargs(request)
        self._record("get_injuries", **kwargs)
        return []

    async def get_transfers(self, request):
        kwargs = _transfer_request_kwargs(request)
        self._record("get_transfers", **kwargs)
        return []

    async def get_head_to_head(self, **kwargs):
        self._record("get_head_to_head", **kwargs)
        return [_fixture()]


def _fixture() -> dict[str, object]:
    return {
        "fixture": {"id": 10, "date": f"{date.today().isoformat()}T20:00:00+00:00", "status": {"short": "NS"}},
        "league": {"name": "Liga MX", "round": "Round 1"},
        "teams": {"home": {"id": 1, "name": "Mexico"}, "away": {"id": 2, "name": "France"}},
        "goals": {"home": None, "away": None},
    }


def _player(*, name: str = "Erling Haaland", player_id: int = 9) -> dict[str, object]:
    return {
        "player": {"id": player_id, "name": name},
        "statistics": [
            {
                "team": {"id": 5, "name": "Manchester City"},
                "games": {"position": "Attacker", "appearences": 12},
                "goals": {"total": 10, "assists": 2},
            }
        ],
    }


class FootballCommandTests(unittest.TestCase):

    def test_worldcup_is_in_league_choices_and_help(self) -> None:
        self.assertIn("worldcup", LEAGUE_CODES)
        self.assertIn("worldcup", LEAGUE_HELP_TEXT)

    def test_shared_resolver_aliases(self) -> None:
        self.assertEqual(football_resolver.normalize_league_key("Champions League"), "champions")
        self.assertEqual(football_resolver.canonical_team_query("barca"), "Barcelona")
        self.assertEqual(football_resolver.canonical_team_query("francia"), "France")
        self.assertEqual(football_resolver.canonical_player_query("mbappe"), "mbappe")
        self.assertEqual(football_resolver.canonical_player_query("dibu mtz"), "Emiliano Martinez")

    def test_player_query_parser_splits_stat_focus_from_name(self) -> None:
        parsed = football_resolver.parse_player_query("dibu mtz penales")

        self.assertEqual(parsed.stat_focus, "penalties")
        self.assertEqual(parsed.candidates[0], "Emiliano Martinez")
        self.assertNotIn("penales", " ".join(parsed.candidates).casefold())


    def test_shared_formatter_fixture_line(self) -> None:
        item = {
            "teams": {"home": {"name": "Mexico"}, "away": {"name": "France"}},
            "goals": {"home": 1, "away": 2},
            "fixture": {"status": {"short": "FT"}},
            "league": {"round": "Final"},
        }
        title, details = football_formatter.format_fixture_line(item)
        self.assertEqual(title, "Mexico vs France")
        self.assertIn("1 - 2", details)
        self.assertIn("Final", details)


class FootballCommandContractTests(unittest.IsolatedAsyncioTestCase):


    async def test_xai_identity_replan_sends_exact_current_feedback_and_progress(self) -> None:
        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def text(self):
                return "{}"

        class _Session:
            def __init__(self):
                self.payload = None

            def post(self, _url, *, json, headers):
                self.payload = json
                return _Response()

        session = _Session()
        client = XAIClient("test-key", "test-model")

        async def _get_session():
            return session

        client._get_session = _get_session
        client._extract_output_text = lambda _data: json.dumps({
            "slot_kind": "team", "slot_index": 1,
            "candidate_names": ["Wolverhampton Wanderers"],
            "scope_candidates": [], "evidence_refs": ["fbref:current"],
        })
        feedback = {
            "failed_slot": {"kind": "team", "index": 1},
            "failed_slots": [{"kind": "team", "index": 1}],
            "outcome": "NOT_FOUND",
            "omissions": ["unique_provider_scope"],
            "scope": {},
            "resolved_entities": {"league": {"id": 40, "display_name": "Championship"}},
            "observed_candidates": [{
                "ref": "fbref:current", "entity_type": "team", "display_name": "Wolves",
                "provider_id": 39, "scope": {},
            }],
        }
        progress = {
            "newly_resolved_entities": {"league": {"id": 40, "display_name": "Championship"}},
            "newly_validated_scope": {},
        }

        result = await client.replan_football_identity(
            operation="h2h", focus="h2h", slot_kind="team", slot_index=1,
            original_candidate="Wolverhampton", repair_feedback=feedback,
            provider_progress=progress,
        )
        sent = json.loads(session.payload["input"][1]["content"])

        self.assertEqual(result["evidence_refs"], ["fbref:current"])
        self.assertEqual(sent["repair_feedback"], feedback)
        self.assertEqual(sent["provider_progress"], progress)
        self.assertEqual(sent["slot_index"], 1)

        client._extract_output_text = lambda _data: json.dumps({
            "slot_kind": "league", "slot_index": 0, "candidate_names": ["Second Division"],
            "scope_candidates": [], "evidence_refs": [],
        })
        league_feedback = {**feedback, "failed_slot": {"kind": "league", "index": 0},
                           "failed_slots": [{"kind": "league", "index": 0}],
                           "observed_candidates": [], "resolved_entities": {}}
        result = await client.replan_football_identity(
            operation="standings", focus="standings", slot_kind="league", slot_index=0,
            original_candidate="Regional Second", repair_feedback=league_feedback,
            provider_progress={},
        )
        self.assertEqual(result["candidate_names"], ["Second Division"])
        scope_schema = session.payload["text"]["format"]["schema"]["properties"]["scope_candidates"]
        self.assertEqual(scope_schema["maxItems"], 0)
        self.assertEqual(scope_schema["items"]["properties"]["league_name"]["type"], "string")


    async def test_router_planner_league_is_not_authoritative(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "where is Wolverhampton in the table",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["Premier League"],
                "team_candidates": ["Wolverhampton"],
            },
        )

        self.assertEqual(operation.league_slots[0].source, "planner")
        self.assertEqual(operation.league_slots[0].authority, "PLANNER_INFERENCE")
        self.assertEqual(operation.league_slots[0].source_component, "native_router")
        self.assertEqual(operation.league_slots[0].evidence, "planner_candidate")


    async def test_explicit_ambiguous_league_team_current_match_still_fails_closed(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "where is Wolverhampton in the EFL Championship table",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["EFL Championship"],
                "team_candidates": ["Wolverhampton"],
            },
        )

        for roster_by_league, expected_outcome, expected_note in (
            (
                {40: [{"team": {"id": 901, "name": "Wolverhampton"}}], 41: [{"team": {"id": 902, "name": "Wolverhampton"}}]},
                FootballOutcome.AMBIGUOUS,
                "team_ambiguous",
            ),
            (
                {40: [], 41: []},
                FootballOutcome.NOT_FOUND,
                "team_not_found",
            ),
        ):
            with self.subTest(expected_outcome=expected_outcome):
                class _FailClosedClient(_FakeFootballClient):
                    async def search_leagues(self, request):
                        return [
                            {"league": {"id": 40, "name": "Championship", "type": "League"}},
                            {"league": {"id": 41, "name": "Championship", "type": "League"}},
                        ]

                    async def search_teams(self, request):
                        return roster_by_league[request.league_id]

                    async def get_standings(self, **kwargs):
                        raise AssertionError("unresolved team-scoped league must stop before standings")

                result = await FootballOperationService(_FailClosedClient()).execute(
                    operation,
                    league_id=None,
                    season=None,
                    data_focus="standings",
                )

                self.assertEqual(result.outcome, expected_outcome)
                self.assertIn(expected_note, result.notes)


    async def test_league_country_directory_selects_unique_provider_extension(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "table for Synthetic League in Exampleland",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["Synthetic League"],
                "country_candidates": ["Exampleland"],
            },
        )

        class _DirectoryClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search_leagues(self, request):
                kwargs = _league_request_kwargs(request)
                self.calls.append(kwargs)
                if kwargs["name"] is None and kwargs["country"] == "Exampleland":
                    return [{"league": {"id": 77, "name": "Synthetïc-League / Extension"}}]
                return []

            async def get_current_season(self, league_id):
                return 2026

        client = _DirectoryClient()
        lookup = await football_resolver.resolve_league_candidate(
            client,
            operation.league_slots[0],
            country=operation.country_slots[0],
        )

        self.assertEqual(lookup.league_id, 77)
        self.assertEqual(lookup.row["league"]["name"], "Synthetïc-League / Extension")
        country_directory_calls = [
            call for call in client.calls
            if call["name"] is None and call["country"] == "Exampleland"
        ]
        self.assertEqual(country_directory_calls, [
            {"name": None, "country": "Exampleland", "search": None, "current": True},
        ])
        self.assertEqual(
            len(country_directory_calls),
            1,
        )

    async def test_league_country_directory_keeps_distinct_matches_ambiguous(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "table for Synthetic League in Exampleland",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["Synthetic League"],
                "country_candidates": ["Exampleland"],
            },
        )

        class _DirectoryClient:
            async def search_leagues(self, request):
                kwargs = _league_request_kwargs(request)
                if kwargs["name"] is None:
                    return [
                        {"league": {"id": 77, "name": "Synthetic League Extension"}},
                        {"league": {"id": 77, "name": "Synthetic League Extension"}},
                        {"league": {"id": 78, "name": "Synthetic League Reserve"}},
                    ]
                return []

        lookup = await football_resolver.resolve_league_candidate(
            _DirectoryClient(),
            operation.league_slots[0],
            country=operation.country_slots[0],
        )

        self.assertIsNone(lookup.league_id)
        self.assertTrue(lookup.ambiguous)
        self.assertEqual({row["league"]["id"] for row in lookup.matches}, {77, 78})


    async def test_standings_with_team_scope_resolves_team_and_uses_existing_full_table(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "where is Wolverhampton in the table",
            {
                "semantic_source": "router",
                "data_focus": "standings",
                "league_candidates": ["Synthetic League"],
                "team_candidates": ["Synthetic Club"],
            },
        )
        client = _FakeFootballClient()

        result = await FootballOperationService(client).execute(
            operation,
            league_id=None,
            season=None,
            data_focus="standings",
        )

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertEqual(len(result.standings_table), 1)
        self.assertEqual(result.standing_row["team"]["id"], 1)
        self.assertEqual(
            result.requested_scope["request_plan"]["entity_evidence"]["team"][0]["label"],
            "Synthetic Club",
        )
        self.assertEqual([name for name, _kwargs in client.calls if name == "get_standings"], ["get_standings"])
        self.assertEqual([name for name, _kwargs in client.calls].count("search_teams"), 1)
        standings_kwargs = next(kwargs for name, kwargs in client.calls if name == "get_standings")
        self.assertNotIn("team_id", standings_kwargs)


    async def test_current_football_commands_call_expected_api_wrappers(self) -> None:
        client = _FakeFootballClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog._run_football_live(ctx, "ligamx")
        await cog.football_today.callback(cog, ctx, "ligamx")
        await cog.football_next.callback(cog, ctx, "ligamx", target="2")
        self.assertIn(("get_next_fixtures", {"league_id": 262, "season": 2026, "next_count": 2, "team_id": None}), client.calls)
        await cog.football_last.callback(cog, ctx, "ligamx", team="Mexico")
        from io import BytesIO
        from PIL import Image

        badge = BytesIO()
        Image.new("RGBA", (32, 32), "white").save(badge, format="PNG")
        logo_url = "https://media.api-sports.io/football/teams/1.png"
        cog._table_logo_cache[logo_url] = badge.getvalue()
        rows = [{"rank": i + 1, "team": {"id": i + 1, "name": f"Club {i + 1}"},
                 "group": "Group A" if i < 25 else "Group B", "points": 30 - i,
                 "all": {"played": 10}, "goalsDiff": -i} for i in range(27)]
        rows[0]["team"]["logo"] = logo_url
        rows[0].update({"form": "WDLWW", "description": "Provider qualification zone",
                        "home": {"played": 0, "win": 0, "draw": 0, "lose": 0, "goals": {"for": 0, "against": 0}},
                        "away": {"played": 10, "win": 4, "draw": 2, "lose": 4, "goals": {"for": 12, "against": 15}}})
        standings = [{"league": {"id": 262, "name": "Provider League", "logo": logo_url, "season": 2026,
                                  "standings": [rows[:25], rows[25:]]}}]

        def interaction():
            return SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                   followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

        start = len(ctx.sent)
        with patch.object(client, "get_standings", side_effect=_async_return(standings)) as fetch:
            await cog.football_table.callback(cog, ctx, "ligamx")
            self.assertEqual(len(ctx.sent), start + 1)
            card = ctx.sent[-1]
            view = card["view"]
            self.assertNotIn("embed", card)
            self.assertEqual(len(card["files"]), 1)
            with Image.open(card["files"][0].fp) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 800)
            self.assertEqual(len(view.pages["compact"]), 3)
            self.assertIn("Club 25", football_cards.page_text(view.pages["compact"][1]))
            self.assertIn("Group B", football_cards.page_text(view.pages["compact"][2]))
            self.assertIn("Club 27", football_cards.page_text(view.pages["compact"][2]))
            compact = view.current
            expected = football_cards.render_table(compact["table"], league=compact["league"], season=2026,
                group=compact["group"], lang="en", logos={logo_url: badge.getvalue()}, page=1, pages=3)
            self.assertEqual(card["files"][0].fp.getvalue(), expected.getvalue())
            for key in ("detailed", "home", "away", "compact"):
                view.section_menu._values = [key]
                await view._update(interaction(), "section")
            self.assertEqual(fetch.await_count, 1)
            detail = football_cards.page_text(view.pages["detailed"][0])
            self.assertIn("WDLWW", detail)
            self.assertIn("Provider qualification zone", detail)
            self.assertIn("Goals for: 0", football_cards.page_text(view.pages["home"][0]))
            self.assertIn("Goals against: 15", football_cards.page_text(view.pages["away"][0]))
            self.assertIn("Overall table order", view.pages["home"][0]["subtitle"])
            self.assertIn("Goals for: N/A", football_cards.page_text(view.pages["home"][1]))
            await view._update(interaction(), "refresh")
            self.assertEqual(fetch.await_count, 2)
        with self.assertLogs(level="ERROR"), patch(
            "services.football_cards.render_table", side_effect=OSError("render unavailable")
        ):
            await cog.football_table.callback(cog, ctx, "ligamx")
        self.assertIn("Mexico", ctx.sent[-1]["content"])
        self.assertFalse(ctx.sent[-1]["files"])
        ctx.bot_permissions = SimpleNamespace(attach_files=False)
        await cog.football_table.callback(cog, ctx, "ligamx")
        self.assertIn("Mexico", ctx.sent[-1]["content"])
        await cog.football_team.callback(cog, ctx, "ligamx", team="Mexico")
        await cog.football_top.callback(cog, ctx, "scorers", "ligamx")
        await cog.football_match.callback(cog, ctx, team="Mexico", when="next")
        await cog.football_schedule.callback(cog, ctx, team="Mexico", date="next", league="ligamx")
        await cog.football_player.callback(cog, ctx, "haaland")
        await cog.football_injuries.callback(cog, ctx, "real madrid")
        self.assertIn("No injuries are recorded", ctx.sent[-1]["content"])
        self.assertIn("2026", ctx.sent[-1]["content"])
        await cog.football_transfers.callback(cog, ctx, "america")
        self.assertIn("No transfers are recorded", ctx.sent[-1]["content"])
        await cog.football_h2h.callback(cog, ctx, "argentina", "switzerland")
        await cog.football_top.callback(cog, ctx, "assists", "ligamx")

        fixture = _fixture()
        fixture["fixture"]["status"] = {"short": "PEN", "long": "After penalties", "elapsed": 120}
        fixture["league"].update({"id": 262, "season": 2026})
        fixture["score"] = {"halftime": {"home": 0, "away": 0}, "fulltime": {"home": 1, "away": 1},
                            "extratime": {"home": 2, "away": 2}, "penalty": {"home": 5, "away": 4}}
        fixture["goals"] = {"home": 2, "away": 2}
        fixture["statistics"] = [{"team": {"id": 1, "name": "Mexico"}, "statistics": [
            {"type": f"Metric {index}", "value": 0 if index == 0 else None if index == 1 else index}
            for index in range(17)]}, {"team": {"id": 2, "name": "France"}, "statistics": [
            {"type": "Metric 0", "value": 2}]}]
        fixture["events"] = [{"time": {"elapsed": 40 + index, "extra": 2 if index == 25 else None},
                              "team": {"id": 1, "name": "Mexico"}, "player": {"name": f"Event Player {index}"},
                              "type": "Card", "detail": "Yellow Card"} for index in range(26)]
        grid = [(1,1), (2,1), (2,2), (2,3), (2,4), (3,1), (3,2), (3,3), (4,1), (4,2), (4,3)]
        fixture["lineups"] = [{"team": {"id": 1, "name": "Mexico"}, "coach": {"name": "Coach"}, "formation": "4-3-3",
            "startXI": [{"player": {"name": f"Starter Full Name {index}", "number": index+1, "pos": "M", "grid": f"{x}:{y}"}}
                        for index,(x,y) in enumerate(grid)],
            "substitutes": [{"player": {"name": f"Bench Full Name {index}", "number": index+12, "pos": "F"}}
                            for index in range(13)]}]
        fixture["players"] = [{"team": {"id": 1, "name": "Mexico"}, "players": [
            {"player": {"id": 9, "name": "Performance Player"}, "statistics": [
                {"games": {"minutes": 90, "rating": None}, "goals": {"total": 0, "assists": 1}, "custom": {"metric": 42}}]}]}]
        ctx.bot_permissions.attach_files = True

        def interaction():
            return SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                   followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

        with patch.object(client, "get_last_fixtures", side_effect=_async_return([fixture])) as fetch:
            await cog.football_match.callback(cog, ctx, team="Mexico", when="last")
        view = ctx.sent[-1]["view"]
        self.assertNotIn("embed", ctx.sent[-1])
        self.assertTrue(ctx.sent[-1]["files"][0].filename.endswith(".png"))
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(fetch.call_args.kwargs["team_id"], 1)
        self.assertEqual(fetch.call_args.kwargs["last_count"], 10)
        overview_text = "\n".join(football_cards.page_text(page) for page in view.pages["overview"])
        self.assertIn("Penalty shootout: 5 - 4", overview_text)
        self.assertIn("After extra time: 2 - 2", overview_text)
        calls = len(client.calls)
        for key in ("statistics", "events", "lineups", "players", "periods"):
            view.section_menu._values = [key]
            await view._update(interaction(), "section")
            self.assertTrue(view.pages[key])
        self.assertEqual(len(client.calls), calls)
        stats = view.pages["statistics"]
        self.assertEqual(len(stats), 3)
        self.assertEqual(stats[0]["comparison"][0], ("Metric 0", "0", "2"))
        self.assertEqual(stats[0]["comparison"][1], ("Metric 1", "N/A", "N/A"))
        provider_labels = ["expected_goals", "Passes accurate", "Shots insidebox", "Shots outsidebox", "Blocked Shots", "Free Kicks"]
        raw_stats = [{"team":{"id":1},"statistics":[{"type":label,"value":0 if index == 4 else None}
                     for index,label in enumerate(provider_labels)]}]
        translated = football_cards.match_statistics_pages(build_match_data(fixture,time_scope="fixture_id"),raw_stats,
                                                           "Partido completo",lang="es")[0]
        self.assertEqual([row[0] for row in translated["comparison"]],
                         ["Goles esperados (xG)","Pases completados","Tiros dentro del área", "Tiros fuera del área","Tiros bloqueados","Tiros libres"])
        self.assertEqual(translated["comparison"][4][1:],("0","N/A"))
        self.assertIn("Tiros libres",football_cards.page_text(translated))
        events = "\n".join(football_cards.page_text(page) for page in view.pages["events"])
        self.assertIn("Event Player 25", events)
        self.assertIn("65+2'", events)
        lineups = view.pages["lineups"]
        self.assertTrue(lineups[0]["pitch"])
        self.assertIn("Bench Full Name 12", "\n".join(football_cards.page_text(page) for page in lineups))
        fixture["lineups"][0]["startXI"][0]["player"]["grid"] = None
        fallback = football_cards.match_pages(build_match_data(fixture,time_scope="fixture_id",lineups=fixture["lineups"]), "lineups",lang="en")
        self.assertTrue(all(not page.get("pitch") for page in fallback))
        self.assertIn("Starter Full Name 0", football_cards.page_text(fallback[0]))
        performance = "\n".join(football_cards.page_text(page) for page in view.pages["players"])
        self.assertIn("Goals: 0", performance)
        self.assertIn("Rating: N/A", performance)
        self.assertIn("Custom Metric: 42", performance)
        fixture["league"]["round"] = "Regular Season - 12"
        fixture["players"][0]["players"][0]["statistics"][0].update({
            "games": {"minutes": 90, "position": "Attacker", "captain": True, "rating": None},
            "shots": {"on": 0}, "passes": {"key": 2}, "penalty": {"missed": 0},
        })
        localized = build_match_data(fixture, time_scope="fixture_id", events=fixture["events"],
                                     lineups=fixture["lineups"], players=fixture["players"])
        text = lambda section: "\n".join(football_cards.page_text(page)
            for page in football_cards.match_pages(localized, section, lang="es"))
        self.assertIn("Tarjeta amarilla", text("events"))
        self.assertIn("Temporada regular - 12", text("events"))
        self.assertIn("3 / MED", text("lineups"))
        for phrase in ("Posición: Delantero", "Capitán: Sí", "Tiros a puerta: 0", "Pases clave: 2",
                       "Penales fallados: 0", "Custom Metric: 42", "Valoracion: N/A"):
            self.assertIn(phrase, text("players"))
        player_row = _player()
        player_row["player"]["position"] = "Goalkeeper"
        player_row["statistics"][0]["games"]["position"] = "Goalkeeper"
        player_pages = football_cards.player_pages(player_row, season=2026, lang="es")
        self.assertIn("Portero", player_pages["statistics"][0]["subtitle"])
        self.assertIn("Portero", football_cards.page_text(player_pages["profile"][0]))
        view.section_menu._values = ["statistics"]
        await view._update(interaction(), "section")
        await view._update(interaction(), "next")
        self.assertEqual(view.index, 1)
        self.assertEqual(len(client.calls), calls)
        del fixture["lineups"]
        with patch.object(client,"get_next_fixtures",side_effect=_async_return([fixture])), patch.object(
            client,"get_league_by_id",side_effect=_async_return([{"league":{"id":262},"seasons":[
                {"year":2026,"coverage":{"fixtures":{"lineups":False}}}]}])
        ), patch.object(client,"get_fixture_lineups") as lineups_fetch:
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            pane = ctx.sent[-1]["view"]
            pane.section_menu._values = ["lineups"]
            await pane._update(interaction(), "section")
        self.assertIn("does not cover", pane.current["text"])
        lineups_fetch.assert_not_awaited()
        with patch.object(client,"get_fixture_lineups",side_effect=_async_return([])):
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            pane = ctx.sent[-1]["view"]
            pane.section_menu._values = ["lineups"]
            await pane._update(interaction(), "section")
        self.assertIn("No lineups have been published", pane.current["text"])
        with self.assertLogs(level="ERROR"), patch.object(client,"get_fixture_statistics",side_effect=FootballApiError("unavailable")):
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            pane = ctx.sent[-1]["view"]
            pane.section_menu._values = ["statistics"]
            await pane._update(interaction(), "section")
        self.assertIn("Could not load this section", pane.current["text"])
        wrong_team = [{"team":{"id":999,"name":"Mexico"},"statistics":[{"type":"Wrong","value":999}]}]
        self.assertEqual(football_cards.match_statistics_pages(build_match_data(fixture,time_scope="fixture_id"),
                         wrong_team,"Full match",lang="en"),[])

        called = {name for name, _kwargs in client.calls}
        expected = {
            "get_live_fixtures",
            "get_fixtures_on_date",
            "get_next_fixtures",
            "get_last_fixtures",
            "get_standings",
            "search_teams",
            "get_top_scorers",
            "search_player_profiles",
            "get_injuries",
            "get_transfers",
            "get_head_to_head",
            "get_top_assists",
        }
        self.assertTrue(expected.issubset(called), expected - called)

        ranked = _player()
        ranked["statistics"][0]["cards"] = {"yellow": 7, "red": 0}
        for category, endpoint, value in [("assists", "get_top_assists", 2),
                                          ("yellowcards", "get_top_yellow_cards", 7),
                                          ("redcards", "get_top_red_cards", 0)]:
            with patch.object(client, endpoint, side_effect=_async_return([ranked])):
                await cog.football_top.callback(cog, ctx, category, "ligamx")
            current = ctx.sent[-1]["view"].current
            self.assertEqual(current["entries"][0]["value"], value)
            self.assertNotIn("embed", ctx.sent[-1])
        ranking = []
        for index in range(21):
            row = _player(name=f"Full Player Name {index}", player_id=index + 1)
            row["statistics"][0]["goals"].update({"total": index, "assists": None if index == 20 else 0})
            ranking.append(row)
        with patch.object(client, "get_top_scorers", side_effect=_async_return(ranking)) as fetch:
            await cog.football_top.callback(cog, ctx, "scorers", "ligamx")
            view = ctx.sent[-1]["view"]
            self.assertEqual(len(view.pages["ranking"]), 5)
            for _ in range(4):
                await view._update(interaction(), "next")
            self.assertIn("Full Player Name 20", football_cards.page_text(view.current))
            self.assertEqual(view.current["entries"][0]["value"], 20)
            self.assertEqual(fetch.await_count, 1)
            private = interaction()
            await view.text.callback(private)
            self.assertTrue(private.followup.send.call_args.kwargs["ephemeral"])
        multiple = _player()
        multiple["statistics"].append({"team": {"name": "Second Club"}, "goals": {"assists": None}})
        pages = football_cards.leaderboard_pages([multiple], league={"name": "Competition"}, season=2026,
                                                  focus="top_assists", lang="es")
        self.assertEqual(len(pages[0]["entries"]), 2)
        self.assertIsNone(pages[0]["entries"][1]["value"])
        self.assertIn("Second Club", football_cards.page_text(pages[0]))
        self.assertIn("Asistencias: N/A", football_cards.page_text(pages[0]))

        from copy import deepcopy
        meetings = []
        for index, (status, reverse, goals, penalty) in enumerate([
            ("FT", False, (2, 0), None), ("FT", True, (3, 1), None),
            ("PEN", False, (1, 1), (4, 5)), ("AET", True, (0, 1), None),
            ("NS", False, (None, None), None), ("FT", False, (None, None), None),
            ("PEN", True, (0, 0), None),
        ]):
            item = deepcopy(_fixture())
            item["fixture"].update({"id": 200 + index, "status": {"short": status}})
            if reverse:
                item["teams"]["home"], item["teams"]["away"] = item["teams"]["away"], item["teams"]["home"]
            item["goals"] = dict(zip(("home", "away"), goals))
            if penalty is not None:
                item["score"] = {"penalty": dict(zip(("home", "away"), penalty))}
            meetings.append(item)
        with patch.object(client, "get_head_to_head", side_effect=_async_return(meetings)) as fetch:
            await cog.football_h2h.callback(cog, ctx, "argentina", "switzerland", matches=12)
            self.assertEqual(fetch.call_args.kwargs["last"], 12)
            view = ctx.sent[-1]["view"]
            self.assertEqual(view.section, "comparison")
            totals = dict(view.current["rows"])
            self.assertEqual(totals["Returned meetings"], 7)
            self.assertEqual(totals["Finished meetings with scores"], 5)
            self.assertEqual(totals["Excluded from goal comparison"], 2)
            self.assertEqual(totals["Wins: Argentina"], 2)
            self.assertEqual(totals["Wins: Switzerland"], 1)
            self.assertEqual(totals["Draws before shootouts"], 2)
            self.assertEqual(totals["Goals: Argentina"], 5)
            self.assertEqual(totals["Goals: Switzerland"], 4)
            self.assertEqual(totals["Shootout wins: Switzerland"], 1)
            self.assertEqual(totals["Shootouts without results"], 1)
            self.assertEqual(len(view.pages["fixtures"]), 2)
            view.section_menu._values = ["fixtures"]
            await view._update(interaction(), "section")
            await view._update(interaction(), "next")
            self.assertEqual(view.fixture_menu.options[0].value, "205")
            view.fixture_menu._values = ["205"]
            await view._update(interaction(), "fixture")
            view.section_menu._values = ["comparison"]
            await view._update(interaction(), "section")
            self.assertEqual(dict(view.current["rows"]), totals)
            self.assertEqual(fetch.await_count, 1)
            await view._update(interaction(), "refresh")
            self.assertEqual(fetch.await_count, 2)
            self.assertTrue(view.pending_fixture)
            self.assertFalse(view.refresh_button.disabled)
            schema = {item["name"]: item for item in cog.football_h2h.app_command.to_dict(None)["options"]}
            self.assertEqual(set(schema), {"team", "opponent", "league", "matches"})
            self.assertEqual(schema["matches"]["max_value"], 50)
            self.assertEqual(schema["matches"]["description"], "Matches that will be looked up. Default 10")
            fetch.side_effect = _async_return([])
            await view._update(interaction(), "refresh")
            self.assertIn("No meetings are recorded", view.current["text"])
            self.assertTrue(view.fixture_menu.disabled)
            self.assertFalse(view.refresh_button.disabled)
        coverage = [{"league": {"id": 262}, "seasons": [
            {"year": 2025, "coverage": {"top_assists": True}},
            {"year": 2026, "coverage": {"top_assists": False}}]}]
        with patch.object(client, "get_league_by_id", side_effect=_async_return(coverage)), patch.object(
            client, "get_top_assists", side_effect=AssertionError("unsupported scope must not fetch ranking")
        ):
            await cog.football_top.callback(cog, ctx, "assists", "ligamx")
        self.assertIn("does not cover", ctx.sent[-1]["content"])
        transfer = {"player": {"name": "Example Player"}, "transfers": [
            {"date": "2026-10-01", "type": "Loan", "teams": {
                "out": {"name": "Old FC"}, "in": {"name": "New FC"}}}]}
        with patch.object(client, "get_transfers", side_effect=_async_return([transfer])):
            await cog.football_transfers.callback(cog, ctx, "america")
        self.assertIn("From: Old FC", football_cards.page_text(ctx.sent[-1]["view"].current))
        self.assertIn("To: New FC", football_cards.page_text(ctx.sent[-1]["view"].current))
        self.assertIn("2026-10-01", football_cards.page_text(ctx.sent[-1]["view"].current))
        injury = {"player": {"name": "Example Player", "type": "Missing Fixture", "reason": "Knee Injury"}}
        with patch.object(client, "get_injuries", side_effect=_async_return([injury])):
            await cog.football_injuries.callback(cog, ctx, "america")
        self.assertIn("Knee Injury", football_cards.page_text(ctx.sent[-1]["view"].current))
        profile = {"player": {"id": 9, "name": "Erling Haaland", "position": "Attacker"}}
        with patch.object(client, "search_player_profiles", side_effect=_async_return([profile])), patch.object(
            client, "get_player_stats", side_effect=_async_return([])
        ):
            await cog.football_player.callback(cog, ctx, "haaland")
        view = ctx.sent[-1]["view"]
        self.assertIn("Position: Attacker", football_cards.page_text(view.current))
        self.assertIn("No season statistics", football_cards.page_text(view.current))
        self.assertEqual(view.pages["profile"][0]["subtitle"], "Player profile / Latest registered season 2026")
        actual_zero = {"player": {"name": "Example"}, "statistics": [{"goals": {"total": 0}}]}
        self.assertIn("Goals: 0", football_cards.page_text(football_cards.player_pages(
            actual_zero, season=2026, lang="en")["statistics"][0]))
        live_row = {"fixture": {"id": 10}, "status": {}, "odds": [{"name": "Total Goals", "values": [
            {"value": "Over", "handicap": "2.5", "odd": "1.85", "suspended": False},
            {"value": "Under", "handicap": "2.5", "odd": "2.10", "suspended": True}]}]}
        for blocked in (False, True):
            live_row["status"]["blocked"] = blocked
            current = _fixture()
            current["fixture"]["status"]["short"] = "1H"
            current["league"].update({"id": 262, "season": 2026})
            with patch.object(client, "get_live_odds", side_effect=_async_return([live_row])) as fetch, patch.object(
                client, "get_live_fixtures", side_effect=_async_return([current])
            ):
                await cog.football_match.callback(cog, ctx, team="Mexico", when="live")
                pane = ctx.sent[-1]["view"]
                pane.section_menu._values = ["odds"]
                await pane._update(interaction(), "section")
            self.assertIsNone(fetch.call_args.args[0].season)
            description = "\n".join(football_cards.page_text(page) for page in ctx.sent[-1]["view"].pages["odds"])
            self.assertNotIn("2.10", description)
            if blocked:
                self.assertNotIn("1.85", description)
            else:
                self.assertIn("Over / Handicap: 2.5: 1.85", description)
        pre_match = [{"fixture": {"id": 10}, "bookmakers": [{"name": "Example Book", "bets": [
            {"name": "Match Winner", "values": [{"value": "Home", "odd": "1.90"}]}]}]}]
        pre_text = football_cards.page_text(football_cards.odds_pages(pre_match, fixture=_fixture(), lang="en", live=False)[0])
        self.assertIn("Example Book / Match Winner", pre_text)
        self.assertIn("Home: 1.90", pre_text)

        selected = deepcopy(_fixture())
        selected["league"].update({"id": 262, "season": 2026})
        other = deepcopy(selected)
        other["fixture"]["id"] = 11
        ctx.bot_permissions.attach_files = False
        with patch.object(client, "get_next_fixtures", side_effect=_async_return([selected, other])), patch.object(
            client, "get_predictions", wraps=client.get_predictions
        ) as forecast, patch.object(client, "get_team_statistics", wraps=client.get_team_statistics) as team_stats:
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            view = ctx.sent[-1]["view"]
            self.assertTrue(view.pending_fixture)
            forecast.assert_not_called()
            team_stats.assert_not_called()
            view.fixture_menu._values = ["11"]
            await view._update(interaction(), "fixture")
            self.assertEqual(view.section, "overview")
            view.section_menu._values = ["preview"]
            await view._update(interaction(), "section")
            self.assertIn("Available recent meetings", football_cards.page_text(view.current))
            forecast.assert_not_called()
            for key in ("predictions", "season_comparison", "preview", "predictions"):
                view.section_menu._values = [key]
                await view._update(interaction(), "section")
            self.assertEqual(forecast.await_count, 1)
            self.assertEqual(forecast.call_args.args[0].fixture_id, 11)
            self.assertEqual(team_stats.await_count, 2)
            self.assertTrue(all(call.kwargs["request"].season.value == 2026 for call in team_stats.call_args_list))
            self.assertIn("Provider forecast", football_cards.page_text(view.current))
        unsupported = [{"league": {"id": 262}, "seasons": [
            {"year": 2025, "coverage": {"predictions": True}},
            {"year": 2026, "coverage": {"predictions": False}}]}]
        with patch.object(client, "get_next_fixtures", side_effect=_async_return([selected])), patch.object(
            client, "get_league_by_id", side_effect=_async_return(unsupported)
        ), patch.object(client, "get_predictions", side_effect=AssertionError("uncovered forecast must not fetch")) as fetch:
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            view = ctx.sent[-1]["view"]
            view.section_menu._values = ["predictions"]
            await view._update(interaction(), "section")
            self.assertIn("does not cover", view.current["text"])
            fetch.assert_not_called()
        with patch.object(client, "get_standings", side_effect=FootballApiError("unavailable")), patch.object(
            client, "get_head_to_head", side_effect=FootballApiError("unavailable")
        ):
            partial = await FootballOperationService(client).get_fixture_preview(build_match_data(selected, time_scope="fixture_id"))
        partial_page = football_cards.preview_pages(build_match_data(selected, time_scope="fixture_id"), partial, lang="en")[0]
        self.assertIn("fixture details remain valid", football_cards.page_text(partial_page))
        self.assertIn("Available recent meetings: N/A", football_cards.page_text(partial_page))

        from services.api_football import FootballApiPage
        first_odds = [{"fixture": {"id": 10}, "bookmakers": [{"id": 4, "name": "Complete Book", "bets": [
            {"name": "Long Market Name", "values": [{"value": f"Selection {index}", "odd": str(index)} for index in range(14)]}]}]}]
        second_odds = [{"fixture": {"id": 10}, "bookmakers": [{"name": "Last Book", "bets": [
            {"name": "Final Market", "values": [{"value": "Final selection", "odd": "1.99"}]}]}]}]
        with patch.object(client, "get_next_fixtures", side_effect=_async_return([selected])), patch.object(
            client, "get_odds_page", create=True, new_callable=AsyncMock,
            side_effect=[FootballApiPage(first_odds, 1, 2), FootballApiPage(second_odds, 2, 2)]
        ) as fetch:
            await cog.football_match.callback(cog, ctx, "Mexico", when="next")
            view = ctx.sent[-1]["view"]
            view.section_menu._values = ["odds"]
            await view._update(interaction(), "section")
            self.assertEqual(len(view.pages["odds"]), 3)
            for _ in range(2):
                await view._update(interaction(), "next")
            self.assertEqual(fetch.await_count, 1)
            self.assertIn("Selection 13", football_cards.page_text(view.current))
            await view._update(interaction(), "next")
            self.assertEqual(fetch.await_count, 2)
            self.assertEqual(fetch.call_args.args[0].page, 2)
            self.assertIn("Last Book", football_cards.page_text(view.current))
            self.assertIn("Final selection: 1.99", football_cards.page_text(view.current))
            self.assertTrue(view.next_page.disabled)
            await view._update(interaction(), "previous")
            await view._update(interaction(), "next")
            self.assertEqual(fetch.await_count, 2)

        records = [{"player": {"name": f"Injured Player {index}", "reason": "Provider reason"},
                    "fixture": {"date": "2026-10-29"}, "league": {"name": "League"}} for index in range(11)]
        with patch.object(client, "get_injuries", side_effect=_async_return(records)) as fetch:
            await cog.football_injuries.callback(cog, ctx, "Mexico")
            view = ctx.sent[-1]["view"]
            self.assertEqual(len(view.pages["records"]), 4)
            for _ in range(3):
                await view._update(interaction(), "next")
            self.assertIn("Injured Player 10", football_cards.page_text(view.current))
            self.assertEqual(fetch.await_count, 1)
        movements = [{"player": {"name": "Player"}, "transfers": [
            {"date": f"2026-10-{index+1:02d}", "type": "Loan", "teams": {"in": {"name": "New Club"}, "out": {"name": f"Club {index}"}}}
            for index in range(13)]}]
        text = "\n".join(football_cards.page_text(page) for page in football_cards.record_pages(
            movements, team={"name": "Team"}, season=2026, focus="transfers", lang="en"))
        self.assertIn("Club 12", text)
        self.assertIn("Club 0", text)

        catalogue = [{"name": f"Country with a complete long name {index}", "code": f"C{index}"} for index in range(80)]
        with patch.object(client, "get_countries", create=True, new_callable=AsyncMock, side_effect=_async_return(catalogue)) as fetch:
            await cog.football_countries.callback(cog, ctx)
            view = ctx.sent[-1]["view"]
            self.assertGreater(len(view.pages["reference"]), 1)
            text = "\n".join(football_cards.page_text(page) for page in view.pages["reference"])
            self.assertIn("long name 79", text)
            for _ in range(len(view.pages["reference"]) - 1):
                await view._update(interaction(), "next")
            self.assertEqual(fetch.await_count, 1)
            self.assertFalse(ctx.sent[-1]["files"])
        commands = {command.name for command in cog.football.app_command.commands}
        self.assertTrue({"odds", "live-odds", "preview", "lineup", "stats", "summary", "scorers"}.isdisjoint(commands))
        self.assertTrue({"match", "top", "schedule", "next", "last", "injuries", "transfers", "h2h"}.issubset(commands))
        for command, args in [(cog.football_injuries, ("Missing",)),
                              (cog.football_transfers, ("Missing",)), (cog.football_h2h, ("Missing", "Other"))]:
            start = len(ctx.sent)
            with patch.object(client, "search_teams", side_effect=_async_return([])):
                await command.callback(cog, ctx, *args)
            self.assertEqual(len(ctx.sent), start + 1)
            self.assertTrue(ctx.sent[-1]["content"])
        finished = _fixture()
        finished["fixture"]["status"] = {"short": "FT"}
        with patch.object(client, "get_next_fixtures", side_effect=_async_return([])), patch.object(
            client, "get_last_fixtures", side_effect=_async_return([finished])
        ):
            await cog.football_team.callback(cog, ctx, "ligamx", team="Mexico")
        self.assertIn("Last match:", football_cards.page_text(ctx.sent[-1]["view"].current))
        with patch.object(client, "get_top_assists", side_effect=_async_return([])):
            await cog.football_top.callback(cog, ctx, "assists", "ligamx")
        self.assertIn("No ranking entries are recorded", ctx.sent[-1]["content"])
        self.assertNotIn("no yellow cards", ctx.sent[-1]["content"].lower())
        with patch.object(client, "get_top_scorers", side_effect=_async_return([])):
            await cog.football_top.callback(cog, ctx, "scorers", "ligamx")
        self.assertIn("No scorer entries are recorded", ctx.sent[-1]["content"])
        with patch.object(client, "get_head_to_head", side_effect=_async_return([])):
            await cog.football_h2h.callback(cog, ctx, "argentina", "switzerland")
        self.assertIn("No meetings are recorded", ctx.sent[-1]["content"])
        with patch.object(client, "get_next_fixtures", side_effect=_async_return([])):
            await cog.football_schedule.callback(cog, ctx, team="Mexico", date="next", league="ligamx")
        self.assertIn("No upcoming fixtures are listed", ctx.sent[-1]["content"])
        with patch.object(client, "get_injuries", side_effect=FootballApiError("provider unavailable")):
            await cog.football_injuries.callback(cog, ctx, "america")
        self.assertNotIn("No injuries are recorded", ctx.sent[-1]["content"])


    async def test_player_identity_uses_profile_fields_before_stats(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.profile_calls: list[dict[str, object]] = []
                self.stats_calls: list[dict[str, object]] = []

            async def search_player_profiles(self, request):
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self.profile_calls.append(kwargs)
                return [
                    {"player": {"id": 129718, "name": "J. Bellingham", "firstname": "Jude", "lastname": "Bellingham"}, "statistics": []},
                    {"player": {"id": 321005, "name": "J. Bellingham", "firstname": "Jobe", "lastname": "Bellingham"}, "statistics": []},
                ]

            async def get_player_stats(self, request):
                kwargs = _player_stats_kwargs(request)
                self.stats_calls.append(kwargs)
                raise AssertionError("identity resolution must not fetch stats before selecting player_id")

        client = _Client()
        lookup = await football_resolver.resolve_player(client, _player_slot("Jude Bellingham"))

        self.assertEqual(client.profile_calls, [{"search": "Bellingham"}])
        self.assertEqual(client.stats_calls, [])
        self.assertEqual(lookup.resolution.selected["player"]["id"], 129718)

        client = _FakeFootballClient()
        operation = compile_football_operation("FOOTBALL_PLAYER_QUERY", "Erling Haaland",
            {"data_focus": "player_recent_stats", "player_candidates": ["Erling Haaland"]})
        record = {"team": {"id": 5, "name": "Club A"},
                  "league": {"id": 262, "name": "League A", "season": 2025},
                  "games": {"appearences": 0, "minutes": None}, "goals": {"total": 0}}
        second = {**record, "team": {"id": 6, "name": "Club B"}}
        rows = [{"player": {"id": 9}, "statistics": [record, second,
                 {**record, "league": {"id": 262, "season": 2024}},
                 {**record, "league": {"id": 999, "season": 2025}}]},
                {"player": {"id": 99}, "statistics": [record]}]
        metadata = [{"league": {"id": 262}, "seasons": [
            {"year": 2024, "coverage": {"players": False}},
            {"year": 2025, "coverage": {"players": True}}]}]
        with patch.object(client, "get_player_stats", side_effect=_async_return(rows)) as fetch, patch.object(
            client, "get_league_by_id", side_effect=_async_return(metadata)
        ):
            result = await FootballOperationService(client).execute(operation, league_id=262, season=2025)
        self.assertEqual(fetch.call_args.args[0].player_id, 9)
        self.assertEqual(_player_stats_kwargs(fetch.call_args.args[0])["season"], 2025)
        self.assertEqual(result.player_context_row["statistics"], [record, second])
        self.assertIsNone(result.football_entity_context["team_id"])
        self.assertNotIn("get_player_seasons", [name for name, _ in client.calls])
        cards = football_cards.player_pages(result.player_context_row, season=2025, lang="en")["statistics"]
        self.assertEqual(len(cards), 2)
        self.assertIn("Club B", cards[1]["subtitle"])
        self.assertIn("Goals: 0", football_cards.page_text(cards[0]))
        self.assertIn("Minutes: N/A", football_cards.page_text(cards[0]))
        for year in (2025, None):
            client.calls.clear()
            with patch.object(client, "get_player_stats", side_effect=_async_return([])) as fetch:
                result = await FootballOperationService(client).execute(operation, league_id=None, season=year)
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(_player_stats_kwargs(fetch.call_args.args[0])["season"], year or 2026)
            self.assertEqual(result.player_context_row["player"]["id"], 9)
            self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        for supported, error in ((False, False), (True, True)):
            metadata[0]["seasons"][1]["coverage"]["players"] = supported
            with patch.object(client, "get_league_by_id", side_effect=_async_return(metadata)), patch.object(
                client, "get_player_stats", side_effect=FootballApiError("unavailable") if error else _async_return([])
            ) as fetch:
                result = await FootballOperationService(client).execute(operation, league_id=262, season=2025)
            self.assertEqual(result.player_context_row["player"]["id"], 9)
            self.assertEqual(result.player_context_row["statistics"], [])
            self.assertEqual(fetch.await_count, int(supported))
            self.assertEqual(result.outcome, FootballOutcome.PROVIDER_FAILURE if error else FootballOutcome.UNSUPPORTED_BY_COVERAGE)


    async def test_card_navigation_loads_sections_once_and_expires(self) -> None:
        import discord
        client = _FakeFootballClient()
        ctx = _FakeCtx(client)
        ctx.bot_permissions = SimpleNamespace(attach_files=False)
        cog = FootballCog(ctx.bot)
        profile = {"team": {"id": 1, "name": "Mexico"}, "venue": {
            "id": 7, "name": "Known Venue", "city": "City", "address": "Long address", "capacity": 0}}
        with patch.object(client, "search_teams", side_effect=_async_return([profile])):
            await cog.football_team.callback(cog, ctx, "ligamx", team="Mexico", season=2025)
        view = ctx.sent[-1]["view"]
        self.assertIsInstance(view, FootballCardView)
        self.assertNotIn("get_team_statistics", [name for name, _ in client.calls])
        self.assertNotIn("get_current_season", [name for name, _ in client.calls])
        self.assertFalse({"get_player_squads", "get_team_seasons", "search_coaches"} & {name for name, _ in client.calls})

        def interaction(user=42):
            return SimpleNamespace(user=SimpleNamespace(id=user),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

        other = interaction(99)
        await view._update(other, "next")
        other.response.send_message.assert_awaited_once()
        other.edit_original_response.assert_not_awaited()
        view.section_menu._values = ["season"]
        await view._update(interaction(), "section")
        self.assertEqual(len(view.pages["season"]), 3)
        count = len(client.calls)
        await asyncio.gather(view._update(interaction(), "next"), view._update(interaction(), "next"))
        self.assertEqual(view.index, 2)
        self.assertIn("Away", view.current["subtitle"])
        view.section_menu._values = ["overview"]
        await view._update(interaction(), "section")
        view.section_menu._values = ["season"]
        await view._update(interaction(), "section")
        self.assertEqual(len(client.calls), count)
        self.assertTrue(all(kw["season"] == 2025 for name, kw in client.calls if name == "get_team_statistics"))
        squad_rows = [{"team": {"id": 999}, "players": [{"name": "Wrong squad"}]},
                      {"team": {"id": 1}, "players": [{"id": index + 1, "name": f"Squad Player {index}",
                        "number": index, "age": 0, "position": "Attacker"} for index in range(7)]}]
        coach_rows = [{"id": 4, "name": "Wrong coach", "team": {"id": 999}},
                      {"id": 3, "name": "Complete Coach Name", "age": 0, "team": {"id": 1, "name": "Mexico"},
                       "career": [{"team": {"id": 1, "name": "Mexico"}, "start": "2024-01-01", "end": None}]}]
        identity_calls = [item for item in client.calls if item[0] == "search_teams"]
        with patch.object(client, "get_player_squads", side_effect=_async_return(squad_rows)) as squad, patch.object(
            client, "get_team_seasons", create=True, new_callable=AsyncMock,
            side_effect=_async_return([{"season": year} for year in range(2010, 2027)])
        ) as seasons, patch.object(client, "search_coaches", side_effect=_async_return(coach_rows)) as coach, patch.object(
            client, "search_venues", side_effect=AssertionError("embedded venue must not search by club name")
        ) as venue:
            for section in ("team_squad", "team_seasons", "team_coach", "team_venue"):
                view.section_menu._values = [section]
                await view._update(interaction(), "section")
            self.assertIn("Capacity: 0", football_cards.page_text(view.current))
            venue.assert_not_called()
            self.assertEqual(squad.call_args.args[0].team_id, 1)
            self.assertEqual(seasons.call_args.args[0].team_id, 1)
            self.assertEqual(coach.call_args.args[0].team_id, 1)
            text = "\n".join(football_cards.page_text(page) for page in view.pages["team_coach"])
            self.assertIn("Complete Coach Name", text)
            self.assertNotIn("Wrong coach", text)
            text = "\n".join(football_cards.page_text(page) for page in view.pages["team_seasons"])
            self.assertIn("2010", text)
            self.assertIn("2026", text)
            self.assertEqual(len(view.pages["team_seasons"]), 2)
            view.section_menu._values = ["team_squad"]
            await view._update(interaction(), "section")
            self.assertIn("Current squad", view.current["subtitle"])
            self.assertIn("Shirt number: 0", football_cards.page_text(view.current))
            self.assertNotIn("Wrong squad", football_cards.page_text(view.current))
            await view._update(interaction(), "next")
            await view._update(interaction(), "next")
            self.assertIn("Squad Player 6", football_cards.page_text(view.current))
            self.assertEqual(squad.await_count, 1)
            self.assertEqual(coach.await_count, 1)
            self.assertEqual(seasons.await_count, 1)
            self.assertEqual([item for item in client.calls if item[0] == "search_teams"], identity_calls)
            mismatched = FootballRetrievalResult(team_context_row=profile)
            squad.side_effect = _async_return([squad_rows[0]])
            await FootballOperationService(client).load_profile_section(mismatched, "team_squad")
            self.assertEqual(mismatched.outcome, FootballOutcome.RESOLUTION_FAILED)
        view.section_menu._values = ["season"]
        await view._update(interaction(), "section")
        await view._update(interaction(), "refresh")
        self.assertEqual(sum(name == "get_team_statistics" for name, _ in client.calls), 2)
        view.section_menu._values = ["team_venue"]
        await view._update(interaction(), "section")
        self.assertIn("No venue details", view.current["text"])
        view.section_menu._values = ["season"]
        await view._update(interaction(), "section")
        view.can_attach = True
        navigation = interaction()
        navigation.edit_original_response.side_effect = [
            discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "No attachments"), None]
        await view._update(navigation, "next")
        self.assertTrue(navigation.edit_original_response.call_args_list[0].kwargs["attachments"])
        self.assertEqual(navigation.edit_original_response.call_args_list[1].kwargs["attachments"], [])
        view.current["rows"].append(("Complete name", "Long Name " * 400))
        message = interaction()
        await view.text.callback(message)
        message.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertGreater(message.followup.send.await_count, 1)
        pages = [call.args[0] for call in message.followup.send.call_args_list]
        self.assertTrue(all(call.kwargs["ephemeral"] for call in message.followup.send.call_args_list))
        self.assertEqual(" ".join(" ".join(pages).split()).count("Long Name"), 400)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))
        expired = interaction()
        await view._update(expired, "next")
        expired.edit_original_response.assert_not_awaited()
        await cog.football_player.callback(cog, ctx, "Erling Haaland", season=2025)
        player_view = ctx.sent[-1]["view"]
        player_view.can_attach = False
        self.assertTrue(all(player_view.pages[key] is None for key in
                            ("player_teams", "player_transfers", "player_trophies", "player_sidelined")))
        identity_calls = [item for item in client.calls if item[0] in {"search_player_profiles", "search_teams", "get_player_stats"}]
        career_rows = [{"team": {"id": index + 5, "name": f"Career Club {index}"},
                        "seasons": [2010, 2024]} for index in range(7)]
        transfers = [{"player": {"id": 9, "name": "Erling Haaland"}, "transfers": [
            {"date": f"2024-01-{index+1:02d}", "type": "Loan", "teams": {"out": {"name": f"Old Club {index}"},
               "in": {"name": "New Club"}}} for index in range(7)]}]
        transfers.append({"player": {"id": 999, "name": "Wrong player"}, "transfers": [
            {"date": "2026-01-01", "type": "Loan", "teams": {"in": {"name": "Unrelated club"}}}]})
        with patch.object(client, "get_player_teams", side_effect=_async_return(career_rows)) as career, patch.object(
            client, "get_transfers", side_effect=_async_return(transfers)
        ) as transfer, patch.object(client, "get_trophies", wraps=client.get_trophies) as trophies, patch.object(
            client, "get_sidelined", wraps=client.get_sidelined
        ) as absences:
            for section in ("player_teams", "player_transfers", "player_trophies", "player_sidelined", "profile", "player_teams"):
                player_view.section_menu._values = [section]
                await player_view._update(interaction(), "section")
            for fetch in (career, transfer, trophies, absences):
                self.assertEqual(fetch.call_args.args[0].player_id, 9)
                self.assertEqual(fetch.await_count, 1)
            self.assertIsNone(transfer.call_args.args[0].team_id)
            self.assertEqual([item for item in client.calls if item[0] in
                              {"search_player_profiles", "search_teams", "get_player_stats"}], identity_calls)
            await player_view._update(interaction(), "next")
            await player_view._update(interaction(), "next")
            self.assertIn("Career Club 6", football_cards.page_text(player_view.current))
            self.assertIn("2010", football_cards.page_text(player_view.current))
            self.assertEqual(career.await_count, 1)
            text = "\n".join(football_cards.page_text(page) for page in player_view.pages["player_transfers"])
            self.assertIn("Old Club 0", text)
            self.assertIn("Old Club 6", text)
            self.assertNotIn("Wrong player", text)
            player_view.section_menu._values = ["player_trophies"]
            await player_view._update(interaction(), "section")
            trophies.side_effect = _async_return([])
            await player_view._update(interaction(), "refresh")
            self.assertIn("No records are published", player_view.current["text"])
            self.assertTrue(player_view.pages["profile"])
            absences.side_effect = FootballApiError("unavailable")
            player_view.section_menu._values = ["player_sidelined"]
            with self.assertLogs(level="ERROR"):
                await player_view._update(interaction(), "section")
            self.assertIn("Could not load this section", player_view.current["text"])
            player_view.section_menu._values = ["profile"]
            await player_view._update(interaction(), "section")
            self.assertIn("Erling Haaland", football_cards.page_text(player_view.current))
        view.message.edit.assert_awaited_once()

        ctx.bot_permissions.attach_files = True
        with self.assertLogs(level="ERROR"), patch.object(football_cards, "render_profile", side_effect=OSError("unavailable")):
            await cog.football_player.callback(cog, ctx, "haaland", season=2025)
        self.assertIn("Erling Haaland", ctx.sent[-1]["content"])
        self.assertFalse(ctx.sent[-1]["files"])
        ctx.send = AsyncMock(side_effect=[discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "No attachments"),
                                         SimpleNamespace(id=1)])
        await cog.football_player.callback(cog, ctx, "haaland", league="ligamx")
        self.assertTrue(ctx.send.call_args_list[0].kwargs["files"])
        self.assertIn("Erling Haaland", ctx.send.call_args_list[1].kwargs["content"])
        self.assertNotIn("embed", ctx.send.call_args_list[0].kwargs)
        self.assertIn("2026", ctx.send.call_args_list[0].kwargs["view"].current["subtitle"])

        from copy import deepcopy
        client = _FakeFootballClient()
        ctx = _FakeCtx(client)
        ctx.bot_permissions = SimpleNamespace(attach_files=False)
        cog = FootballCog(ctx.bot)
        fixtures = []
        for index in range(31):
            fixture = deepcopy(_fixture())
            fixture["fixture"].update(id=100 + index, date="2025-10-27T20:00:00-06:00")
            fixture["league"].update(id=262, season=2025, round="Regular Season - 12")
            fixture["teams"]["home"]["name"] = f"Complete Home Team Name {index}"
            fixtures.append(fixture)
        with patch.object(client, "get_next_fixtures", side_effect=_async_return(fixtures)) as fetch:
            await cog.football_next.callback(cog, ctx, "262", target="31", season=2025)
            self.assertEqual(fetch.call_args.kwargs["next_count"], 31)
            self.assertEqual(fetch.call_args.kwargs["season"], 2025)
            view = ctx.sent[-1]["view"]
            self.assertEqual(len(view.pages["fixtures"]), 7)
            self.assertEqual(len(view.fixture_menu.options), 5)
            for _ in range(6):
                await view._update(interaction(), "next")
            self.assertEqual(view.index, 6)
            self.assertIn("Complete Home Team Name 30", football_cards.page_text(view.current))
            self.assertEqual([option.value for option in view.fixture_menu.options], ["130"])
            self.assertEqual(fetch.await_count, 1)
            other = interaction(99)
            await view._update(other, "previous")
            self.assertEqual(view.index, 6)
            other.response.send_message.assert_awaited_once()
            with patch.object(client, "get_fixture_by_id", new_callable=AsyncMock) as detail:
                view.fixture_menu._values = ["130"]
                await view._update(interaction(), "fixture")
                detail.assert_not_awaited()
            self.assertIn("Complete Home Team Name 30", view.current["title"])
            view.section_menu._values = ["fixtures"]
            await view._update(interaction(), "section")
            self.assertEqual(view.index, 6)
            with self.assertLogs(level="ERROR"), patch.object(client, "get_next_fixtures", side_effect=FootballApiError("unavailable")):
                await view._update(interaction(), "refresh")
            self.assertIn("Use Refresh", view.current["text"])
            with patch.object(client, "get_next_fixtures", side_effect=_async_return([])):
                await view._update(interaction(), "refresh")
            self.assertTrue(view.fixture_menu.disabled)
            self.assertIn("No fixtures", view.current["text"])
            self.assertFalse(view.refresh_button.disabled)
            view.message = SimpleNamespace(edit=AsyncMock())
            await view.on_timeout()
            self.assertTrue(all(item.disabled for item in view.children))

        for command in (cog.football_today, cog.football_next, cog.football_last, cog.football_schedule):
            league = next(param for param in command.app_command.parameters if param.name == "league")
            self.assertTrue(league.autocomplete)
            self.assertFalse(league.choices)

    async def test_surname_only_player_stays_ambiguous_without_clear_hydrated_winner(self) -> None:
        class _Client:
            async def search_player_profiles(self, request):
                return [
                    {"player": {"id": 1100, "name": "E. Haaland"}, "statistics": []},
                    {"player": {"id": 315448, "name": "S. Haaland"}, "statistics": []},
                ]

            async def get_player_stats(self, request):
                return []

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("Haaland"))

        self.assertTrue(lookup.resolution.ambiguous)
        self.assertIsNone(lookup.resolution.selected)

    def test_player_identity_rejects_conflicting_firstname(self) -> None:
        requested = _player_slot("Jude Bellingham")
        result = football_resolver.pick_player_identity(
            [{"player": {"id": 1, "name": "Adam Bellingham"}, "statistics": [_player()["statistics"][0]]}],
            requested,
        )

        self.assertIsNone(result.selected)
        self.assertFalse(result.ambiguous)


    async def test_player_command_disambiguates_multiple_matches(self) -> None:
        class _AmbiguousClient(_FakeFootballClient):
            async def search_player_profiles(self, request):
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                return [
                    _player(name="Emiliano Martinez", player_id=1),
                    _player(name="Lautaro Martinez", player_id=2),
                ]

        client = _AmbiguousClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_player.callback(cog, ctx, "martinez")

        self.assertTrue(ctx.sent)
        self.assertIn("Multiple players matched", str(ctx.sent[-1]["content"]))

        profiles = [{"player": {"id": 1100, "name": "E. Haaland", "firstname": "Erling", "lastname": "Braut Haaland"}},
                    {"player": {"id": 315448, "name": "S. Haaland", "firstname": "Susanne Oliver", "lastname": "Haaland"}}]
        llm = XAIClient("test-key", "test-model")
        cog.bot.llm_client = SimpleNamespace(select_football_player_candidate=llm.select_football_player_candidate)
        for ref in ("p1", "unknown"):
            with patch.object(client, "search_player_profiles", side_effect=_async_return(profiles)), patch.object(
                llm, "_small_json_call", side_effect=_async_return({"ref": ref, "confidence": 0.95})
            ) as choose:
                await cog.football_player.callback(cog, ctx, "Erling Haaland")
            self.assertTrue(choose.called)
            self.assertEqual(choose.call_args.kwargs["request"]["request"], "Erling Haaland")
            self.assertEqual(choose.call_args.kwargs["request"]["candidates"][0]["lastname"], "Braut Haaland")
            if ref == "p1":
                self.assertIn("E. Haaland", ctx.sent[-1]["view"].current["title"])
            else:
                self.assertIn("Player not found", ctx.sent[-1]["content"])

        profiles = [{"player": {"id": n, "name": "R. Aguirre"}, "statistics": []} for n in (71, 72)]
        record = {"team": {"id": 1, "name": "Club America"},
                  "league": {"id": 262, "name": "Liga MX", "season": 2026},
                  "goals": {"total": 0}}
        scoped = [{"player": {"id": 72, "name": "Rodrigo Aguirre"}, "statistics": [record]},
                  {"player": {"id": 71, "name": "Rodrigo Aguirre"}, "statistics": [
                      {**record, "team": {"id": 2}}, {**record, "league": {"id": 262, "season": 2025}}]}]
        cog.bot.llm_client = SimpleNamespace(select_football_player_candidate=AsyncMock(return_value=None))
        ctx.bot_permissions = SimpleNamespace(attach_files=False)
        with patch.object(client, "search_player_profiles", side_effect=_async_return(profiles)), patch.object(
            client, "search_players", side_effect=_async_return(scoped)
        ) as search, patch.object(client, "get_player_stats", side_effect=_async_return(scoped)) as stats:
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", league="ligamx", team="Club America")
            view = ctx.sent[-1]["view"]
            self.assertIn("Club America", view.current["subtitle"])
            self.assertEqual(_player_request_kwargs(search.call_args.args[0]),
                {"name": "Aguirre", "league_id": 262, "season": 2026, "team_id": 1})
            self.assertEqual(_player_stats_kwargs(stats.call_args.args[0]),
                {"player_id": 72, "league_id": 262, "season": 2026, "team_id": 1})
            await view.refresh()
            self.assertEqual(stats.await_count, 2)
            self.assertTrue(all(_player_stats_kwargs(call.args[0])["team_id"] == 1 for call in stats.call_args_list))
            cog.bot.llm_client.select_football_player_candidate.assert_not_awaited()

        # A supplied team must not broaden to unrelated players or current squads in a historical season.
        with patch.object(client, "search_player_profiles", side_effect=_async_return(profiles)), patch.object(
            client, "search_players", side_effect=_async_return([])
        ), patch.object(client, "get_player_stats", new_callable=AsyncMock) as stats, patch.object(
            client, "get_player_squads", new_callable=AsyncMock
        ) as squads:
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", league="ligamx", season=2025, team="Club America")
            self.assertIn("Player not found", ctx.sent[-1]["content"])
            stats.assert_not_awaited()
            squads.assert_not_awaited()

        with patch.object(client, "search_player_profiles", side_effect=_async_return(profiles)), patch.object(
            client, "get_player_squads", side_effect=_async_return([
                {"team": record["team"], "players": [{"id": 72, "name": "Rodrigo Aguirre"}]}
            ])
        ) as squads, patch.object(client, "get_player_stats", side_effect=_async_return(scoped)):
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", team="Club America")
            self.assertEqual(squads.call_args.args[0].team_id, 1)
            self.assertIn("Rodrigo Aguirre", ctx.sent[-1]["view"].current["title"])

        with patch.object(client, "search_teams", side_effect=_async_return([])), patch.object(
            client, "search_player_profiles", new_callable=AsyncMock
        ) as profiles_call:
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", league="ligamx", team="Unknown Club")
            self.assertIn("No matching provider data", ctx.sent[-1]["content"])
            profiles_call.assert_not_awaited()

        with patch.object(client, "search_player_profiles", side_effect=_async_return(profiles)), patch.object(
            client, "search_players", side_effect=FootballApiError("unavailable")
        ):
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", league="ligamx", team="Club America")
            self.assertIn("provider could not load", ctx.sent[-1]["content"].lower())

        with patch.object(client, "get_league_by_id", side_effect=_async_return([
            {"league": {"id": 262}, "seasons": [{"year": 2026, "coverage": {"players": False}}]}
        ])), patch.object(client, "search_players", new_callable=AsyncMock) as search:
            await cog.football_player.callback(cog, ctx, "Rodrigo Aguirre", league="ligamx", team="Club America")
            self.assertIn("does not cover", ctx.sent[-1]["content"])
            search.assert_not_awaited()

        params = cog.football_player.app_command.parameters
        self.assertFalse(next(item for item in params if item.name == "team").required)

    async def test_global_team_fallback_avoids_default_league_false_not_found(self) -> None:
        class _FallbackTeamClient(_FakeFootballClient):
            async def search_teams(self, request):
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                if kwargs.get("league_id") is not None:
                    return []
                return [{"team": {"id": 22, "name": str(kwargs.get("name", "Argentina")).title()}}]

        client = _FallbackTeamClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_match.callback(cog, ctx, team="argentina", when="next")

        self.assertIn(("get_next_fixtures", {"league_id": None, "season": None, "next_count": 10, "team_id": 22}), client.calls)
        self.assertNotIn("resolve_league_id", [name for name, _ in client.calls])

        def fixture(fixture_id, status):
            item = _fixture()
            item["fixture"]["id"], item["fixture"]["status"] = fixture_id, {"short": status}
            item["teams"]["home"].update(id=22, name="Argentina")
            return item

        with patch.object(client, "get_live_fixtures", side_effect=_async_return([fixture(80,"HT")])) as live:
            await cog.football_match.callback(cog,ctx,team="Argentina",when="live")
        live.assert_awaited_once_with(league_id=None,team_id=22)
        self.assertEqual(ctx.sent[-1]["view"].current["match"]["fixture"]["id"],80)
        with patch.object(client,"get_live_fixtures",side_effect=_async_return([fixture(81,"NS")])), patch.object(
            client,"get_next_fixtures") as next_fetch:
            await cog.football_match.callback(cog,ctx,team="Argentina",when="live")
        self.assertEqual(ctx.sent[-1]["content"],"Argentina is not playing a match right now. Check its schedule with next or last.")
        next_fetch.assert_not_awaited()
        fixtures = [fixture(90,"FT"),fixture(91,"FT")]
        with patch.object(client,"get_last_fixtures",side_effect=_async_return(fixtures)) as last:
            await cog.football_match.callback(cog,ctx,team="Argentina",when="last")
        self.assertEqual(last.call_args.kwargs["team_id"],22)
        view = ctx.sent[-1]["view"]
        self.assertTrue(view.pending_fixture)
        self.assertTrue(view.section_menu.disabled)
        self.assertEqual([option.value for option in view.fixture_menu.options],["90","91"])

        def interaction(user=42):
            return SimpleNamespace(user=SimpleNamespace(id=user),response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                                   followup=SimpleNamespace(send=AsyncMock()),edit_original_response=AsyncMock())

        view.fixture_menu._values = ["91"]
        other = interaction(99)
        await view.fixture_menu.callback(other)
        other.edit_original_response.assert_not_awaited()
        self.assertTrue(view.pending_fixture)
        calls = len(client.calls)
        await view.fixture_menu.callback(interaction())
        self.assertEqual(len(client.calls),calls)
        self.assertFalse(view.pending_fixture)
        self.assertFalse(view.section_menu.disabled)
        self.assertEqual(view.current["match"]["fixture"]["id"],91)
        view.section_menu._values = ["statistics"]
        await view._update(interaction(),"section")
        self.assertEqual(client.calls[-1],("get_fixture_statistics",{"fixture_id":91}))
        view.fixture_menu._values = ["90"]
        await view.fixture_menu.callback(interaction())
        self.assertIsNone(view.pages["statistics"])
        view.section_menu._values = ["statistics"]
        await view._update(interaction(),"section")
        self.assertEqual(client.calls[-1],("get_fixture_statistics",{"fixture_id":90}))
        with patch.object(client,"get_fixture_by_id",side_effect=_async_return([fixtures[0]])) as fetch:
            await view._update(interaction(),"refresh")
        fetch.assert_awaited_once_with(fixture_id=90,cache_ttl_seconds=30)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.fixture_menu.disabled)

        # Selected provider league IDs and autocomplete use the existing compiler and metadata.
        base_client = _FakeFootballClient()
        base_ctx = _FakeCtx(base_client)
        base_cog = FootballCog(base_ctx.bot)
        suggestions = await base_cog.football_match_league_autocomplete(SimpleNamespace(),"League")
        self.assertIn("71",[choice.value for choice in suggestions])
        await base_cog.football_match.callback(base_cog,base_ctx,team="Mexico",league="71",when="next")
        self.assertIn(("get_next_fixtures",{"league_id":71,"season":2026,"team_id":1,"next_count":10}),base_client.calls)
        self.assertNotIn("resolve_league_id",[name for name,_ in base_client.calls])
        await base_cog.football_match.callback(base_cog,base_ctx,team=" ")
        self.assertEqual(base_ctx.sent[-1]["content"],"Enter a team name.")
        base_cog.bot.db.get_guild_settings = _async_return(SimpleNamespace(language_code="es"))
        with patch.object(base_client,"get_live_fixtures",side_effect=_async_return([])):
            await base_cog.football_match.callback(base_cog,base_ctx,team="Mexico",when="live")
        self.assertEqual(base_ctx.sent[-1]["content"],
                         "Mexico no esta disputando un partido ahora. Revisa su calendario con next o last.")
        self.assertEqual([param.name for param in cog.football_match.app_command.parameters],["team","league","when"])
        self.assertTrue(cog.football_match.app_command.parameters[0].required)

        from copy import deepcopy
        client = _FakeFootballClient()
        ctx = _FakeCtx(client)
        ctx.bot_permissions = SimpleNamespace(attach_files=False)
        cog = FootballCog(ctx.bot)
        team_row = {"team": {"id": 22, "name": "Northbank Town", "country": "Mexico"}}
        domestic = {"league": {"id": 262, "name": "Domestic League", "type": "League"},
                    "country": {"name": "Mexico"}, "seasons": [{"year": 2026, "current": True}]}
        cup = {**domestic, "league": {"id": 263, "name": "Domestic Cup", "type": "Cup"}}
        foreign = {**domestic, "league": {"id": 264, "type": "League"}, "country": {"name": "England"}}
        first, second = fixture(201, "NS"), fixture(202, "NS")
        for item, day, round_name in ((first, "2026-10-28", "Round 16"), (second, "2026-11-03", "Round 17")):
            item["fixture"]["date"] = f"{day}T20:00:00-06:00"
            item["league"].update(id=262, season=2026, round=round_name)
        cup_fixture = deepcopy(first)
        cup_fixture["fixture"]["id"] = 203
        cup_fixture["league"]["id"] = 263
        with patch.object(client, "search_teams", side_effect=_async_return([team_row])), patch.object(
            client, "search_leagues", new_callable=AsyncMock, return_value=[cup, foreign, domestic]
        ) as leagues, patch.object(client, "get_next_fixtures", new_callable=AsyncMock,
                                   return_value=[cup_fixture, second, first]) as upcoming:
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="next", count=2)
            self.assertEqual(upcoming.call_args.kwargs, {"league_id": 262, "season": 2026, "team_id": 22, "next_count": 2})
            self.assertEqual(leagues.call_args.args[0].team_id, 22)
            self.assertIs(leagues.call_args.args[0].current, True)
            self.assertEqual(ctx.sent[-1]["view"].current["match"]["fixture"]["id"], 202)
            self.assertIn("Round 17", ctx.sent[-1]["view"].current["subtitle"])
            self.assertIsNone(ctx.sent[-1]["view"].fixture_menu)
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="next")
            self.assertEqual(upcoming.call_args.kwargs["next_count"], 1)
            self.assertEqual(ctx.sent[-1]["view"].current["match"]["fixture"]["id"], 201)
            upcoming.return_value = [first]
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="next", count=2)
            self.assertIn("Only 1", ctx.sent[-1]["content"])
            self.assertNotIn("view", ctx.sent[-1])
            leagues.return_value = [domestic, {**domestic, "league": {"id": 265, "type": "League"}}]
            upcoming.reset_mock()
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="next")
            self.assertIn("optional league field", ctx.sent[-1]["content"])
            upcoming.assert_not_awaited()
            leagues.reset_mock()
            upcoming.return_value = [cup_fixture]
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="next", league="263")
            self.assertEqual(upcoming.call_args.kwargs["league_id"], 263)
            leagues.assert_not_awaited()

        old, recent = deepcopy(first), deepcopy(second)
        old["fixture"].update(date="2026-10-18T20:00:00-06:00", status={"short": "FT"})
        recent["fixture"].update(date="2026-10-25T20:00:00-06:00", status={"short": "FT"})
        with patch.object(client, "search_teams", side_effect=_async_return([team_row])), patch.object(
            client, "get_last_fixtures", side_effect=_async_return([old, first, recent])
        ) as previous, patch.object(client, "get_next_fixtures", new_callable=AsyncMock) as upcoming:
            await cog.football_schedule.callback(cog, ctx, team="Northbank Town", date="last", league="262", count=2)
            self.assertEqual(previous.call_args.kwargs["last_count"], 2)
            self.assertEqual(ctx.sent[-1]["view"].current["match"]["fixture"]["id"], 201)
            upcoming.assert_not_awaited()
        params = cog.football_schedule.app_command.parameters
        self.assertEqual([param.name for param in params], ["team", "date", "league", "count"])
        self.assertEqual([param.required for param in params], [True, True, False, False])
        self.assertEqual([choice.value for choice in params[1].choices], ["next", "last"])



    async def test_league_fixture_results_call_date_fixtures_with_league_id(self) -> None:
        class _LeagueFixtureClient(_FakeFootballClient):
            def __init__(self) -> None:
                super().__init__()
                self.date_calls: list[dict[str, object]] = []

            def today_iso(self) -> str:
                return "2026-08-06"

            async def resolve_league_id(self, league_key):
                return 140 if league_key == "laliga" else 1

            async def get_current_season(self, league_id):
                return 2026

            async def get_fixtures_on_date(self, **kwargs):
                self.date_calls.append(kwargs)
                return [
                    {
                        "fixture": {"id": 8, "date": "2026-08-06T19:00:00+00:00", "status": {"short": "FT"}},
                        "league": {"id": 140, "name": "La Liga"},
                        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
                        "goals": {"home": 1, "away": 1},
                    }
                ]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "how did the matches in LaLiga finish today?",
            {"data_focus": "summary", "league_candidates": ["LaLiga"], "date_hint": "today"},
        )
        client = _LeagueFixtureClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertEqual(client.date_calls, [{"league_id": 140, "season": 2026, "date_iso": "2026-08-06"}])
        self.assertEqual(result.fixtures[0]["fixture"]["id"], 8)
        self.assertEqual(result.football_entity_context["league_id"], 140)


    def test_followup_date_correction_preserves_validated_league_and_replaces_date(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "I was wrong, it was yesterday",
            None,
            prior_context='{"entity_type":"league","league_id":140,"league_name":"LaLiga","season":2026,"operation_type":"league_fixture_results","time_scope":"today","date_hint":"today"}',
        )

        self.assertEqual(operation.operation_type, "fixture_result")
        self.assertEqual(tuple(slot.name for slot in operation.league_slots), ("LaLiga",))
        self.assertEqual(operation.time_scope, "yesterday")
        self.assertFalse(operation.team_slots)


    def test_player_identity_team_hint_disambiguates(self) -> None:
        requested = _player_slot("Alex Moreno")
        rows = [
            {"player": {"id": 1, "name": "Alex Moreno", "team": {"id": 10, "name": "Team A"}}},
            {"player": {"id": 2, "name": "Alex Moreno", "team": {"id": 11, "name": "Team B"}}},
        ]
        resolution = football_resolver.pick_player_identity(rows, requested, team_hint="Team B")

        self.assertEqual(resolution.selected["player"]["id"], 2)

    async def test_team_season_statistics_recipe_requires_league_and_team_scope(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "estadisticas de temporada de Mexico en Liga MX",
            {"data_focus": "team_season_statistics", "team_candidates": ["Mexico"], "league_candidates": ["Liga MX"]},
        )
        client = _FakeFootballClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="team_season_statistics")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIsNotNone(result.team_season_statistics)
        self.assertIn(("get_team_statistics", {"league_id": 262, "season": 2026, "team_id": 1}), client.calls)


    async def test_direct_coach_and_venue_candidates_use_provider_search(self) -> None:
        coach = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "quien es Pep Guardiola",
            {
                "semantic_source": "router",
                "operation_family": "coach_profile",
                "data_focus": "coach",
                "coach_candidates": ["Pep Guardiola"],
            },
        )
        venue = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "informacion del Camp Nou",
            {
                "semantic_source": "router",
                "operation_family": "venue_lookup",
                "data_focus": "venue",
                "venue_candidates": ["Camp Nou"],
            },
        )

        client = _FakeFootballClient()
        coach_result = await FootballOperationService(client).execute(coach, league_id=None, season=None, data_focus="coach")
        venue_result = await FootballOperationService(client).execute(venue, league_id=None, season=None, data_focus="venue")

        self.assertEqual(coach_result.outcome, FootballOutcome.SELECTED)
        self.assertEqual(venue_result.outcome, FootballOutcome.SELECTED)
        coach_call = next(kwargs for name, kwargs in client.calls if name == "search_coaches")
        venue_call = next(kwargs for name, kwargs in client.calls if name == "search_venues")
        self.assertEqual(coach_call["search"], "Pep Guardiola")
        self.assertEqual(venue_call["search"], "Camp Nou")


class FootballIdentityRepairExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_names_reach_provider_seasons_and_exact_id_standings(self) -> None:
        from cogs.ai_chat import AIChatCog

        class _Provider(_FakeFootballClient):
            async def search_leagues(self, request):
                name = _value(getattr(request, "name", None))
                self._record("search_leagues", name=name)
                if name not in (None, "Second Division"):
                    return []
                rows = [{"league": {"id": n, "name": "Second Division", "type": "League"},
                         "country": {"name": country}, "seasons": [{"year": 2026, "current": True}]}
                        for n, country in ((72, "Canada"), (71, "Ireland"))]
                return rows[1:] if getattr(request, "team_id", None) else rows

            async def search_teams(self, request):
                name = _value(getattr(request, "name", None) or getattr(request, "search", None))
                self._record("search_teams", name=name, search=_value(getattr(request, "search", None)),
                             league_id=getattr(request, "league_id", None), season=_value(getattr(request, "season", None)))
                if name not in (None, "Northbank"):
                    return []
                return [{"team": {"id": 8, "name": "Northbank", "country": "Canada"}},
                        {"team": {"id": 7, "name": "Northbank", "country": "Ireland"}}]

            async def get_team_seasons(self, request):
                self._record("get_team_seasons", team_id=request.team_id)
                return [{"season": 2025}, {"season": 2026}]

            async def get_standings(self, **kwargs):
                self._record("get_standings", **kwargs)
                return [{"league": {"standings": [[{"rank": 2, "team": {"id": 7, "name": "Northbank"}}]]}}]

        for operation_name, league in (("team_seasons", None), ("standings", None), ("standings", "Regional Second")):
            with self.subTest(operation=operation_name, league=league):
                operation = compile_football_operation("FOOTBALL_LOOKUP", f"Northbank Town {league or ''}", {
                    "semantic_source": "router", "operation_family": operation_name,
                    "data_focus": operation_name, "team_candidates": ["Northbank Town"],
                    "league_candidates": [league] if league else [],
                })
                provider = _Provider()
                async def select(entity_type, slot, rows):
                    return next((i for i, row in enumerate(rows)
                                 if (row.get("country", {}).get("name") if entity_type == "league"
                                     else row["team"].get("country")) == "Ireland"), None)

                service = FootballOperationService(provider, identity_selector=select)
                prior = await service.execute(operation, league_id=None, season=None, data_focus=operation_name)
                repairs = []

                async def replan(**kwargs):
                    repairs.append(kwargs["slot_kind"])
                    return {"slot_kind": kwargs["slot_kind"], "slot_index": kwargs["slot_index"],
                            "candidate_names": ["Second Division" if kwargs["slot_kind"] == "league" else "Northbank"],
                            "scope_candidates": [], "evidence_refs": []}

                cog = AIChatCog.__new__(AIChatCog)
                result = await cog._apply_bounded_football_identity_repairs(service, operation, prior, replan_method=replan, league_id=None, season=None, data_focus=operation_name)
                self.assertEqual(result.outcome, FootballOutcome.SELECTED)
                self.assertEqual(result.resolved_entities["team"]["id"], 7)
                self.assertFalse(any(name == "search_teams" and kwargs.get("search") is not None
                                     and (kwargs.get("league_id") is not None or kwargs.get("season") is not None)
                                     for name, kwargs in provider.calls))
                self.assertEqual(repairs, ["league"] if league else ["team"])
                if league:
                    self.assertFalse(any(kwargs.get("league_id") == 72 for name, kwargs in provider.calls if name == "search_teams"))
                if operation_name == "team_seasons":
                    self.assertEqual(result.reference_rows, [{"season": 2025}, {"season": 2026}])
                    self.assertIn(("get_team_seasons", {"team_id": 7}), provider.calls)
                else:
                    self.assertEqual(result.standing_row["team"]["id"], 7)
                    self.assertEqual(sum(name == "get_standings" for name, _kwargs in provider.calls), 1)

    async def test_an_unchanged_name_does_not_repeat_execution(self) -> None:
        from cogs.ai_chat import AIChatCog

        operation = compile_football_operation("FOOTBALL_LOOKUP", "synthetic request", {
            "semantic_source": "router", "operation_family": "team_seasons",
            "data_focus": "team_seasons", "team_candidates": ["Northbank Town"],
        })
        feedback = {"failed_slot": {"kind": "team", "index": 0}, "scope": {}, "observed_candidates": [
            {"ref": "fbref:original", "entity_type": "team", "provider_id": 7, "scope": {}},
        ]}
        prior = FootballRetrievalResult(outcome=FootballOutcome.NOT_FOUND, missing_inputs=["team"], repair_feedback=feedback)
        executions = []

        async def execute(*args, **kwargs):
            executions.append(kwargs)
            return prior

        async def replan(**kwargs):
            return None if refs is None else {"slot_kind": "team", "slot_index": 0, "candidate_names": ["Northbank Town"], "scope_candidates": [], "evidence_refs": refs}

        cog = AIChatCog.__new__(AIChatCog)
        for refs in (None, [], ["fbref:original"]):
            result = await cog._apply_bounded_football_identity_repairs(SimpleNamespace(execute=execute), operation, prior, replan_method=replan, league_id=None, season=None, data_focus="team_seasons")
            self.assertIs(result, prior)
        self.assertEqual(executions, [])


if __name__ == "__main__":
    unittest.main()
