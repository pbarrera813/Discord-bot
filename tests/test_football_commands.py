from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
import unittest

from cogs.football import FootballCog, LEAGUE_CODES, LEAGUE_HELP_TEXT
from services.api_football import FootballApiError
from services import football_formatter, football_resolver
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
        await cog.football_last.callback(cog, ctx, "ligamx", team="Mexico")
        await cog.football_table.callback(cog, ctx, "ligamx")
        await cog.football_team.callback(cog, ctx, "ligamx", team="Mexico")
        await cog.football_scorers.callback(cog, ctx, "ligamx")
        await cog.football_match.callback(cog, ctx, "10", "ligamx")
        await cog.football_schedule.callback(cog, ctx, target="league", mode="next", league="ligamx")
        await cog.football_player.callback(cog, ctx, "haaland")
        await cog.football_lineup.callback(cog, ctx, 10)
        await cog.football_stats.callback(cog, ctx, 10)
        await cog.football_injuries.callback(cog, ctx, "real madrid")
        await cog.football_transfers.callback(cog, ctx, "america")
        await cog.football_h2h.callback(cog, ctx, "argentina", "switzerland")
        await cog.football_top.callback(cog, ctx, "assists", "ligamx")
        await cog.football_preview.callback(cog, ctx, 10)
        await cog.football_summary.callback(cog, ctx, 10)

        called = {name for name, _kwargs in client.calls}
        expected = {
            "get_live_fixtures",
            "get_fixtures_on_date",
            "get_next_fixtures",
            "get_last_fixtures",
            "get_standings",
            "search_teams",
            "get_top_scorers",
            "get_fixture_by_id",
            "get_fixture_events",
            "get_fixture_statistics",
            "search_player_profiles",
            "get_fixture_lineups",
            "get_injuries",
            "get_transfers",
            "get_head_to_head",
            "get_top_assists",
        }
        self.assertTrue(expected.issubset(called))


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

        await cog.football_match.callback(cog, ctx, "argentina", "ligamx")

        self.assertIn(("get_next_fixtures", {"league_id": 262, "season": 2026, "next_count": 1, "team_id": 22}), client.calls)


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


if __name__ == "__main__":
    unittest.main()
