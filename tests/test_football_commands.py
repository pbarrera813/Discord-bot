from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
import unittest

from cogs.football import FootballCog, LEAGUE_CODES, LEAGUE_HELP_TEXT
from services.api_football import FootballApiError
from services import football_formatter, football_resolver
from services.football_api_request_compiler import InvalidFootballApiRequest
from services.football_api_request_compiler import _make_league_slot
from services.football_operation_service import FootballOperationService, FootballOutcome
from services.football_query_service import compile_football_operation


def _value(item):  # noqa: ANN001, ANN202
    return getattr(item, "value", item)


def _player_slot(name: str):
    return compile_football_operation("FOOTBALL_PLAYER_QUERY", name, {"data_focus": "player", "player_candidates": [name]}).player_slots[0]


def _league_slot(name: str):
    return compile_football_operation("FOOTBALL_LOOKUP", name, {"data_focus": "league_lookup", "league_candidates": [name]}).league_slots[0]


def _league_request_kwargs(request):  # noqa: ANN001, ANN202
    return {
        "name": _value(getattr(request, "name", None)),
        "country": _value(getattr(request, "country", None)),
        "search": _value(getattr(request, "search", None)),
        "current": getattr(request, "current", None),
    }


def _team_request_kwargs(request):  # noqa: ANN001, ANN202
    return {
        "name": _value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
    }


def _player_request_kwargs(request):  # noqa: ANN001, ANN202
    return {
        "name": _value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _player_stats_kwargs(request):  # noqa: ANN001, ANN202
    return {
        "player_id": getattr(request, "player_id", None),
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _player_seasons_kwargs(request):  # noqa: ANN001, ANN202
    return {"player_id": getattr(request, "player_id", None)}


def _injury_request_kwargs(request):  # noqa: ANN001, ANN202
    return {
        "league_id": getattr(request, "league_id", None),
        "season": _value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
        "player_id": getattr(request, "player_id", None),
        "fixture_id": getattr(request, "fixture_id", None),
    }


def _transfer_request_kwargs(request):  # noqa: ANN001, ANN202
    return {"team_id": getattr(request, "team_id", None), "player_id": getattr(request, "player_id", None)}


def _async_return(value):  # noqa: ANN001
    async def _inner(*_args, **_kwargs):  # noqa: ANN202
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

    def test_date_correction_uses_prior_context_without_bogus_team_slot(self) -> None:
        operation = compile_football_operation(
            "CHAT",
            "ah no, hoy no, fue ayer",
            None,
            prior_context='{"entity_type":"team","team_id":77,"team_name":"Pumas UNAM","league_id":262,"league_name":"Leagues Cup","season":2026,"operation_type":"team_fixture_result","time_scope":"today","date_hint":"today"}',
        )

        self.assertEqual(operation.operation_type, "fixture_result")
        self.assertEqual(operation.time_scope, "yesterday")
        self.assertEqual(tuple(slot.name for slot in operation.team_slots), ("Pumas UNAM",))
        self.assertEqual(tuple(slot.name for slot in operation.league_slots), ("Leagues Cup",))

        tomorrow = compile_football_operation(
            "CHAT",
            "era mañana",
            None,
            prior_context='{"entity_type":"team","team_id":77,"team_name":"Pumas UNAM","operation_type":"fixture_next","time_scope":"today","date_hint":"today"}',
        )
        self.assertEqual(tomorrow.time_scope, "tomorrow")
        self.assertEqual(tuple(slot.name for slot in tomorrow.team_slots), ("Pumas UNAM",))

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

    def test_operation_service_recipe_registry_declares_contracts(self) -> None:
        expected = {
            "player",
            "fixture_list",
            "fixture_match",
            "team_fixture_result",
            "league_fixture_results",
            "fixture_result_by_id",
            "team",
            "league",
            "h2h",
            "team_statistics",
            "competition",
            "fixture_optional",
            "coach",
            "venue",
            "reference",
        }
        self.assertEqual(set(FootballOperationService.RECIPES), expected)
        for key, recipe in FootballOperationService.RECIPES.items():
            with self.subTest(recipe=key):
                self.assertEqual(recipe.key, key)
                self.assertTrue(recipe.aliases)
                self.assertTrue(recipe.permitted_endpoints)
                self.assertTrue(recipe.output_payload)
                self.assertTrue(recipe.failure_outcomes)
                self.assertIsInstance(recipe.required_slots, tuple)
                self.assertIsInstance(recipe.allowed_optional_slots, tuple)
                self.assertIsInstance(recipe.forbidden_slots, tuple)

    def test_compiles_player_entity_operations_without_raw_sentence_candidates(self) -> None:
        cases = [
            ("podrias darme estadisticas mas recientes de Julian Brandt", "player_recent_stats", ("Julian Brandt",)),
            ("stats de Brandt", "player_recent_stats", ("Brandt",)),
            ("donde juega Julian Brandt ahora", "player_current_team", ("Julian Brandt",)),
            ("transferencias recientes de Julian Brandt", "player_transfers", ("Julian Brandt",)),
            ("lesiones de Julian Brandt", "player_injuries", ("Julian Brandt",)),
        ]
        for request, expected_type, expected_candidates in cases:
            with self.subTest(request=request):
                operation = compile_football_operation("CHAT", request, {})
                self.assertEqual(operation.operation_type, expected_type)
                self.assertEqual(operation.route_action, "FOOTBALL_PLAYER_QUERY")
                for candidate in operation.player_candidates:
                    self.assertFalse(
                        candidate.casefold().startswith("podrias darme"),
                        msg=f"raw player candidate leaked: {candidate}",
                    )
                self.assertEqual(operation.player_candidates[: len(expected_candidates)], expected_candidates)

    def test_player_slot_extracts_structured_identity_hints(self) -> None:
        operation = compile_football_operation(
            "CHAT",
            "podrias darme estadisticas recientes de Jude Bellingham, jugador ingles que juega en el Real Madrid",
            {},
        )

        self.assertEqual(operation.operation_type, "player_recent_stats")
        self.assertEqual(operation.player_slots[0].full_name, "Jude Bellingham")
        self.assertEqual(operation.player_slots[0].first_name, "Jude")
        self.assertEqual(operation.player_slots[0].last_name, "Bellingham")
        self.assertEqual(operation.player_slots[0].team_hint, "Real Madrid")
        self.assertEqual(operation.player_slots[0].nationality_hint, "England")
        self.assertNotIn("Jude Bellingham Ingles Real Madrid", operation.player_candidates)
        self.assertEqual([slot.full_name for slot in operation.player_slots], ["Jude Bellingham"])

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

    def test_team_injury_and_transfer_plans_do_not_become_player_requests(self) -> None:
        injury_operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "lesionados del Real Madrid",
            {"data_focus": "injuries", "team_candidates": ["Real Madrid"], "player_candidates": []},
        )
        transfer_operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "transferencias del America",
            {"data_focus": "transfers", "team_candidates": ["America"], "player_candidates": []},
        )

        self.assertEqual(injury_operation.operation_type, "team_injuries")
        self.assertEqual(injury_operation.player_candidates, ())
        self.assertEqual(transfer_operation.operation_type, "team_transfers")
        self.assertEqual(transfer_operation.player_candidates, ())

    def test_compiler_prunes_player_candidates_contaminated_by_team_context(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "podrias darme las estadisticas mas recientes de Diego Lainez que juega en Tigres UANL",
            {
                "data_focus": "player_recent_stats",
                "player_candidates": ["Diego Lainez", "Diego Lainez Tigres Uanl"],
                "team_candidates": ["Tigres UANL"],
            },
        )

        self.assertEqual(operation.operation_type, "player_recent_stats")
        self.assertEqual(operation.team_candidates, ("Tigres UANL",))
        self.assertEqual(operation.player_candidates, ("Diego Lainez",))
        self.assertNotIn("Tigres", " ".join(operation.player_candidates))

    def test_compiles_live_and_past_match_stat_operations(self) -> None:
        cases = [
            ("como van los rayados ahorita", "fixture_live", None, "live"),
            ("como van los rayados en la tabla", "standings", None, None),
            ("cuantos tiros a gol lleva Rayados", "fixture_statistics", "shots_on_goal", "today"),
            ("cual fue la posesion del balon en el ultimo juego de Toluca", "fixture_statistics", "ball_possession", "last_finished_match"),
            ("cuantas amarillas recibieron los Tigres en el juego de ayer", "fixture_statistics", "yellow_cards", "yesterday"),
            ("cuantos tiros a puerta tuvo America en su ultimo partido", "fixture_statistics", "shots_on_goal", "last_finished_match"),
            ("que posesion tuvo Necaxa vs Rayados", "fixture_statistics", "ball_possession", "recent_finished"),
        ]
        for request, operation_type, stat_focus, time_scope in cases:
            with self.subTest(request=request):
                operation = compile_football_operation("FOOTBALL_LOOKUP", request, {})
                self.assertEqual(operation.operation_type, operation_type)
                self.assertEqual(operation.stat_focus, stat_focus)
                self.assertEqual(operation.time_scope, time_scope)

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

    def test_compiler_does_not_launder_champions_inside_championship(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando inicia la EFL Championship?",
            {
                "intent": "FIXTURE",
                "data_focus": "season_start",
                "league_candidates": ["EFL Championship"],
                "team_candidates": [],
                "player_candidates": [],
            },
        )

        self.assertEqual(operation.operation_type, "competition_structure")
        self.assertEqual(operation.data_focus, "season_start")
        self.assertEqual(operation.league_candidates, ("EFL Championship",))
        self.assertEqual(tuple(slot.name for slot in operation.league_slots), ("EFL Championship",))
        self.assertNotIn("champions", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])

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

    def test_next_fixture_paraphrases_override_contradictory_planner(self) -> None:
        cases = (
            "next match for TEAM_A",
            "next fixture for TEAM_A",
            "upcoming game for TEAM_A",
            "upcoming fixture for TEAM_A",
            "proximo partido de TEAM_A",
            "siguiente juego de TEAM_A",
        )
        for request in cases:
            with self.subTest(request=request):
                operation = compile_football_operation(
                    "FOOTBALL_LOOKUP",
                    request,
                    {"data_focus": "standings", "team_candidates": ["TEAM_A"]},
                )
                self.assertEqual(operation.operation_type, "fixture_next")
                self.assertEqual(operation.capability_intent.operation_family, "fixture_next")

    def test_capability_paraphrases_follow_grounded_entity_permutation(self) -> None:
        for entity in ("TEAM_A", "TEAM_B"):
            with self.subTest(entity=entity):
                next_operation = compile_football_operation(
                    "FOOTBALL_LOOKUP",
                    f"next fixture for {entity}",
                    {"data_focus": "standings", "team_candidates": [entity]},
                )
                result_operation = compile_football_operation(
                    "FOOTBALL_LOOKUP",
                    f"how did {entity} finish yesterday",
                    {"data_focus": "next_fixtures", "team_candidates": [entity], "date_hint": "yesterday"},
                )
                self.assertEqual(next_operation.operation_type, "fixture_next")
                self.assertEqual(tuple(slot.name for slot in next_operation.team_slots), (entity,))
                self.assertEqual(result_operation.operation_type, "fixture_result")
                self.assertEqual(tuple(slot.name for slot in result_operation.team_slots), (entity,))

    def test_static_league_alias_requires_alias_provenance(self) -> None:
        class _NoStaticAliasClient(_FakeFootballClient):
            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                self._record("resolve_league_id", league_key=league_key)
                raise AssertionError(f"static alias must not be used for explicit NL slot: {league_key}")

            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                kwargs = _league_request_kwargs(request)
                self._record("search_leagues", **kwargs)
                return [{"league": {"id": 9001, "name": str(kwargs.get("search") or "Champions"), "type": "League"}}]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "standings for champions",
            {"data_focus": "standings", "league_candidates": ["champions"]},
        )
        explicit_slot = _make_league_slot(
            "champions",
            source="deterministic_parser",
            literal="champions",
            evidence="champions",
        )
        operation = replace(operation, league_candidates=("champions",), league_slots=(explicit_slot,))
        client = _NoStaticAliasClient()
        result = asyncio.run(FootballOperationService(client).execute(operation, league_id=None, season=None))

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertNotIn("resolve_league_id", [name for name, _kwargs in client.calls])
        self.assertIn(("search_leagues", {"name": None, "search": "champions", "country": None, "current": True}), client.calls)

    def test_player_stats_fail_closed_without_safe_season_source(self) -> None:
        class _NoSeasonClient(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                self._record("search_player_profiles", name=_value(getattr(request, "name", None)))
                return [{"player": {"id": 44, "name": "Player Alpha", "firstname": "Player", "lastname": "Alpha"}}]

            async def get_player_seasons(self, request):  # noqa: ANN001, ANN202
                self._record("get_player_seasons", **_player_seasons_kwargs(request))
                return []

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                self._record("get_player_stats", **_player_stats_kwargs(request))
                raise AssertionError("stats must not be probed with guessed current/current-1 seasons")

        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "recent stats for Player Alpha",
            {"data_focus": "player_recent_stats", "player_candidates": ["Player Alpha"]},
        )
        client = _NoSeasonClient()
        result = asyncio.run(FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=operation.data_focus))

        self.assertEqual(result.outcome, FootballOutcome.RESOLUTION_FAILED)
        self.assertIn("season", result.missing_inputs)
        self.assertNotIn("get_player_stats", [name for name, _kwargs in client.calls])

    def test_competition_start_uses_grounded_league_not_similar_alias(self) -> None:
        class _CompetitionStartClient(_FakeFootballClient):
            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                self._record("resolve_league_id", league_key=league_key)
                raise AssertionError(f"ungrounded league alias was used: {league_key}")

            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                kwargs = _league_request_kwargs(request)
                self._record("search_leagues", **kwargs)
                return [
                    {
                        "league": {"id": 999, "name": "Championship", "country": "England", "type": "League"},
                        "seasons": [{"year": 2025, "start": "2025-08-01", "end": "2026-05-30"}],
                    }
                ]

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2025

            async def get_next_fixtures(self, **kwargs):  # noqa: ANN202
                raise AssertionError(f"season start must not call next fixtures: {kwargs}")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando inicia la EFL Championship?",
            {"data_focus": "season_start", "league_candidates": ["EFL Championship"]},
        )
        client = _CompetitionStartClient()
        result = asyncio.run(FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=operation.data_focus))

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertEqual(result.generic_label, "FOOTBALL_COMPETITION_SEASON_START")
        self.assertIn(("search_leagues", {"name": None, "search": "EFL Championship", "country": None, "current": True}), client.calls)
        self.assertNotIn("resolve_league_id", [name for name, _kwargs in client.calls])
        self.assertNotIn("get_next_fixtures", [name for name, _kwargs in client.calls])

    def test_competition_metadata_api_error_is_resolution_failed_not_no_data(self) -> None:
        class _MetadataErrorClient(_FakeFootballClient):
            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                kwargs = _league_request_kwargs(request)
                self._record("search_leagues", **kwargs)
                return [{"league": {"id": 9100, "name": "League Alpha", "type": "League"}}]

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2026

            async def get_league_by_id(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_league_by_id", league_id=league_id)
                raise FootballApiError("provider unavailable")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "when does League Alpha start?",
            {"data_focus": "season_start", "league_candidates": ["League Alpha"]},
        )
        client = _MetadataErrorClient()
        result = asyncio.run(FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=operation.data_focus))

        self.assertEqual(result.outcome, FootballOutcome.RESOLUTION_FAILED)
        self.assertNotEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn("season", result.missing_inputs)
        self.assertTrue(any(note.startswith("season_metadata_fetch_failed=") for note in result.notes))

    def test_competition_metadata_valid_empty_payload_is_no_data_for_scope(self) -> None:
        class _EmptyMetadataClient(_FakeFootballClient):
            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                kwargs = _league_request_kwargs(request)
                self._record("search_leagues", **kwargs)
                return [{"league": {"id": 9101, "name": "League Beta", "type": "League"}}]

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2026

            async def get_league_by_id(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_league_by_id", league_id=league_id)
                return []

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "when does League Beta start?",
            {"data_focus": "season_start", "league_candidates": ["League Beta"]},
        )
        client = _EmptyMetadataClient()
        result = asyncio.run(FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=operation.data_focus))

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn("season_metadata_missing_for_scope", result.notes)

    def test_compiler_does_not_let_liga_alias_override_laliga(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando empieza la temporada de LaLiga",
            {"data_focus": "season_start"},
        )

        self.assertIn("laliga", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])
        self.assertNotIn("ligamx", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])
        self.assertEqual(operation.operation_type, "competition_structure")

    def test_compiler_does_not_treat_liga_de_argentina_as_laliga(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_SUMMARY",
            "como quedo el River Plate en el juego de ayer de la liga de argentina?",
            {"data_focus": "summary", "team_candidates": ["River Plate"], "league_candidates": ["liga de argentina"], "date_hint": "ayer"},
        )

        self.assertEqual(operation.operation_type, "fixture_result")
        self.assertIn("liga de argentina", operation.league_candidates)
        self.assertNotIn("laliga", [football_resolver.normalize_league_key(item) for item in operation.league_candidates])


class _FakeCtx:
    def __init__(self, client: object) -> None:
        self.guild = SimpleNamespace(id=1)
        self.interaction = None
        self.sent: list[dict[str, object]] = []
        self.bot = SimpleNamespace(
            api_football_client=client,
            db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en"))),
        )

    async def send(self, content: str | None = None, **kwargs):  # noqa: ANN001, ANN202
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.sent))


class _FakeFootballClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, call_name: str, **kwargs):  # noqa: ANN202
        self.calls.append((call_name, dict(kwargs)))

    async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
        self._record("resolve_league_id", league_key=league_key)
        return 262

    async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
        self._record("get_current_season", league_id=league_id)
        return 2026

    async def search_leagues(self, request):  # noqa: ANN001, ANN202
        kwargs = _league_request_kwargs(request)
        self._record("search_leagues", **kwargs)
        return [{"league": {"id": 71, "name": str(kwargs.get("search") or kwargs.get("name") or "League"), "type": "League"}}]

    async def get_live_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_live_fixtures", **kwargs)
        return [_fixture()]

    async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixtures_on_date", **kwargs)
        return [_fixture()]

    async def get_next_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_next_fixtures", **kwargs)
        return [_fixture()]

    async def get_last_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_last_fixtures", **kwargs)
        return [_fixture()]

    async def get_standings(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_standings", **kwargs)
        return [{"league": {"standings": [[{"rank": 1, "points": 3, "goalsDiff": 1, "team": {"id": 1, "name": "Mexico"}, "all": {"played": 1}}]]}}]

    async def search_teams(self, request):  # noqa: ANN001, ANN202
        kwargs = _team_request_kwargs(request)
        self._record("search_teams", **kwargs)
        name = str(kwargs.get("name", "Team"))
        team_id = 1 if "switzerland" not in name.casefold() else 2
        return [{"team": {"id": team_id, "name": name.title()}}]

    async def get_top_scorers(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_scorers", **kwargs)
        return [_player()]

    async def get_top_assists(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_assists", **kwargs)
        return [_player()]

    async def get_top_yellow_cards(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_yellow_cards", **kwargs)
        return [_player()]

    async def get_top_red_cards(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_red_cards", **kwargs)
        return [_player()]

    async def search_players(self, request):  # noqa: ANN001, ANN202
        kwargs = _player_request_kwargs(request)
        self._record("search_players", **kwargs)
        return [_player(name=str(kwargs.get("name", "Erling Haaland")))]

    async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
        lastname = _value(getattr(request, "lastname", None))
        self._record("search_player_profiles", search=lastname)
        key = str(lastname or "").casefold()
        if key == "haaland":
            return [_player(name="Erling Haaland")]
        if key == "brandt":
            return [_player(name="Julian Brandt", player_id=11)]
        return []

    async def get_player_seasons(self, request):  # noqa: ANN001, ANN202
        kwargs = _player_seasons_kwargs(request)
        self._record("get_player_seasons", **kwargs)
        return [{"season": 2026}, {"season": 2025}]

    async def get_fixture_by_id(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_by_id", **kwargs)
        return [_fixture()]

    async def get_fixture_events(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_events", **kwargs)
        return [{"time": {"elapsed": 10}, "team": {"name": "Mexico"}, "player": {"name": "A"}, "type": "Goal", "detail": "Normal Goal"}]

    async def get_fixture_statistics(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_statistics", **kwargs)
        return [{"team": {"name": "Mexico"}, "statistics": [{"type": "Shots", "value": 4}]}]

    async def get_fixture_lineups(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_lineups", **kwargs)
        return [{"team": {"name": "Mexico"}, "coach": {"name": "Coach"}, "formation": "4-3-3"}]

    async def get_fixture_rounds(self, request):  # noqa: ANN001, ANN202
        self._record("get_fixture_rounds", league_id=getattr(request, "league_id", None), season=_value(getattr(request, "season", None)), current=getattr(request, "current", None), include_dates=getattr(request, "include_dates", None))
        return [{"round": "Regular Season - 1", "dates": ["2026-08-01"]}]

    async def get_team_statistics(self, request=None, **kwargs):  # noqa: ANN001, ANN202
        if request is not None:
            kwargs = {"league_id": getattr(request, "league_id", None), "season": _value(getattr(request, "season", None)), "team_id": getattr(request, "team_id", None)}
        self._record("get_team_statistics", **kwargs)
        return [{"team": {"id": kwargs.get("team_id"), "name": "Mexico"}, "fixtures": {"played": {"total": 1}}, "goals": {"for": {"total": {"total": 2}}}}]

    async def get_player_squads(self, request):  # noqa: ANN001, ANN202
        self._record("get_player_squads", player_id=getattr(request, "player_id", None), team_id=getattr(request, "team_id", None))
        return [{"team": {"id": getattr(request, "team_id", None), "name": "Mexico"}, "players": []}]

    async def get_player_teams(self, request):  # noqa: ANN001, ANN202
        self._record("get_player_teams", player_id=getattr(request, "player_id", None))
        return [{"team": {"id": 5, "name": "Manchester City"}, "seasons": [2026]}]

    async def search_coaches(self, request):  # noqa: ANN001, ANN202
        self._record("search_coaches", coach_id=getattr(request, "coach_id", None), team_id=getattr(request, "team_id", None), search=_value(getattr(request, "search", None)))
        return [{"coach": {"id": 3, "name": "Coach"}, "team": {"id": getattr(request, "team_id", None), "name": "Mexico"}, "career": []}]

    async def search_venues(self, request):  # noqa: ANN001, ANN202
        self._record("search_venues", name=_value(getattr(request, "name", None)), search=_value(getattr(request, "search", None)))
        return [{"id": 10, "name": str(_value(getattr(request, "search", None)) or "Stadium"), "city": "City", "capacity": 50000}]

    async def get_trophies(self, request):  # noqa: ANN001, ANN202
        self._record("get_trophies", player_id=getattr(request, "player_id", None), coach_id=getattr(request, "coach_id", None))
        return [{"league": "League", "place": "Winner"}]

    async def get_sidelined(self, request):  # noqa: ANN001, ANN202
        self._record("get_sidelined", player_id=getattr(request, "player_id", None), coach_id=getattr(request, "coach_id", None))
        return [{"type": "Injury", "start": "2026-01-01", "end": "2026-01-10"}]

    async def get_predictions(self, request):  # noqa: ANN001, ANN202
        self._record("get_predictions", fixture_id=getattr(request, "fixture_id", None))
        return [{"predictions": {"winner": {"name": "Mexico"}, "percent": {"home": "45%", "draw": "30%", "away": "25%"}}}]

    async def get_odds(self, request):  # noqa: ANN001, ANN202
        self._record("get_odds", fixture_id=getattr(request, "fixture_id", None), league_id=getattr(request, "league_id", None))
        return [{"bookmakers": [{"name": "Book", "bets": []}]}]

    async def get_live_odds(self, request):  # noqa: ANN001, ANN202
        self._record("get_live_odds", fixture_id=getattr(request, "fixture_id", None), league_id=getattr(request, "league_id", None))
        return [{"fixture": {"id": getattr(request, "fixture_id", None)}, "odds": []}]

    async def get_league_by_id(self, **kwargs):  # noqa: ANN001, ANN202
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

    async def get_injuries(self, request):  # noqa: ANN001, ANN202
        kwargs = _injury_request_kwargs(request)
        self._record("get_injuries", **kwargs)
        return []

    async def get_transfers(self, request):  # noqa: ANN001, ANN202
        kwargs = _transfer_request_kwargs(request)
        self._record("get_transfers", **kwargs)
        return []

    async def get_head_to_head(self, **kwargs):  # noqa: ANN001, ANN202
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
    def test_worldcup_league_aliases_normalize(self) -> None:
        cog = FootballCog(SimpleNamespace())

        self.assertEqual(cog._normalize_league_key("worldcup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("world cup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("fifa world cup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("copa del mundo"), "worldcup")
        self.assertEqual(cog._normalize_league_key("copa mundial fifa"), "worldcup")

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

    def test_player_query_parser_strips_age_goal_and_year_noise(self) -> None:
        parsed = football_resolver.parse_player_query("cuantos años tendra Diego Lainez en 2030")

        joined = " ".join(parsed.candidates).casefold()
        self.assertIn("diego lainez", joined)
        self.assertNotIn("2030", joined)
        self.assertNotIn("años", joined)

    def test_player_query_parser_removes_stat_words(self) -> None:
        parsed = football_resolver.parse_player_query("Dibu Martinez penalty stats performance")

        joined = " ".join(parsed.candidates).casefold()
        self.assertIn("emiliano martinez", joined)
        self.assertNotIn("penalty", joined)
        self.assertNotIn("stats", joined)
        self.assertNotIn("performance", joined)

    def test_pick_team_exact_match_outranks_partial_rows(self) -> None:
        rows = [
            {"team": {"id": 1, "name": "America"}},
            {"team": {"id": 2, "name": "America de Cali"}},
        ]
        result = football_resolver.pick_team(rows, "america")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.selected["team"]["id"], 1)

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

    def test_extract_table_rows_flattens_worldcup_groups(self) -> None:
        rows = [
            {
                "league": {
                    "standings": [
                        [
                            {"rank": 1, "team": {"id": 1, "name": "Mexico"}},
                            {"rank": 2, "team": {"id": 2, "name": "Canada"}},
                        ],
                        [
                            {"rank": 1, "team": {"id": 3, "name": "Argentina"}},
                        ],
                    ]
                }
            }
        ]

        flattened = FootballCog._extract_table_rows(rows)

        self.assertEqual(
            [item["team"]["name"] for item in flattened],
            ["Mexico", "Canada", "Argentina"],
        )


class FootballCommandContractTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_player_command_uses_profile_discovery_without_explicit_league(self) -> None:
        client = _FakeFootballClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_player.callback(cog, ctx, "haaland")

        profile_calls = [kwargs for name, kwargs in client.calls if name == "search_player_profiles"]
        player_calls = [kwargs for name, kwargs in client.calls if name == "search_players"]
        self.assertTrue(profile_calls)
        self.assertEqual(profile_calls[0]["search"], "haaland")
        self.assertEqual(player_calls, [])

    async def test_player_resolver_does_not_send_invalid_unscoped_search(self) -> None:
        class _StrictClient(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs.get("league_id") is None and kwargs.get("team_id") is None:
                    raise FootballApiError("league or team required")
                return [_player(name=str(kwargs.get("name", "Erling Haaland")))]

        client = _StrictClient()
        lookup = await football_resolver.resolve_player(client, _player_slot("haaland"))

        player_calls = [kwargs for name, kwargs in client.calls if name == "search_players"]
        self.assertIsNotNone(lookup.resolution.selected)
        self.assertEqual(player_calls, [])

    async def test_raw_message_cannot_cross_query_spec_boundary(self) -> None:
        raw = "RAW_SENTINEL_7788 please show me Structured FC now"
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            raw,
            {"data_focus": "team", "team_candidates": ["Structured FC"]},
        )

        self.assertIsNotNone(operation.spec)
        self.assertNotIn(raw, repr(operation.spec))
        self.assertEqual([slot.name for slot in operation.team_slots], ["Structured FC"])
        with self.assertRaises(InvalidFootballApiRequest):
            await football_resolver.resolve_team_candidate(_FakeFootballClient(), raw)  # type: ignore[arg-type]

        class _Client(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                if raw in str(kwargs.get("name") or ""):
                    raise AssertionError("raw message crossed FootballQuerySpec boundary")
                return [{"team": {"id": 44, "name": str(kwargs.get("name"))}}]

        client = _Client()
        lookup = await football_resolver.resolve_team_candidate(client, operation.team_slots[0])

        self.assertIsNotNone(lookup.resolution.selected)
        self.assertEqual(client.calls[0], ("search_teams", {"name": "Structured FC", "league_id": None, "season": None}))

    async def test_league_resolver_discovers_unknown_supported_league(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                kwargs = _league_request_kwargs(request)
                self._record("search_leagues", **kwargs)
                if kwargs.get("search") == "Brasileirao":
                    return [{"league": {"id": 71, "name": "Serie A", "type": "League"}}]
                return []

        client = _Client()
        lookup = await football_resolver.resolve_league_candidate(client, _league_slot("Brasileirao"))

        self.assertEqual(lookup.league_id, 71)
        self.assertEqual(lookup.season, 2026)
        self.assertIn(("search_leagues", {"name": None, "country": None, "search": "Brasileirao", "current": True}), client.calls)

    async def test_ordinary_player_search_succeeds_without_canonicalizer(self) -> None:
        client = _FakeFootballClient()
        calls = 0

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            return {"candidate_names": ["Should Not Be Used"], "confidence": 1.0}

        lookup = await football_resolver.resolve_player(client, _player_slot("haaland"), canonicalizer=canonicalizer)

        self.assertIsNotNone(lookup.resolution.selected)
        self.assertEqual(calls, 0)
        self.assertFalse(lookup.canonicalizer_used)

    async def test_player_identity_uses_profile_fields_before_stats(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.profile_calls: list[dict[str, object]] = []
                self.stats_calls: list[dict[str, object]] = []

            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self.profile_calls.append(kwargs)
                return [
                    {"player": {"id": 129718, "name": "J. Bellingham", "firstname": "Jude", "lastname": "Bellingham"}, "statistics": []},
                    {"player": {"id": 321005, "name": "J. Bellingham", "firstname": "Jobe", "lastname": "Bellingham"}, "statistics": []},
                ]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_stats_kwargs(request)
                self.stats_calls.append(kwargs)
                raise AssertionError("identity resolution must not fetch stats before selecting player_id")

        client = _Client()
        lookup = await football_resolver.resolve_player(client, _player_slot("Jude Bellingham"))

        self.assertEqual(client.profile_calls, [{"search": "Bellingham"}])
        self.assertEqual(client.stats_calls, [])
        self.assertEqual(lookup.resolution.selected["player"]["id"], 129718)

    async def test_player_identity_does_not_select_first_profile_result(self) -> None:
        class _Client:
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                return [
                    {"player": {"id": 1, "name": "Adam Bellingham"}, "statistics": []},
                    {"player": {"id": 2, "name": "J. Bellingham", "firstname": "Jude", "lastname": "Bellingham"}, "statistics": []},
                ]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                raise AssertionError("first profile result must not be corrected by pre-identity stat hydration")

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("Jude Bellingham"))

        self.assertEqual(lookup.resolution.selected["player"]["id"], 2)
        self.assertEqual(lookup.resolution.selected["player"]["name"], "J. Bellingham")

    async def test_surname_only_player_does_not_select_by_stats_availability(self) -> None:
        class _Client:
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                return [
                    {"player": {"id": 1100, "name": "E. Haaland"}, "statistics": []},
                    {"player": {"id": 315448, "name": "S. Haaland"}, "statistics": []},
                ]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_stats_kwargs(request)
                if kwargs["player_id"] == 1100:
                    return [_player(name="Erling Haaland", player_id=1100)]
                return []

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("Haaland"))

        self.assertTrue(lookup.resolution.ambiguous)
        self.assertIsNone(lookup.resolution.selected)

    async def test_surname_only_player_stays_ambiguous_without_clear_hydrated_winner(self) -> None:
        class _Client:
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                return [
                    {"player": {"id": 1100, "name": "E. Haaland"}, "statistics": []},
                    {"player": {"id": 315448, "name": "S. Haaland"}, "statistics": []},
                ]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
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

    async def test_canonicalizer_candidate_must_validate_through_api(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                if kwargs["search"] == "Ronaldo":
                    return [_player(name="Cristiano Ronaldo", player_id=7)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Cristiano Ronaldo"], "confidence": 0.91}

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("cr7"), canonicalizer=canonicalizer)

        self.assertTrue(lookup.canonicalizer_used)
        self.assertEqual(lookup.resolution.selected["player"]["name"], "Cristiano Ronaldo")

    async def test_unvalidated_canonicalizer_candidate_is_rejected(self) -> None:
        class _EmptyClient(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Imaginary Player"], "confidence": 0.91}

        lookup = await football_resolver.resolve_player(_EmptyClient(), _player_slot("unknown nickname"), canonicalizer=canonicalizer)

        self.assertTrue(lookup.canonicalizer_used)
        self.assertIsNone(lookup.resolution.selected)

    async def test_la_tortuga_can_resolve_through_canonicalizer(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                if kwargs["search"] == "Mbappe":
                    return [_player(name="Kylian Mbappe", player_id=10)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Kylian Mbappe"], "confidence": 0.86}

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("la tortuga"), canonicalizer=canonicalizer)

        self.assertEqual(lookup.resolution.selected["player"]["name"], "Kylian Mbappe")

    async def test_canonicalizer_validation_can_remain_ambiguous(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                if kwargs["search"] == "Ronaldo":
                    return [
                        _player(name="Cristiano Ronaldo", player_id=7),
                        _player(name="Ronaldo Nazario", player_id=9),
                    ]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Ronaldo"], "confidence": 0.8}

        lookup = await football_resolver.resolve_player(_Client(), _player_slot("ronaldo"), canonicalizer=canonicalizer)

        self.assertTrue(lookup.resolution.ambiguous)

    async def test_validated_alias_cache_is_reused_without_canonicalizer(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_players", **kwargs)
                if kwargs["search"] == "Ronaldo":
                    return [_player(name="Cristiano Ronaldo", player_id=7)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Cristiano Ronaldo"], "confidence": 0.91}

        cache: dict[str, dict[str, object]] = {}
        first_client = _Client()
        await football_resolver.resolve_player(first_client, _player_slot("cr7"), canonicalizer=canonicalizer, alias_cache=cache)
        second_client = _Client()
        await football_resolver.resolve_player(second_client, _player_slot("cr7"), alias_cache=cache)

        second_searches = [kwargs["search"] for name, kwargs in second_client.calls if name == "search_players"]
        self.assertEqual(second_searches[0], "Ronaldo")

    async def test_player_command_disambiguates_multiple_matches(self) -> None:
        class _AmbiguousClient(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
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
            async def search_teams(self, request):  # noqa: ANN001, ANN202
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

    async def test_fixture_next_team_scope_never_executes_without_resolved_team_id(self) -> None:
        class _AmbiguousTeamClient(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                return [
                    {"team": {"id": 1, "name": "River Plate"}},
                    {"team": {"id": 2, "name": "River Plate"}},
                ]

            async def get_next_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("get_next_fixtures", **kwargs)
                raise AssertionError("team-scoped next fixture must not execute without a selected team")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando juega River Plate",
            {"data_focus": "next_fixtures", "team_candidates": ["River Plate"]},
        )
        client = _AmbiguousTeamClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="next_fixtures")

        self.assertEqual(result.outcome, FootballOutcome.AMBIGUOUS)
        self.assertNotIn("get_next_fixtures", [name for name, _kwargs in client.calls])

    async def test_recipe_slot_compatibility_rejects_incompatible_player_slot_before_api_calls(self) -> None:
        class _NoCallClient(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                self._record("search_teams", **_team_request_kwargs(request))
                raise AssertionError("incompatible recipe must not resolve entities")

        player_operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "estadisticas de Julian Brandt",
            {"data_focus": "player_recent_stats", "player_candidates": ["Julian Brandt"]},
        )
        operation = replace(player_operation, operation_type="fixture_next")
        client = _NoCallClient()

        result = await FootballOperationService(client).execute(operation, league_id=None, season=None)

        self.assertEqual(result.outcome, FootballOutcome.UNSUPPORTED)
        self.assertIn("player", "".join(result.notes))
        self.assertEqual(client.calls, [])

    async def test_fixture_next_uses_resolved_team_id(self) -> None:
        class _TeamClient(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                return [{"team": {"id": 44, "name": "River Plate"}}]

            async def get_next_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("get_next_fixtures", **kwargs)
                return [{"fixture": {"id": 9}, "teams": {"home": {"id": 44}, "away": {"id": 7}}}]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando juega River Plate",
            {"data_focus": "next_fixtures", "team_candidates": ["River Plate"]},
        )
        client = _TeamClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="next_fixtures")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn(("get_next_fixtures", {"league_id": None, "season": None, "next_count": 3, "team_id": 44}), client.calls)

    async def test_team_resolution_tries_distinct_clean_alternatives(self) -> None:
        class _TeamClient(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                if kwargs["name"] == "America":
                    return []
                if kwargs["name"] == "Club America":
                    return [{"team": {"id": 200, "name": "Club America"}}]
                return []

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando juega el America",
            {"data_focus": "next_fixtures", "team_candidates": ["America", "Club America"]},
        )
        client = _TeamClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="next_fixtures")

        searched_names = [kwargs["name"] for name, kwargs in client.calls if name == "search_teams"]
        self.assertEqual(searched_names[:2], ["America", "Club America"])
        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn(("get_next_fixtures", {"league_id": None, "season": None, "next_count": 3, "team_id": 200}), client.calls)

    async def test_exact_team_match_with_partial_rows_proceeds_to_fixture_lookup(self) -> None:
        class _TeamClient(_FakeFootballClient):
            async def search_teams(self, request):  # noqa: ANN001, ANN202
                kwargs = _team_request_kwargs(request)
                self._record("search_teams", **kwargs)
                return [
                    {"team": {"id": 44, "name": "River Plate"}},
                    {"team": {"id": 45, "name": "River Plate II"}},
                ]

            async def get_next_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("get_next_fixtures", **kwargs)
                return [{"fixture": {"id": 9}, "teams": {"home": {"id": 44}, "away": {"id": 7}}}]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "cuando juega River Plate",
            {"data_focus": "next_fixtures", "team_candidates": ["River Plate"]},
        )
        client = _TeamClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="next_fixtures")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn(("get_next_fixtures", {"league_id": None, "season": None, "next_count": 3, "team_id": 44}), client.calls)

    async def test_team_alias_exact_miss_continues_to_canonical_alias_search(self) -> None:
        class _TeamAliasClient(_FakeFootballClient):
            def __init__(self) -> None:
                super().__init__()
                self.team_searches: list[dict[str, object]] = []

            async def search_teams(self, request):  # noqa: ANN001, ANN202
                payload = {
                    "name": _value(getattr(request, "name", None)),
                    "search": _value(getattr(request, "search", None)),
                    "league_id": getattr(request, "league_id", None),
                    "season": _value(getattr(request, "season", None)),
                }
                self.team_searches.append(payload)
                self._record("search_teams", **payload)
                if payload.get("name") == "Barcelona":
                    return [{"team": {"id": 529, "name": "Barcelona", "country": "Spain"}}]
                return []

            async def get_last_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("get_last_fixtures", **kwargs)
                return [
                    {
                        "fixture": {"id": 77, "date": "2026-08-05T20:00:00+00:00", "status": {"short": "FT"}},
                        "league": {"id": 140, "name": "La Liga"},
                        "teams": {"home": {"id": 529, "name": "Barcelona"}, "away": {"id": 1, "name": "Rival"}},
                        "goals": {"home": 2, "away": 0},
                    }
                ]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "como quedo el barca",
            {"data_focus": "summary", "team_candidates": ["barca"], "time_scope": "last_finished_match"},
        )
        client = _TeamAliasClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn({"name": "barca", "search": None, "league_id": None, "season": None}, client.team_searches)
        self.assertIn({"name": "Barcelona", "search": None, "league_id": None, "season": None}, client.team_searches)
        self.assertIn(("get_last_fixtures", {"league_id": None, "season": None, "last_count": 10, "team_id": 529}), client.calls)

    async def test_league_fixture_results_call_date_fixtures_with_league_id(self) -> None:
        class _LeagueFixtureClient(_FakeFootballClient):
            def __init__(self) -> None:
                super().__init__()
                self.date_calls: list[dict[str, object]] = []

            def today_iso(self) -> str:
                return "2026-08-06"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140 if league_key == "laliga" else 1

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                return 2026

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
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

    async def test_unresolved_league_fixture_results_make_zero_global_fixture_calls(self) -> None:
        class _MissingLeagueClient(_FakeFootballClient):
            def __init__(self) -> None:
                super().__init__()
                self.date_calls: list[dict[str, object]] = []

            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                self._record("search_leagues", name=_value(getattr(request, "name", None)), search=_value(getattr(request, "search", None)))
                return []

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                self.date_calls.append(kwargs)
                raise AssertionError("unresolved league must not make global fixture date calls")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "how did the matches in Imaginary League finish today?",
            {"data_focus": "summary", "league_candidates": ["Imaginary League"], "date_hint": "today"},
        )
        client = _MissingLeagueClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.NOT_FOUND)
        self.assertEqual(client.date_calls, [])

    async def test_league_fixture_results_reject_broad_nonmatching_payloads(self) -> None:
        class _BroadFixtureClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-08-06"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                return 2026

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                return [
                    {
                        "fixture": {"id": 9, "date": "2026-08-06T19:00:00+00:00", "status": {"short": "FT"}},
                        "league": {"id": 999, "name": "Other League"},
                        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
                        "goals": {"home": 1, "away": 1},
                    }
                ]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "how did the matches in LaLiga finish today?",
            {"data_focus": "summary", "league_candidates": ["LaLiga"], "date_hint": "today"},
        )
        result = await FootballOperationService(_BroadFixtureClient()).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertEqual(result.fixtures, [])

    async def test_date_target_uses_provider_season_not_calendar_year(self) -> None:
        class _CrossYearClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-01-15"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2026

            async def get_league_by_id(self, **kwargs):  # noqa: ANN202
                self._record("get_league_by_id", **kwargs)
                return [
                    {
                        "league": {"id": kwargs.get("league_id"), "name": "Cross Year League"},
                        "seasons": [
                            {"year": 2025, "start": "2025-08-01", "end": "2026-05-31"},
                            {"year": 2026, "start": "2026-08-01", "end": "2027-05-31"},
                        ],
                    }
                ]

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                self._record("get_fixtures_on_date", **kwargs)
                return [
                    {
                        "fixture": {"id": 88, "date": "2026-01-15T20:00:00+00:00", "status": {"short": "FT"}},
                        "league": {"id": 140, "name": "Cross Year League"},
                        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
                    }
                ]

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "how did the matches in LaLiga finish on 2026-01-15?",
            {"data_focus": "summary", "league_candidates": ["LaLiga"], "date_hint": "2026-01-15"},
        )
        client = _CrossYearClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn(("get_fixtures_on_date", {"league_id": 140, "season": 2025, "date_iso": "2026-01-15"}), client.calls)
        self.assertNotIn(("get_fixtures_on_date", {"league_id": 140, "season": 2026, "date_iso": "2026-01-15"}), client.calls)

    async def test_same_season_date_correction_reuses_validated_season(self) -> None:
        class _SameSeasonClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-01-16"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2025

            async def get_league_by_id(self, **kwargs):  # noqa: ANN202
                self._record("get_league_by_id", **kwargs)
                return [{"league": {"id": 140}, "seasons": [{"year": 2025, "start": "2025-08-01", "end": "2026-05-31"}]}]

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                self._record("get_fixtures_on_date", **kwargs)
                return []

        operation = compile_football_operation(
            "CHAT",
            "no, fue ayer",
            None,
            prior_context='{"entity_type":"league","league_id":140,"league_name":"LaLiga","season":2025,"operation_type":"league_fixture_results","time_scope":"today","date_hint":"today"}',
        )
        client = _SameSeasonClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn("season_prior_reused", result.notes)
        self.assertIn(("get_fixtures_on_date", {"league_id": 140, "season": 2025, "date_iso": "2026-01-15"}), client.calls)

    async def test_date_correction_crossing_season_boundary_reresolves_season(self) -> None:
        class _BoundaryClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-01-16"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                self._record("get_current_season", league_id=league_id)
                return 2026

            async def get_league_by_id(self, **kwargs):  # noqa: ANN202
                self._record("get_league_by_id", **kwargs)
                return [
                    {
                        "league": {"id": 140},
                        "seasons": [
                            {"year": 2025, "start": "2025-08-01", "end": "2026-05-31"},
                            {"year": 2026, "start": "2026-08-01", "end": "2027-05-31"},
                        ],
                    }
                ]

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                self._record("get_fixtures_on_date", **kwargs)
                return []

        operation = compile_football_operation(
            "CHAT",
            "no, fue ayer",
            None,
            prior_context='{"entity_type":"league","league_id":140,"league_name":"LaLiga","season":2026,"operation_type":"league_fixture_results","time_scope":"today","date_hint":"today"}',
        )
        client = _BoundaryClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn(("get_fixtures_on_date", {"league_id": 140, "season": 2025, "date_iso": "2026-01-15"}), client.calls)

    async def test_no_compatible_season_metadata_makes_zero_fixture_calls(self) -> None:
        class _NoSeasonClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-01-15"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 140

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                return 2026

            async def get_league_by_id(self, **kwargs):  # noqa: ANN202
                self._record("get_league_by_id", **kwargs)
                return [{"league": {"id": 140}, "seasons": [{"year": 2026, "start": "2026-08-01", "end": "2027-05-31"}]}]

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                raise AssertionError("fixture call must not happen without compatible season metadata")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "how did the matches in LaLiga finish today?",
            {"data_focus": "summary", "league_candidates": ["LaLiga"], "date_hint": "today"},
        )
        client = _NoSeasonClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="summary")

        self.assertEqual(result.outcome, FootballOutcome.RESOLUTION_FAILED)
        self.assertNotIn("get_fixtures_on_date", [name for name, _kwargs in client.calls])

    async def test_team_and_league_date_result_uses_scoped_compatible_season(self) -> None:
        class _TeamLeagueClient(_FakeFootballClient):
            def today_iso(self) -> str:
                return "2026-08-06"

            async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
                return 262

            async def search_leagues(self, request):  # noqa: ANN001, ANN202
                payload = _league_request_kwargs(request)
                self._record("search_leagues", **payload)
                return [{"league": {"id": 262, "name": "Leagues Cup", "type": "Cup"}}]

            async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
                return 2026

            async def get_league_by_id(self, **kwargs):  # noqa: ANN202
                self._record("get_league_by_id", **kwargs)
                return [{"league": {"id": 262, "name": "Leagues Cup"}, "seasons": [{"year": 2026, "start": "2026-07-01", "end": "2026-08-31"}]}]

            async def search_teams(self, request):  # noqa: ANN001, ANN202
                payload = _team_request_kwargs(request)
                self._record("search_teams", **payload)
                return [{"team": {"id": 77, "name": "Pumas UNAM", "country": "Mexico", "national": False}}]

            async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN202
                self._record("get_fixtures_on_date", **kwargs)
                return []

        operation = compile_football_operation(
            "FOOTBALL_FIXTURE_QUERY",
            "jugaron los pumas hoy leagues cup?",
            {"operation": "fixture_next", "league_candidates": ["Leagues Cup"], "team_candidates": ["Pumas", "Pumas UNAM"]},
        )
        client = _TeamLeagueClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=operation.data_focus)

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn(("search_teams", {"name": "Pumas", "league_id": 262, "season": 2026}), client.calls)
        self.assertIn(("get_fixtures_on_date", {"league_id": 262, "season": 2026, "date_iso": "2026-08-06", "team_id": 77}), client.calls)
        self.assertEqual(result.football_entity_context["context_kind"], "validated_query")
        self.assertEqual(result.football_entity_context["team_id"], 77)
        self.assertEqual(result.football_entity_context["league_id"], 262)

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

    async def test_player_recent_stats_discovers_seasons_after_identity_lock(self) -> None:
        class _PlayerClient(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                kwargs = {"search": _value(getattr(request, "lastname", None))}
                self._record("search_player_profiles", **kwargs)
                return [
                    {"player": {"id": 10, "name": "J. Bellingham", "firstname": "Jobe", "lastname": "Bellingham"}, "statistics": []},
                    {"player": {"id": 20, "name": "J. Bellingham", "firstname": "Jude", "lastname": "Bellingham"}, "statistics": []},
                ]

            async def get_player_seasons(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_seasons_kwargs(request)
                self._record("get_player_seasons", **kwargs)
                return [{"season": 2025}, {"season": 2026}]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_stats_kwargs(request)
                self._record("get_player_stats", **kwargs)
                if kwargs["player_id"] != 20:
                    raise AssertionError("stats fetched for an unselected profile id")
                if kwargs["season"] == 2026:
                    return [_player(name="Jude Bellingham", player_id=20)]
                return []

        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "estadisticas recientes de Jude Bellingham",
            {"data_focus": "player_recent_stats", "player_candidates": ["Jude Bellingham"]},
        )
        client = _PlayerClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None)

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertIn(("get_player_seasons", {"player_id": 20}), client.calls)
        stats_calls = [kwargs for name, kwargs in client.calls if name == "get_player_stats"]
        self.assertEqual(stats_calls, [{"player_id": 20, "league_id": None, "season": 2026, "team_id": None}])

    async def test_selected_player_without_applicable_data_returns_no_data_for_scope(self) -> None:
        class _PlayerClient(_FakeFootballClient):
            async def search_player_profiles(self, request):  # noqa: ANN001, ANN202
                self._record("search_player_profiles", search=_value(getattr(request, "lastname", None)))
                return [{"player": {"id": 20, "name": "Jude Bellingham", "firstname": "Jude", "lastname": "Bellingham"}, "statistics": []}]

            async def get_player_seasons(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_seasons_kwargs(request)
                self._record("get_player_seasons", **kwargs)
                return [{"season": 2026}]

            async def get_player_stats(self, request):  # noqa: ANN001, ANN202
                kwargs = _player_stats_kwargs(request)
                self._record("get_player_stats", **kwargs)
                return []

        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "estadisticas recientes de Jude Bellingham",
            {"data_focus": "player_recent_stats", "player_candidates": ["Jude Bellingham"]},
        )
        client = _PlayerClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None)

        self.assertEqual(result.outcome, FootballOutcome.NO_DATA_FOR_SCOPE)
        self.assertIn(("get_player_seasons", {"player_id": 20}), client.calls)

    def test_current_explicit_player_suppresses_prior_player_context(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_PLAYER_QUERY",
            "estadisticas recientes de Julian Quinones",
            {"data_focus": "player_recent_stats", "player_candidates": ["Julian Quinones", "Alexis Vega"]},
            prior_context='{"entity_type":"player","player_id":7,"player_name":"Alexis Vega"}',
        )

        self.assertEqual(tuple(slot.full_name for slot in operation.player_slots), ("Julian Quinones",))

    def test_player_identity_rejects_same_surname_wrong_firstname(self) -> None:
        requested = _player_slot("Julian Brandt")
        resolution = football_resolver.pick_player_identity([_player(name="Max Brandt", player_id=1)], requested)

        self.assertIsNone(resolution.selected)

    def test_player_identity_does_not_select_first_profile_automatically(self) -> None:
        requested = _player_slot("Martinez")
        rows = [_player(name="Emiliano Martinez", player_id=1), _player(name="Lautaro Martinez", player_id=2)]
        resolution = football_resolver.pick_player_identity(rows, requested)

        self.assertIsNone(resolution.selected)
        self.assertTrue(resolution.ambiguous)

    def test_player_identity_exact_normalized_full_name_wins(self) -> None:
        requested = _player_slot("Julian Brandt")
        rows = [_player(name="Max Brandt", player_id=1), _player(name="Julian Brandt", player_id=2)]
        resolution = football_resolver.pick_player_identity(rows, requested)

        self.assertEqual(resolution.selected["player"]["id"], 2)

    def test_player_identity_accent_insensitive_match_works(self) -> None:
        requested = _player_slot("Angel Correa")
        rows = [_player(name="Ángel Correa", player_id=2)]
        resolution = football_resolver.pick_player_identity(rows, requested)

        self.assertEqual(resolution.selected["player"]["id"], 2)

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

    async def test_competition_rounds_recipe_uses_rounds_endpoint(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "rondas actuales de LaLiga",
            {"data_focus": "competition_rounds", "league_candidates": ["LaLiga"]},
        )
        client = _FakeFootballClient()
        result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus="competition_rounds")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertEqual(result.rounds[0]["round"], "Regular Season - 1")
        self.assertIn("get_fixture_rounds", [name for name, _kwargs in client.calls])

    async def test_fixture_prediction_uses_coverage_and_prediction_endpoint(self) -> None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "prediccion del partido de Mexico hoy",
            {"data_focus": "prediction", "team_candidates": ["Mexico"], "time_scope": "today"},
        )
        client = _FakeFootballClient()
        result = await FootballOperationService(client).execute(operation, league_id=262, season=2026, data_focus="prediction")

        self.assertEqual(result.outcome, FootballOutcome.SELECTED)
        self.assertTrue(result.predictions)
        self.assertIn("get_predictions", [name for name, _kwargs in client.calls])

    async def test_coverage_false_short_circuits_optional_endpoint(self) -> None:
        class _CoverageFalseClient(_FakeFootballClient):
            async def get_league_by_id(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("get_league_by_id", **kwargs)
                return [{"league": {"id": kwargs.get("league_id")}, "seasons": [{"year": 2026, "start": "2026-01-01", "end": "2026-12-31", "coverage": {"predictions": False}}]}]

            async def get_predictions(self, request):  # noqa: ANN001, ANN202
                raise AssertionError("coverage=false should skip optional prediction endpoint")

        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            "prediccion del partido de Mexico hoy",
            {"data_focus": "prediction", "team_candidates": ["Mexico"], "time_scope": "today"},
        )
        client = _CoverageFalseClient()
        result = await FootballOperationService(client).execute(operation, league_id=262, season=2026, data_focus="prediction")

        self.assertEqual(result.outcome, FootballOutcome.UNSUPPORTED_BY_COVERAGE)
        self.assertIn("coverage_false=predictions", result.notes)
        self.assertNotIn("get_predictions", [name for name, _kwargs in client.calls])

    async def test_player_trophies_and_sidelined_are_post_identity_recipes(self) -> None:
        for focus, expected_call in (("player_trophies", "get_trophies"), ("player_sidelined", "get_sidelined")):
            with self.subTest(focus=focus):
                operation = compile_football_operation(
                    "FOOTBALL_PLAYER_QUERY",
                    "trofeos de Julian Brandt",
                    {"data_focus": focus, "player_candidates": ["Julian Brandt"]},
                )
                client = _FakeFootballClient()
                result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=focus)

                self.assertEqual(result.outcome, FootballOutcome.SELECTED)
                calls = [name for name, _kwargs in client.calls]
                self.assertLess(calls.index("search_player_profiles"), calls.index(expected_call))

    async def test_coach_and_venue_recipes_use_team_resolution(self) -> None:
        for focus, expected in (("current_coach", "search_coaches"), ("venue", "search_teams")):
            with self.subTest(focus=focus):
                operation = compile_football_operation(
                    "FOOTBALL_LOOKUP",
                    "coach de Mexico" if focus == "current_coach" else "estadio de Mexico",
                    {"data_focus": focus, "team_candidates": ["Mexico"]},
                )
                client = _FakeFootballClient()
                result = await FootballOperationService(client).execute(operation, league_id=None, season=None, data_focus=focus)

                self.assertEqual(result.outcome, FootballOutcome.SELECTED)
                self.assertIn(expected, [name for name, _kwargs in client.calls])


if __name__ == "__main__":
    unittest.main()
