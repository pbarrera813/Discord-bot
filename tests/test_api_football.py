from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import unittest

from services.api_football import ApiFootballClient, FootballApiError
from services.football_api_request_compiler import (
    InvalidFootballApiRequest,
    build_coach_search_request,
    build_fixture_ids_request,
    build_fixture_rounds_request,
    build_fixture_statistics_request,
    build_odds_reference_request,
    build_odds_request,
    build_league_search_request,
    build_player_profile_request,
    build_player_seasons_request,
    build_player_teams_request,
    build_prediction_request,
    build_team_statistics_request,
    build_trophy_request,
    build_sidelined_request,
    build_team_search_request,
    build_team_seasons_request,
    build_venue_search_request,
    make_league_candidate,
    make_player_candidate,
    make_team_candidate,
)
from services.football_query_service import compile_football_operation
from services.football_formatter import fixture_datetime


class _FakeFootballResponse:
    status = 200

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *_args):  # noqa: ANN002, ANN202
        return None

    async def json(self, content_type=None):  # noqa: ANN001, ANN202
        return {"response": [], "errors": []}


class _FakeFootballSession:
    closed = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def get(self, url, *, params=None, headers=None):  # noqa: ANN001, ANN202
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

    async def get_session():  # noqa: ANN202
        return session

    client._get_session = get_session  # type: ignore[method-assign]
    return client, session


class ApiFootballClientTests(unittest.TestCase):
    def test_extract_response(self) -> None:
        payload = {"response": [{"id": 1}, {"id": 2}], "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(len(rows), 2)

    def test_extract_response_accepts_single_object(self) -> None:
        payload = {"response": {"team": {"id": 1}}, "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(rows, [{"team": {"id": 1}}])

    def test_extract_response_wraps_primitive_values_for_typed_wrappers(self) -> None:
        payload = {"response": [2026, 2025], "errors": []}
        rows = ApiFootballClient._extract_response(payload)
        self.assertEqual(rows, [{"value": 2026}, {"value": 2025}])

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

    def test_ai_chat_has_no_legacy_natural_language_league_helpers(self) -> None:
        source = Path("cogs/ai_chat.py").read_text(encoding="utf-8")

        self.assertNotIn("_football_league_from_text", source)
        self.assertNotIn("_football_league_from_plan_or_text", source)
        self.assertNotIn("_football_league_from_operation_or_text", source)
        self.assertNotIn("_football_resolved_league_context", source)

    def test_touched_production_sources_have_no_mojibake_signatures(self) -> None:
        paths = (
            Path("cogs/ai_chat.py"),
            Path("services/api_football.py"),
            Path("services/football_resolver.py"),
            Path("services/xai_client.py"),
        )
        signatures = ("Ã", "Â", "â€", "\ufffd")

        for path in paths:
            with self.subTest(path=str(path)):
                source = path.read_text(encoding="utf-8")
                for signature in signatures:
                    self.assertNotIn(signature, source)

    def test_planner_prompt_uses_neutral_football_placeholders(self) -> None:
        source = Path("services/xai_client.py").read_text(encoding="utf-8")
        prompt_start = source.index("You are a football/soccer request planner")
        prompt_end = source.index('"role": "user"', prompt_start)
        prompt = source[prompt_start:prompt_end]

        self.assertIn("TEAM_A", prompt)
        self.assertIn("LEAGUE_A", prompt)
        self.assertIn("PLAYER_A", prompt)
        for forbidden in ("Pumas", "Champions", "LaLiga", "Liga MX", "Argentina", "Suiza"):
            self.assertNotIn(forbidden, prompt)

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

    def test_player_profile_wrapper_uses_profiles_endpoint_with_validated_lastname(self) -> None:
        client, session = _client_with_fake_transport()

        import asyncio

        request = build_player_profile_request(_player_slot("Julian Brandt"))
        asyncio.run(client.search_player_profiles(request))

        self.assertEqual(session.calls[0][0], "https://example.test/players/profiles")
        self.assertEqual(session.calls[0][1], {"search": "Brandt"})

    def test_player_seasons_wrapper_uses_compiled_request(self) -> None:
        client, session = _client_with_fake_transport()

        import asyncio

        asyncio.run(client.get_player_seasons(build_player_seasons_request(player_id=278)))

        self.assertEqual(session.calls[0][0], "https://example.test/players/seasons")
        self.assertEqual(session.calls[0][1], {"player": 278})

    def test_new_capability_wrappers_use_compiled_requests(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            await client.get_fixtures_by_ids(build_fixture_ids_request([1, 2]))
            await client.get_fixture_statistics(request=build_fixture_statistics_request(fixture_id=1, half=True, team_id=10, stat_type="Shots on Goal"))
            await client.get_fixture_rounds(build_fixture_rounds_request(league_id=140, season=2026, current=True, include_dates=True))
            await client.get_team_statistics(request=build_team_statistics_request(league_id=140, season=2026, team_id=529, date_iso="2026-08-06"))
            await client.get_team_seasons(build_team_seasons_request(team_id=529))
            await client.search_venues(build_venue_search_request(search="Camp Nou"))
            await client.search_coaches(build_coach_search_request(team_id=529))
            await client.get_player_teams(build_player_teams_request(player_id=278))
            await client.get_trophies(build_trophy_request(player_id=278))
            await client.get_sidelined(build_sidelined_request(player_id=278))
            await client.get_predictions(build_prediction_request(fixture_id=1))
            await client.get_odds(build_odds_request(fixture_id=1))
            await client.get_live_odds(build_odds_request(fixture_id=1, live=True))
            await client.get_odds_bookmakers(build_odds_reference_request())

        import asyncio

        asyncio.run(_run())

        params = [call[1] for call in session.calls]
        self.assertIn({"ids": "1-2", "timezone": "America/Mexico_City"}, params)
        self.assertIn({"fixture": 1, "team": 10, "type": "Shots on Goal", "half": "true"}, params)
        self.assertIn({"league": 140, "season": 2026, "current": "true", "dates": "true", "timezone": "America/Mexico_City"}, params)
        self.assertIn({"league": 140, "season": 2026, "team": 529, "date": "2026-08-06"}, params)
        self.assertIn({"search": "Camp Nou"}, params)
        self.assertIn({"fixture": 1}, params)

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

    def test_raw_football_param_firewall_rejects_sentence_like_searches(self) -> None:
        _client, session = _client_with_fake_transport()

        with self.assertRaises(InvalidFootballApiRequest):
            make_player_candidate("estadisticas mas recientes julian brandt", source="slash_arg")
        with self.assertRaises(InvalidFootballApiRequest):
            make_player_candidate("donde juega julian brandt ahora", source="slash_arg")
        with self.assertRaises(InvalidFootballApiRequest):
            make_team_candidate("lesiones recientes julian brandt", source="slash_arg")
        with self.assertRaises(InvalidFootballApiRequest):
            make_league_candidate("dime donde juega julian brandt", source="slash_arg")
        self.assertEqual(session.calls, [])

    def test_entity_wrappers_reject_legacy_raw_inputs_with_zero_http_calls(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            invalid_calls = [
                lambda: client.search_leagues("Brasileirao"),  # type: ignore[arg-type]
                lambda: client.search_teams("America"),  # type: ignore[arg-type]
                lambda: client.search_players("Haaland"),  # type: ignore[arg-type]
                lambda: client.search_player_profiles("Brandt"),  # type: ignore[arg-type]
                lambda: client.get_player_stats({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_player_seasons({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_player_squads({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_player_teams({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_injuries({"team_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_transfers({"team_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_fixtures_by_ids({"fixture_ids": [1]}),  # type: ignore[arg-type]
                lambda: client.get_fixture_rounds({"league_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_team_seasons({"team_id": 1}),  # type: ignore[arg-type]
                lambda: client.search_venues("Camp Nou"),  # type: ignore[arg-type]
                lambda: client.search_coaches("Coach"),  # type: ignore[arg-type]
                lambda: client.get_trophies({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_sidelined({"player_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_predictions({"fixture_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_odds({"fixture_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_live_odds({"fixture_id": 1}),  # type: ignore[arg-type]
                lambda: client.get_odds_bookmakers("book"),  # type: ignore[arg-type]
                lambda: client.get_odds_bets("bet"),  # type: ignore[arg-type]
                lambda: client.get_live_odds_bets("bet"),  # type: ignore[arg-type]
            ]
            for call in invalid_calls:
                with self.assertRaises(InvalidFootballApiRequest):
                    await call()

        asyncio.run(_run())
        self.assertEqual(session.calls, [])

    def test_id_wrappers_reject_invalid_scalars_with_zero_http_calls(self) -> None:
        client, session = _client_with_fake_transport()

        async def _run() -> None:
            invalid_calls = [
                lambda: client.get_current_season(True),  # type: ignore[arg-type]
                lambda: client.get_live_fixtures(team_id=True),  # type: ignore[arg-type]
                lambda: client.get_fixtures_on_date(date_iso="ayer", team_id=33),
                lambda: client.get_next_fixtures(next_count=0),
                lambda: client.get_last_fixtures(last_count=False),  # type: ignore[arg-type]
                lambda: client.get_standings(league_id=True, season=2026),  # type: ignore[arg-type]
                lambda: client.get_top_scorers(league_id=262, season=True),  # type: ignore[arg-type]
                lambda: client.get_fixture_by_id(fixture_id=True),  # type: ignore[arg-type]
                lambda: client.get_fixture_events(fixture_id=0),
                lambda: client.get_fixture_lineups(fixture_id=False),  # type: ignore[arg-type]
                lambda: client.get_fixture_statistics(fixture_id=-1),
                lambda: client.get_fixture_players(fixture_id=None),  # type: ignore[arg-type]
                lambda: client.get_top_assists(league_id=0, season=2026),
                lambda: client.get_top_yellow_cards(league_id=262, season=1800),
                lambda: client.get_top_red_cards(league_id=False, season=2026),  # type: ignore[arg-type]
                lambda: client.get_head_to_head(team_a_id=True, team_b_id=2),  # type: ignore[arg-type]
                lambda: client.get_team_statistics(league_id=262, season=2026, team_id=False),  # type: ignore[arg-type]
            ]
            for call in invalid_calls:
                with self.assertRaises(InvalidFootballApiRequest):
                    await call()

        asyncio.run(_run())
        self.assertEqual(session.calls, [])

    def test_api_football_private_transport_static_audit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        allowed_request_file = root / "services" / "api_football.py"
        allowed_token_file = root / "services" / "football_api_request_compiler.py"
        violations: list[str] = []
        for path in [*root.joinpath("services").glob("*.py"), *root.joinpath("cogs").glob("*.py")]:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_request":
                    if path != allowed_request_file and "api_football" in source:
                        violations.append(f"external_api_football_request:{path}")
                if isinstance(node, ast.ImportFrom) and node.module == "services.api_football":
                    for alias in node.names:
                        if alias.name == "_CompiledApiFootballRequest":
                            violations.append(f"external_envelope_import:{path}")
                if isinstance(node, ast.ImportFrom) and node.module == "services.football_api_request_compiler":
                    for alias in node.names:
                        if alias.name == "_COMPILER_TOKEN" and path != allowed_token_file:
                            violations.append(f"compiler_token_import:{path}")
                        if alias.name in {"make_player_candidate", "make_team_candidate", "make_league_candidate", "make_country_candidate"}:
                            violations.append(f"raw_candidate_factory_import:{path}:{alias.name}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"resolve_team_candidate", "resolve_league_candidate", "resolve_player"}:
                        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            violations.append(f"raw_resolver_literal:{path}:{node.func.attr}")
        self.assertEqual(violations, [])

    def test_football_retrieval_architecture_static_audit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        allowed_client_calls = {
            ("cogs/football.py", "football_match", "get_fixture_events"),
            ("cogs/football.py", "football_match", "get_fixture_statistics"),
            ("cogs/football.py", "_fixtures_for_query", "get_fixture_by_id"),
            ("cogs/football.py", "_send_fixture_detail", "get_fixture_by_id"),
            ("cogs/football.py", "_send_fixture_detail", "get_fixture_events"),
            ("cogs/football.py", "_send_fixture_detail", "get_fixture_statistics"),
            ("cogs/football.py", "_send_fixture_detail", "get_fixture_lineups"),
        }
        violations: list[str] = []
        for path in [root / "cogs" / "ai_chat.py", root / "cogs" / "football.py", root / "services" / "football_watch.py"]:
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent

            def enclosing_function(node: ast.AST) -> str:
                current: ast.AST | None = node
                while current is not None:
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return current.name
                    current = parents.get(current)
                return "<module>"

            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    attr = node.func.attr
                    function = enclosing_function(node)
                    if (
                        isinstance(owner, ast.Name)
                        and owner.id == "client"
                        and (attr.startswith("get_") or attr.startswith("search_") or attr.startswith("resolve_"))
                        and (rel, function, attr) not in allowed_client_calls
                    ):
                        violations.append(f"direct_client_call:{rel}:{function}:{attr}")
                    if (
                        isinstance(owner, ast.Name)
                        and owner.id == "football_resolver"
                        and attr.startswith("resolve_")
                    ):
                        violations.append(f"direct_resolver_call:{rel}:{function}:{attr}")
        self.assertEqual(violations, [])

    def test_football_operation_service_does_not_use_compatibility_candidate_tuples(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "services" / "football_operation_service.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        forbidden_attrs = {"team_candidates", "player_candidates", "league_candidates", "country_candidates"}
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                violations.append(f"compat_tuple_input:{node.attr}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
