from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
import logging
import re
from typing import Any, Awaitable, Callable

from services import football_resolver
from services.football_api_request_compiler import (
    InvalidFootballApiRequest,
    build_coach_search_request,
    build_fixture_rounds_request,
    build_injury_request,
    build_odds_request,
    build_player_teams_request,
    build_player_seasons_request,
    build_player_squads_request,
    build_player_stats_request,
    build_prediction_request,
    build_sidelined_request,
    build_team_statistics_request,
    build_trophy_request,
    build_transfer_request,
    build_venue_search_request,
)
from services.football_live_match_service import FootballLiveMatchService, MatchData, build_match_data, compact_match_data, filter_fixtures, normalize_match_statistics
from services.football_query_service import FootballQueryOperation
from services.api_football import FootballApiError


class FootballOutcome(StrEnum):
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    NO_DATA_FOR_SCOPE = "NO_DATA_FOR_SCOPE"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    UNSUPPORTED_BY_COVERAGE = "UNSUPPORTED_BY_COVERAGE"


@dataclass
class FootballRetrievalResult:
    outcome: FootballOutcome = FootballOutcome.SELECTED
    fixtures: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    statistics: list[dict[str, Any]] = field(default_factory=list)
    lineups: list[dict[str, Any]] = field(default_factory=list)
    fixture_players: list[dict[str, Any]] = field(default_factory=list)
    match_center: dict[str, Any] | None = None
    normalized_events: list[dict[str, Any]] = field(default_factory=list)
    shootout: dict[str, Any] | None = None
    half_statistics: dict[str, Any] = field(default_factory=dict)
    player_performances: list[dict[str, Any]] = field(default_factory=list)
    team_season_statistics: dict[str, Any] | None = None
    rounds: list[dict[str, Any]] = field(default_factory=list)
    venues: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    odds: list[dict[str, Any]] = field(default_factory=list)
    coach_context_row: dict[str, Any] | None = None
    trophies: list[dict[str, Any]] = field(default_factory=list)
    sidelined: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    reference_rows: list[dict[str, Any]] = field(default_factory=list)
    player_context_row: dict[str, Any] | None = None
    player_stat_focus: str | None = None
    team_context_row: dict[str, Any] | None = None
    league_context_row: dict[str, Any] | None = None
    standings_table: list[dict[str, Any]] = field(default_factory=list)
    standing_row: dict[str, Any] | None = None
    generic_rows: list[dict[str, Any]] = field(default_factory=list)
    generic_label: str | None = None
    endpoints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    football_entity_context: dict[str, object] | None = None
    match_data: MatchData | None = None
    ambiguity_candidates: list[dict[str, Any]] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    requested_scope: dict[str, Any] = field(default_factory=dict)
    resolved_entities: dict[str, Any] = field(default_factory=dict)


FootballPlayerCanonicalizer = Callable[[football_resolver.PlayerQuery], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class FootballRecipe:
    key: str
    aliases: tuple[str, ...]
    required_inputs: tuple[str, ...]
    permitted_endpoints: tuple[str, ...]
    output_payload: tuple[str, ...]
    failure_outcomes: tuple[FootballOutcome, ...]
    handler_name: str
    required_slots: tuple[str, ...] = ()
    allowed_optional_slots: tuple[str, ...] = ()
    forbidden_slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class FootballRequestPlan:
    canonical_operation: str
    requested_operation: str
    requested_data_focus: str | None
    temporal_semantics: str | None
    required_slots: tuple[str, ...]
    forbidden_slots: tuple[str, ...]
    permitted_endpoints: tuple[str, ...]
    entity_evidence: dict[str, list[dict[str, Any]]]
    capability_evidence: dict[str, Any]


class FootballOperationService:
    RECIPES: dict[str, FootballRecipe] = {
        "player": FootballRecipe(
            key="player",
            aliases=("player_profile", "player_recent_stats", "player_current_team", "player_previous_team", "player_career_history", "player_teams", "player_transfers", "player_injuries", "player_trophies", "player_sidelined"),
            required_inputs=("player",),
            permitted_endpoints=("/players/profiles", "/players/seasons", "/players", "/players/squads", "/players/teams", "/transfers", "/injuries", "/trophies", "/sidelined"),
            output_payload=("player_context_row", "generic_rows", "football_entity_context"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED, FootballOutcome.UNSUPPORTED_BY_COVERAGE),
            handler_name="_execute_player",
            required_slots=("player",),
            allowed_optional_slots=("team", "league", "country"),
            forbidden_slots=("opponent", "fixture"),
        ),
        "fixture_list": FootballRecipe(
            key="fixture_list",
            aliases=("fixture_next", "fixture_last"),
            required_inputs=("team_if_scoped",),
            permitted_endpoints=("/leagues", "/teams", "/fixtures"),
            output_payload=("fixtures", "team_context_row"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_fixture_list",
            allowed_optional_slots=("team", "league", "country"),
            forbidden_slots=("player", "opponent"),
        ),
        "fixture_match": FootballRecipe(
            key="fixture_match",
            aliases=("fixture_live", "fixture_events", "fixture_statistics", "fixture_lineups", "player_match_stats", "live_watch_start"),
            required_inputs=("fixture",),
            permitted_endpoints=("/leagues", "/teams", "/fixtures", "/fixtures?live=all", "/fixtures?date", "/fixtures?last", "/fixtures?next", "/fixtures/events", "/fixtures/statistics", "/fixtures/lineups", "/fixtures/players"),
            output_payload=("fixtures", "events", "statistics", "lineups", "fixture_players", "match_data"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_fixture_match",
            allowed_optional_slots=("team", "opponent", "league", "country", "fixture"),
            forbidden_slots=("player",),
        ),
        "team_fixture_result": FootballRecipe(
            key="team_fixture_result",
            aliases=("team_fixture_result",),
            required_inputs=("team",),
            permitted_endpoints=("/leagues", "/teams", "/fixtures", "/fixtures?date", "/fixtures?last", "/fixtures/events", "/fixtures/statistics", "/fixtures/lineups"),
            output_payload=("fixtures", "events", "statistics", "lineups", "match_data", "team_context_row", "football_entity_context"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_fixture_match",
            required_slots=("team",),
            allowed_optional_slots=("opponent", "league", "country", "fixture"),
            forbidden_slots=("player",),
        ),
        "league_fixture_results": FootballRecipe(
            key="league_fixture_results",
            aliases=("league_fixture_results",),
            required_inputs=("league", "date"),
            permitted_endpoints=("/leagues", "/fixtures?date"),
            output_payload=("fixtures", "league_context_row", "football_entity_context"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_league_fixture_results",
            required_slots=("league",),
            allowed_optional_slots=("country",),
            forbidden_slots=("team", "opponent", "player", "fixture"),
        ),
        "fixture_result_by_id": FootballRecipe(
            key="fixture_result_by_id",
            aliases=("fixture_result_by_id",),
            required_inputs=("fixture",),
            permitted_endpoints=("/fixtures", "/fixtures/events", "/fixtures/statistics", "/fixtures/lineups"),
            output_payload=("fixtures", "events", "statistics", "lineups", "match_data"),
            failure_outcomes=(FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_fixture_match",
            required_slots=("fixture",),
            allowed_optional_slots=(),
            forbidden_slots=("team", "opponent", "league", "country", "player"),
        ),
        "team": FootballRecipe(
            key="team",
            aliases=("team_profile", "team_squad", "team_injuries", "team_transfers", "injuries", "transfers"),
            required_inputs=("team",),
            permitted_endpoints=("/teams", "/standings", "/fixtures", "/players/squads", "/injuries", "/transfers"),
            output_payload=("team_context_row", "standing_row", "fixtures", "generic_rows"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_team",
            required_slots=("team",),
            allowed_optional_slots=("league", "country"),
            forbidden_slots=("player", "opponent", "fixture"),
        ),
        "league": FootballRecipe(
            key="league",
            aliases=("standings", "top_scorers", "top_assists", "top_yellow_cards", "top_red_cards", "league_lookup"),
            required_inputs=("league", "season"),
            permitted_endpoints=("/leagues", "/standings", "/players/topscorers", "/players/topassists", "/players/topyellowcards", "/players/topredcards"),
            output_payload=("league_context_row", "standings_table", "generic_rows"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_league",
            required_slots=("league",),
            allowed_optional_slots=("country",),
            forbidden_slots=("team", "opponent", "player", "fixture"),
        ),
        "h2h": FootballRecipe(
            key="h2h",
            aliases=("h2h",),
            required_inputs=("team", "opponent"),
            permitted_endpoints=("/teams", "/fixtures/headtohead"),
            output_payload=("generic_rows",),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_h2h",
            required_slots=("team", "opponent"),
            allowed_optional_slots=("league", "country"),
            forbidden_slots=("player", "fixture"),
        ),
        "team_statistics": FootballRecipe(
            key="team_statistics",
            aliases=("team_season_statistics",),
            required_inputs=("team", "league", "season"),
            permitted_endpoints=("/teams", "/leagues", "/teams/statistics"),
            output_payload=("team_context_row", "league_context_row", "team_season_statistics"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_team_statistics",
            required_slots=("team",),
            allowed_optional_slots=("league", "country"),
            forbidden_slots=("player", "opponent", "fixture"),
        ),
        "competition": FootballRecipe(
            key="competition",
            aliases=("competition_rounds", "competition_current_round", "competition_round_fixtures", "competition_structure"),
            required_inputs=("league", "season"),
            permitted_endpoints=("/leagues", "/fixtures/rounds", "/fixtures?date"),
            output_payload=("league_context_row", "rounds", "fixtures"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_competition",
            required_slots=("league",),
            allowed_optional_slots=("country",),
            forbidden_slots=("team", "opponent", "player", "fixture"),
        ),
        "fixture_optional": FootballRecipe(
            key="fixture_optional",
            aliases=("match_center", "fixture_status", "fixture_shootout", "fixture_shootout_attempts", "fixture_statistics_half", "fixture_statistics_half_comparison", "fixture_prediction", "fixture_odds_pre_match", "fixture_odds_live"),
            required_inputs=("fixture",),
            permitted_endpoints=("/teams", "/fixtures", "/fixtures?live=all", "/fixtures?date", "/fixtures?last", "/fixtures?next", "/fixtures/events", "/fixtures/statistics", "/fixtures/statistics?half=true", "/fixtures/lineups", "/fixtures/players", "/predictions", "/odds", "/odds/live"),
            output_payload=("match_data", "match_center", "events", "statistics", "lineups", "fixture_players", "predictions", "odds"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED, FootballOutcome.UNSUPPORTED_BY_COVERAGE),
            handler_name="_execute_fixture_optional",
            allowed_optional_slots=("team", "opponent", "league", "country", "fixture"),
            forbidden_slots=("player",),
        ),
        "coach": FootballRecipe(
            key="coach",
            aliases=("coach_profile", "team_current_coach", "coach_career", "coach_trophies", "coach_sidelined"),
            required_inputs=("coach_or_team",),
            permitted_endpoints=("/teams", "/coachs", "/trophies", "/sidelined"),
            output_payload=("team_context_row", "coach_context_row", "generic_rows", "trophies", "sidelined"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_coach",
            allowed_optional_slots=("team", "country"),
            forbidden_slots=("player", "opponent", "fixture"),
        ),
        "venue": FootballRecipe(
            key="venue",
            aliases=("venue_lookup", "team_venue"),
            required_inputs=("venue_or_team",),
            permitted_endpoints=("/teams", "/venues"),
            output_payload=("team_context_row", "venues"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_venue",
            allowed_optional_slots=("team", "country"),
            forbidden_slots=("player", "opponent", "fixture"),
        ),
        "reference": FootballRecipe(
            key="reference",
            aliases=("football_countries", "football_timezones", "league_seasons", "team_countries", "odds_bookmakers", "odds_bets", "odds_live_bets"),
            required_inputs=("reference_scope",),
            permitted_endpoints=("/teams/countries", "/odds/bookmakers", "/odds/bets", "/odds/live/bets"),
            output_payload=("reference_rows",),
            failure_outcomes=(FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED, FootballOutcome.UNSUPPORTED),
            handler_name="_execute_reference",
            allowed_optional_slots=("league", "team", "country"),
            forbidden_slots=("player", "opponent", "fixture"),
        ),
    }
    _ALIAS_TO_RECIPE: dict[str, str] = {
        alias: key
        for key, recipe in RECIPES.items()
        for alias in recipe.aliases
    }

    def __init__(
        self,
        client: Any,
        *,
        player_canonicalizer: FootballPlayerCanonicalizer | None = None,
        player_alias_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.client = client
        self.player_canonicalizer = player_canonicalizer
        self.player_alias_cache = player_alias_cache

    @classmethod
    def canonical_operation_key(cls, operation: FootballQueryOperation) -> str | None:
        focus = str(operation.data_focus or "").casefold()
        op_type = str(operation.operation_type or "").casefold()
        if op_type == "fixture_result":
            if operation.team_slots:
                return "team_fixture_result"
            if operation.league_slots:
                return "league_fixture_results"
            if operation.fixture_focus:
                return "fixture_result_by_id"
        if focus in {"top_assists", "assists"}:
            return "league"
        if focus in {"top_yellow_cards", "yellowcards", "yellow_cards"}:
            return "league"
        if focus in {"top_red_cards", "redcards", "red_cards"}:
            return "league"
        if focus in {"team_season_statistics", "team_stats", "team_statistics"}:
            return "team_statistics"
        if focus in {"season_start", "rounds", "current_round", "round_fixtures", "competition_rounds", "competition_current_round", "competition_round_fixtures", "competition_structure"}:
            return "competition"
        if focus in {"prediction", "predictions", "odds", "live_odds", "shootout", "match_center", "half_statistics", "fixture_players"}:
            return "fixture_optional"
        if focus in {"coach", "current_coach", "coach_career", "coach_trophies", "coach_sidelined"}:
            return "coach"
        if focus in {"venue", "stadium", "team_venue"}:
            return "venue"
        if focus in {"countries", "timezones", "league_seasons", "team_countries", "bookmakers", "bets", "live_bets"}:
            return "reference"
        return cls._ALIAS_TO_RECIPE.get(op_type)

    @classmethod
    def recipe_for(cls, operation: FootballQueryOperation) -> FootballRecipe | None:
        key = cls.canonical_operation_key(operation)
        return cls.RECIPES.get(key or "")

    async def resolve_team_rows(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        max_teams: int = 2,
    ) -> tuple[list[dict[str, Any]], FootballOutcome]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        saw_ambiguous = False
        saw_candidate = False
        for use_search_fallback in (False, True):
            for slot in operation.team_slots:
                saw_candidate = True
                try:
                    lookup = await football_resolver.resolve_team_candidate(
                        self.client,
                        slot,
                        league_id=league_id,
                        season=season,
                        allow_global=True,
                        use_search_fallback=use_search_fallback,
                    )
                except (AttributeError, TypeError):
                    return rows, FootballOutcome.RESOLUTION_FAILED
                for row in lookup.rows:
                    team = row.get("team") if isinstance(row, dict) else {}
                    team_id = team.get("id") if isinstance(team, dict) else None
                    if isinstance(team_id, int):
                        if team_id in seen_ids:
                            continue
                        seen_ids.add(team_id)
                    rows.append(row)
                if lookup.resolution.ambiguous:
                    saw_ambiguous = True
            selected, outcome = _select_team_rows(rows, operation.team_slots, max_teams=max_teams)
            if selected:
                return selected, outcome
        selected, outcome = _select_team_rows(rows, operation.team_slots, max_teams=max_teams)
        if selected:
            return selected, outcome
        if rows:
            return rows[:5], FootballOutcome.AMBIGUOUS if saw_ambiguous else FootballOutcome.NOT_FOUND
        if saw_ambiguous:
            return rows, FootballOutcome.AMBIGUOUS
        return rows, FootballOutcome.NOT_FOUND if saw_candidate else FootballOutcome.RESOLUTION_FAILED

    async def resolve_team(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
    ) -> tuple[dict[str, Any] | None, FootballOutcome]:
        rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=1)
        return (rows[0] if rows else None), outcome

    async def resolve_league_key(self, league_key: str) -> tuple[int, int]:
        league_id = await self.client.resolve_league_id(league_key)
        season = await self.client.get_current_season(league_id)
        return league_id, season

    async def execute(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        recipe = self.recipe_for(operation)
        if recipe is None:
            return FootballRetrievalResult(
                outcome=FootballOutcome.UNSUPPORTED,
                notes=["unsupported_recipe"],
                requested_scope=_requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus),
            )
        request_plan = _build_request_plan(operation, recipe, data_focus=data_focus)
        preflight_error = _validate_request_plan(request_plan, operation)
        if preflight_error is not None:
            preflight_error.requested_scope = _requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus)
            preflight_error.requested_scope["request_plan"] = _request_plan_payload(request_plan)
            return preflight_error
        invalid = _validate_recipe_slots(operation, recipe)
        if invalid is not None:
            invalid.requested_scope = _requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus)
            invalid.requested_scope["request_plan"] = _request_plan_payload(request_plan)
            return invalid
        handler = getattr(self, recipe.handler_name)
        result = await handler(operation, league_id=league_id, season=season, data_focus=data_focus)
        if not result.requested_scope:
            result.requested_scope = _requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus)
        result.requested_scope.setdefault("request_plan", _request_plan_payload(request_plan))
        _filter_unpermitted_endpoints(result, recipe)
        return result

    async def _execute_fixture_list(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult(endpoints=[])
        league_row = None
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                result.outcome = league_outcome
                result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                    result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                else:
                    result.missing_inputs.append("league")
                return result
        team_row = None
        team_id = None
        if operation.team_slots:
            team_row, team_outcome = await self.resolve_team(operation, league_id=league_id, season=season)
            result.endpoints.append("/teams")
            if team_outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = team_outcome
                result.notes.append("team_ambiguous" if team_outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                if team_row is not None:
                    result.generic_rows = [team_row]
                if team_outcome == FootballOutcome.AMBIGUOUS:
                    result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                else:
                    result.missing_inputs.append("team")
                return result
            team = team_row.get("team") if isinstance(team_row, dict) else {}
            team_id = team.get("id") if isinstance(team, dict) else None
            result.team_context_row = team_row
            if not isinstance(team_id, int):
                result.outcome = FootballOutcome.RESOLUTION_FAILED
                result.notes.append("team_id_missing")
                return result
        if season is None and league_id is not None:
            season = await self.client.get_current_season(league_id)
        focus_key = str(data_focus or "")
        fixture_count = 1 if focus_key.startswith("single_") else (5 if focus_key.startswith("schedule_") or data_focus == "season_start" else 3)
        if operation.operation_type == "fixture_last":
            result.fixtures = await self.client.get_last_fixtures(league_id=league_id, season=season, last_count=fixture_count, team_id=team_id)
        else:
            result.fixtures = await self.client.get_next_fixtures(
                league_id=league_id,
                season=season,
                next_count=fixture_count,
                team_id=team_id,
            )
        result.endpoints.append("/fixtures")
        if not result.fixtures and (team_id is not None or league_id is not None):
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("fixture_list_missing_for_scope")
        return result

    async def _execute_h2h(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=2)
        result.endpoints.append("/teams")
        if outcome != FootballOutcome.SELECTED or len(rows) < 2:
            result.outcome = outcome if outcome != FootballOutcome.SELECTED else FootballOutcome.RESOLUTION_FAILED
            result.notes.append("h2h_teams_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "h2h_teams_not_found")
            result.generic_rows = rows
            if outcome == FootballOutcome.AMBIGUOUS:
                result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
            else:
                result.missing_inputs.extend(["team", "opponent"])
            return result
        team_ids = [row.get("team", {}).get("id") for row in rows if isinstance(row.get("team"), dict)]
        if len(team_ids) < 2 or not all(isinstance(item, int) for item in team_ids[:2]):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("h2h_team_id_missing")
            return result
        result.generic_rows = await self.client.get_head_to_head(team_a_id=team_ids[0], team_b_id=team_ids[1], last=10)
        result.generic_label = "FOOTBALL_H2H"
        result.endpoints.append("/fixtures/headtohead")
        return result

    async def _execute_team(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                result.outcome = league_outcome
                result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                    result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                else:
                    result.missing_inputs.append("league")
                return result
        team_row, outcome = await self.resolve_team(operation, league_id=league_id, season=season)
        result.endpoints.append("/teams")
        if outcome != FootballOutcome.SELECTED or team_row is None:
            result.outcome = outcome
            result.notes.append("team_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
            if team_row is not None:
                result.generic_rows = [team_row]
            if outcome == FootballOutcome.AMBIGUOUS:
                result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
            else:
                result.missing_inputs.append("team")
            return result
        result.team_context_row = team_row
        team = team_row.get("team") if isinstance(team_row, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_id_missing")
            return result
        if operation.operation_type == "team_profile":
            if league_id is not None:
                if season is None:
                    season = await self.client.get_current_season(league_id)
                standings_raw = await self.client.get_standings(league_id=league_id, season=season)
                result.standings_table = football_resolver.extract_table_rows(standings_raw)
                result.standing_row = football_resolver.find_team_row(result.standings_table, team_id)
                result.endpoints.append("/standings")
            result.fixtures = await self.client.get_next_fixtures(league_id=league_id, season=season, next_count=1, team_id=team_id)
            if not result.fixtures:
                result.fixtures = await self.client.get_last_fixtures(league_id=league_id, season=season, last_count=1, team_id=team_id)
            result.endpoints.append("/fixtures")
        elif operation.operation_type in {"injuries", "team_injuries"}:
            if season is None and league_id is not None:
                season = await self.client.get_current_season(league_id)
            result.generic_rows = await self.client.get_injuries(build_injury_request(league_id=league_id, season=season, team_id=team_id))
            result.generic_label = "FOOTBALL_INJURIES"
            result.endpoints.append("/injuries")
        elif operation.operation_type == "team_squad":
            result.generic_rows = await self.client.get_player_squads(build_player_squads_request(team_id=team_id))
            result.generic_label = "FOOTBALL_TEAM_SQUAD"
            result.endpoints.append("/players/squads")
        else:
            result.generic_rows = await self.client.get_transfers(build_transfer_request(team_id=team_id))
            result.generic_label = "FOOTBALL_TRANSFERS"
            result.endpoints.append("/transfers")
        if operation.operation_type in {"team_squad", "team_injuries", "team_transfers", "injuries", "transfers"} and not result.generic_rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
        return result

    async def resolve_league_and_season(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
    ) -> tuple[int | None, int | None, dict[str, Any] | None, FootballOutcome]:
        if league_id is not None:
            if season is None:
                season = await self.client.get_current_season(league_id)
            return league_id, _season_from_operation_hint(operation, season), None, FootballOutcome.SELECTED
        if not operation.league_slots:
            return None, season, None, FootballOutcome.RESOLUTION_FAILED
        for slot in operation.league_slots:
            league_key = football_resolver.normalize_league_key(slot.name) if football_resolver.slot_allows_static_alias(slot) else None
            if league_key:
                resolved_id, resolved_season = await self.resolve_league_key(league_key)
                return resolved_id, _season_from_operation_hint(operation, season or resolved_season), {"league": {"name": football_resolver.league_label(league_key, "en")}}, FootballOutcome.SELECTED
        rows: list[dict[str, Any]] = []
        saw_ambiguous = False
        for slot in operation.league_slots:
            lookup = await football_resolver.resolve_league_candidate(
                self.client,
                slot,
                country=operation.country_slots[0] if operation.country_slots else None,
                season=season,
            )
            if lookup.matches:
                rows.extend(row for row in lookup.matches if isinstance(row, dict))
            elif lookup.row is not None:
                rows.append(lookup.row)
            if lookup.ambiguous:
                saw_ambiguous = True
                continue
            if lookup.league_id is not None:
                return lookup.league_id, _season_from_operation_hint(operation, lookup.season or season), lookup.row, FootballOutcome.SELECTED
        picked = _select_league_row(rows, operation.league_slots)
        if picked.ambiguous:
            return None, season, picked.matches[0] if picked.matches else None, FootballOutcome.AMBIGUOUS
        league = picked.selected.get("league") if isinstance(picked.selected, dict) else {}
        selected_id = league.get("id") if isinstance(league, dict) else None
        if isinstance(selected_id, int):
            resolved_season = season or await self.client.get_current_season(selected_id)
            return selected_id, _season_from_operation_hint(operation, resolved_season), picked.selected, FootballOutcome.SELECTED
        return None, season, rows[0] if rows else None, FootballOutcome.AMBIGUOUS if saw_ambiguous else FootballOutcome.NOT_FOUND

    async def _resolve_season_for_target_date(
        self,
        league_id: int,
        target_date_iso: str,
        *,
        prior_season: int | None,
        league_row: dict[str, Any] | None,
    ) -> tuple[int | None, FootballOutcome, list[str]]:
        notes: list[str] = []
        seasons = _season_metadata_from_rows([league_row] if league_row else [])
        if not seasons:
            try:
                rows = await self.client.get_league_by_id(league_id=league_id)
            except (AttributeError, FootballApiError, InvalidFootballApiRequest) as exc:
                return None, FootballOutcome.RESOLUTION_FAILED, [f"season_metadata_unavailable={str(exc)[:80]}"]
            seasons = _season_metadata_from_rows(rows)
        compatible = _seasons_containing_date(seasons, target_date_iso)
        notes.append(
            "season_resolution="
            + ",".join(
                f"{item.get('year')}:{item.get('start')}..{item.get('end')}"
                for item in seasons[:6]
                if item.get("year") is not None
            )[:240]
        )
        if prior_season is not None and any(item.get("year") == prior_season for item in compatible):
            notes.append("season_prior_reused")
            return prior_season, FootballOutcome.SELECTED, notes
        if len(compatible) == 1 and isinstance(compatible[0].get("year"), int):
            selected = int(compatible[0]["year"])
            notes.append(f"season_selected_for_date={selected}")
            return selected, FootballOutcome.SELECTED, notes
        if len(compatible) > 1:
            notes.append("season_ambiguous_for_date")
            return None, FootballOutcome.AMBIGUOUS, notes
        notes.append("season_not_compatible_with_target_date")
        return None, FootballOutcome.RESOLUTION_FAILED, notes

    async def _execute_league(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        league_id, season, league_row, outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
        result.league_context_row = league_row
        result.endpoints.append("/leagues")
        if outcome != FootballOutcome.SELECTED or league_id is None or season is None:
            result.outcome = outcome
            result.notes.append("league_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
            if outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
            else:
                result.missing_inputs.append("league")
            return result
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        if operation.operation_type == "league_lookup":
            result.football_entity_context = {
                "entity_type": "league",
                "league_id": league_id,
                "season": season,
                "operation_type": operation.operation_type,
            }
            return result
        if operation.operation_type == "standings":
            standings_raw = await self.client.get_standings(league_id=league_id, season=season)
            result.standings_table = football_resolver.extract_table_rows(standings_raw)
            result.endpoints.append("/standings")
            return result
        if focus in {"top_assists", "assists", "asistencias"}:
            result.generic_rows = await self.client.get_top_assists(league_id=league_id, season=season)
            result.generic_label = "FOOTBALL_TOP_ASSISTS"
            result.endpoints.append("/players/topassists")
        elif focus in {"top_yellow_cards", "yellowcards", "yellow_cards", "yellow", "amarillas"}:
            result.generic_rows = await self.client.get_top_yellow_cards(league_id=league_id, season=season)
            result.generic_label = "FOOTBALL_TOP_YELLOW_CARDS"
            result.endpoints.append("/players/topyellowcards")
        elif focus in {"top_red_cards", "redcards", "red_cards", "red", "rojas"}:
            result.generic_rows = await self.client.get_top_red_cards(league_id=league_id, season=season)
            result.generic_label = "FOOTBALL_TOP_RED_CARDS"
            result.endpoints.append("/players/topredcards")
        else:
            result.generic_rows = await self.client.get_top_scorers(league_id=league_id, season=season)
            result.generic_label = "FOOTBALL_SCORERS"
            result.endpoints.append("/players/topscorers")
        return result

    async def _execute_league_fixture_results(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        resolved_league_id, resolved_season, league_row, outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
        result.league_context_row = league_row
        result.endpoints.append("/leagues")
        if outcome != FootballOutcome.SELECTED or resolved_league_id is None:
            result.outcome = outcome
            result.notes.append("league_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
            if outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
            else:
                result.missing_inputs.append("league")
            return result
        date_iso = _date_iso_for_operation(self.client, operation)
        if date_iso is None:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("date_scope_required")
            result.missing_inputs.append("date")
            result.resolved_entities["league"] = _entity_summary("league", league_row or {"league": {"id": resolved_league_id}})
            return result
        resolved_season, season_outcome, season_notes = await self._resolve_season_for_target_date(
            resolved_league_id,
            date_iso,
            prior_season=resolved_season,
            league_row=league_row,
        )
        result.notes.extend(season_notes)
        if season_outcome != FootballOutcome.SELECTED or resolved_season is None:
            result.outcome = season_outcome
            result.missing_inputs.append("season")
            result.resolved_entities["league"] = _entity_summary("league", league_row or {"league": {"id": resolved_league_id}})
            return result
        rows = await self.client.get_fixtures_on_date(
            league_id=resolved_league_id,
            season=resolved_season,
            date_iso=date_iso,
        )
        result.endpoints.append("/fixtures?date")
        filtered = [
            row
            for row in rows
            if _fixture_matches_date(row, date_iso)
            and _fixture_matches_league(row, resolved_league_id, league_row)
            and _fixture_has_result_status(row)
        ]
        result.fixtures = filtered
        result.resolved_entities["league"] = _entity_summary("league", league_row or {"league": {"id": resolved_league_id}})
        result.football_entity_context = {
            "entity_type": "league",
            "league_id": resolved_league_id,
            "league_name": _league_name_from_row(league_row),
            "season": resolved_season,
            "operation_type": "league_fixture_results",
            "time_scope": operation.time_scope,
            "date_hint": operation.date_hint,
            "date_iso": date_iso,
            "source_endpoint": "/fixtures?date",
            "ambiguous": False,
        }
        if not filtered:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("league_fixture_results_missing_for_date")
        return result

    async def _execute_team_statistics(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        resolved_league_id, resolved_season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
        result.endpoints.append("/leagues")
        result.league_context_row = league_row
        if league_outcome != FootballOutcome.SELECTED or resolved_league_id is None or resolved_season is None:
            result.outcome = league_outcome
            result.missing_inputs.append("league")
            return result
        team_row, team_outcome = await self.resolve_team(operation, league_id=resolved_league_id, season=resolved_season)
        result.endpoints.append("/teams")
        if team_outcome != FootballOutcome.SELECTED or team_row is None:
            result.outcome = team_outcome
            result.generic_rows = [team_row] if team_row else []
            if team_outcome == FootballOutcome.AMBIGUOUS:
                result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
            else:
                result.missing_inputs.append("team")
            return result
        team = team_row.get("team") if isinstance(team_row, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_id_missing")
            return result
        rows = await self.client.get_team_statistics(request=build_team_statistics_request(league_id=resolved_league_id, season=resolved_season, team_id=team_id))
        result.endpoints.append("/teams/statistics")
        result.team_context_row = team_row
        result.team_season_statistics = rows[0] if rows else None
        if not rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("team_statistics_missing_for_scope")
        return result

    async def _execute_competition(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        resolved_league_id, resolved_season, league_row, outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
        result.endpoints.append("/leagues")
        result.league_context_row = league_row
        if outcome != FootballOutcome.SELECTED or resolved_league_id is None or resolved_season is None:
            result.outcome = outcome
            result.missing_inputs.append("league")
            return result
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        if focus == "season_start":
            seasons = _season_metadata_from_rows([league_row] if league_row else [])
            if not seasons:
                try:
                    rows = await self.client.get_league_by_id(league_id=resolved_league_id)
                    result.endpoints.append("/leagues")
                except (AttributeError, FootballApiError, InvalidFootballApiRequest) as exc:
                    result.outcome = FootballOutcome.RESOLUTION_FAILED
                    result.missing_inputs.append("season")
                    result.notes.append(f"season_metadata_fetch_failed={str(exc)[:120]}")
                    return result
                seasons = _season_metadata_from_rows(rows)
            selected = [item for item in seasons if item.get("year") == resolved_season]
            result.generic_rows = selected or seasons[:5]
            result.generic_label = "FOOTBALL_COMPETITION_SEASON_START"
            result.league_context_row = league_row
            result.football_entity_context = {
                "entity_type": "league",
                "league_id": resolved_league_id,
                "league_name": _league_name_from_row(league_row),
                "season": resolved_season,
                "operation_type": "competition_structure",
                "data_focus": "season_start",
            }
            if not result.generic_rows:
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                result.notes.append("season_metadata_missing_for_scope")
            return result
        rounds = await self.client.get_fixture_rounds(
            build_fixture_rounds_request(
                league_id=resolved_league_id,
                season=resolved_season,
                current="current" in focus,
                include_dates=True,
            )
        )
        result.endpoints.append("/fixtures/rounds")
        result.rounds = rounds
        result.generic_rows = rounds
        result.generic_label = "FOOTBALL_COMPETITION_ROUNDS"
        if not rounds:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("rounds_missing_for_scope")
        return result

    async def _execute_fixture_optional(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        base_type = "player_match_stats" if focus in {"match_center", "fixture_players", "half_statistics"} else "fixture_events"
        base = await self._execute_fixture_match(replace(operation, operation_type=base_type), league_id=league_id, season=season, data_focus=data_focus)
        if base.outcome != FootballOutcome.SELECTED or base.match_data is None or base.match_data.fixture_id is None:
            return base
        fixture_id = base.match_data.fixture_id
        base.match_center = compact_match_data(base.match_data)
        base.normalized_events = [event.__dict__ for event in base.match_data.normalized_events]
        base.shootout = base.match_center.get("shootout") if isinstance(base.match_center, dict) else None
        base.player_performances = [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "player_id": item.player_id,
                "player_name": item.player_name,
                "metrics": item.metrics,
            }
            for item in base.match_data.player_performances
        ]
        if focus in {"half_statistics", "fixture_statistics_half", "fixture_statistics_half_comparison"}:
            detail = await FootballLiveMatchService(self.client).get_match_center(
                fixture_id,
                time_scope=base.match_data.time_scope,
                include_stats=True,
                include_half_stats=True,
            )
            base.endpoints.extend(endpoint for endpoint in detail.source_endpoints if endpoint not in base.endpoints)
            base.half_statistics = _compact_half_statistics(detail.half_statistics)
            if not base.half_statistics:
                base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                base.notes.append("half_statistics_missing_for_scope")
        if focus in {"prediction", "predictions"} or operation.operation_type == "fixture_prediction":
            unsupported = await self._coverage_gate(base, "predictions", league_id=league_id, season=season)
            if unsupported:
                return unsupported
            base.predictions = await self.client.get_predictions(build_prediction_request(fixture_id=fixture_id))
            base.endpoints.append("/predictions")
            if not base.predictions:
                base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                base.notes.append("prediction_missing_for_fixture")
        if focus in {"odds", "pre_match_odds"} or operation.operation_type == "fixture_odds_pre_match":
            unsupported = await self._coverage_gate(base, "odds", league_id=league_id, season=season)
            if unsupported:
                return unsupported
            base.odds = await self.client.get_odds(build_odds_request(fixture_id=fixture_id))
            base.endpoints.append("/odds")
            if not base.odds:
                base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                base.notes.append("odds_missing_for_fixture")
        if focus in {"live_odds"} or operation.operation_type == "fixture_odds_live":
            base.odds = await self.client.get_live_odds(build_odds_request(fixture_id=fixture_id, live=True))
            base.endpoints.append("/odds/live")
            if not base.odds:
                base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                base.notes.append("live_odds_missing_for_fixture")
        if focus in {"shootout", "shootout_attempts"} and (not base.shootout or not base.shootout.get("aggregate_available")):
            base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            base.notes.append("shootout_missing_for_fixture")
        return base

    async def _coverage_gate(
        self,
        result: FootballRetrievalResult,
        capability: str,
        *,
        league_id: int | None,
        season: int | None,
    ) -> FootballRetrievalResult | None:
        if capability not in {"events", "lineups", "statistics_fixtures", "statistics_players", "standings", "players", "top_scorers", "top_assists", "top_cards", "injuries", "predictions", "odds"}:
            return None
        fixture = result.match_data.fixture if result.match_data is not None else None
        league = fixture.get("league") if isinstance(fixture, dict) and isinstance(fixture.get("league"), dict) else {}
        resolved_league_id = league_id or (league.get("id") if isinstance(league.get("id"), int) else None)
        resolved_season = season or (league.get("season") if isinstance(league.get("season"), int) else None)
        method = getattr(self.client, "get_league_by_id", None)
        if method is None or not isinstance(resolved_league_id, int):
            return None
        rows = await method(league_id=resolved_league_id, season=resolved_season)
        if "/leagues" not in result.endpoints:
            result.endpoints.append("/leagues")
        coverage = _coverage_from_league_rows(rows)
        if not coverage:
            return None
        result.coverage = {"league_id": resolved_league_id, "season": resolved_season, **coverage}
        support = _coverage_supports(coverage, capability)
        if support is False:
            result.outcome = FootballOutcome.UNSUPPORTED_BY_COVERAGE
            result.notes.append(f"coverage_false={capability}")
            return result
        return None

    async def _execute_coach(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        team_id = None
        if operation.team_slots:
            team_row, outcome = await self.resolve_team(operation, league_id=league_id, season=season)
            result.endpoints.append("/teams")
            if outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = outcome
                result.generic_rows = [team_row] if team_row else []
                if outcome == FootballOutcome.AMBIGUOUS:
                    result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                return result
            result.team_context_row = team_row
            team = team_row.get("team") if isinstance(team_row, dict) else {}
            team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.missing_inputs.append("team")
            return result
        rows = await self.client.search_coaches(build_coach_search_request(team_id=team_id))
        result.endpoints.append("/coachs")
        result.generic_rows = rows
        result.coach_context_row = rows[0] if rows else None
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        coach = rows[0].get("coach") if rows and isinstance(rows[0].get("coach"), dict) else {}
        coach_id = coach.get("id") if isinstance(coach, dict) else None
        if isinstance(coach_id, int) and focus in {"coach_trophies"}:
            result.trophies = await self.client.get_trophies(build_trophy_request(coach_id=coach_id))
            result.endpoints.append("/trophies")
        if isinstance(coach_id, int) and focus in {"coach_sidelined"}:
            result.sidelined = await self.client.get_sidelined(build_sidelined_request(coach_id=coach_id))
            result.endpoints.append("/sidelined")
        if not rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("coach_missing_for_scope")
        return result

    async def _execute_venue(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        if operation.team_slots:
            team_row, outcome = await self.resolve_team(operation, league_id=league_id, season=season)
            result.endpoints.append("/teams")
            if outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = outcome
                result.generic_rows = [team_row] if team_row else []
                return result
            result.team_context_row = team_row
            venue = team_row.get("venue") if isinstance(team_row, dict) and isinstance(team_row.get("venue"), dict) else None
            if venue:
                result.venues = [venue]
                result.generic_rows = [venue]
                result.generic_label = "FOOTBALL_VENUE"
                return result
            team = team_row.get("team") if isinstance(team_row, dict) and isinstance(team_row.get("team"), dict) else {}
            team_name = str(team.get("name") or "").strip()
            if team_name and getattr(self.client, "search_venues", None) is not None:
                rows = await self.client.search_venues(build_venue_search_request(search=team_name))
                result.endpoints.append("/venues")
                result.venues = rows
                result.generic_rows = rows
                result.generic_label = "FOOTBALL_VENUE"
                if rows:
                    return result
        result.outcome = FootballOutcome.RESOLUTION_FAILED
        result.missing_inputs.append("team_or_venue")
        return result

    async def _execute_reference(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        if focus in {"team_countries", "countries"}:
            result.reference_rows = await self.client.get_team_countries()
            result.endpoints.append("/teams/countries")
        elif focus in {"bookmakers", "odds_bookmakers"}:
            from services.football_api_request_compiler import build_odds_reference_request

            result.reference_rows = await self.client.get_odds_bookmakers(build_odds_reference_request())
            result.endpoints.append("/odds/bookmakers")
        elif focus in {"bets", "odds_bets"}:
            from services.football_api_request_compiler import build_odds_reference_request

            result.reference_rows = await self.client.get_odds_bets(build_odds_reference_request())
            result.endpoints.append("/odds/bets")
        elif focus in {"live_bets", "odds_live_bets"}:
            from services.football_api_request_compiler import build_odds_reference_request

            result.reference_rows = await self.client.get_live_odds_bets(build_odds_reference_request())
            result.endpoints.append("/odds/live/bets")
        else:
            result.outcome = FootballOutcome.UNSUPPORTED
            result.notes.append("reference_scope_unsupported")
            return result
        result.generic_rows = result.reference_rows
        result.generic_label = "FOOTBALL_REFERENCE"
        if not result.reference_rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
        return result

    async def _execute_fixture_match(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        fixture_id = _fixture_id_from_operation(operation)
        if fixture_id is not None:
            include_events = operation.operation_type in {"fixture_events", "fixture_result", "player_match_stats", "fixture_result_by_id"}
            include_stats = operation.operation_type in {"fixture_statistics", "fixture_result", "player_match_stats", "fixture_result_by_id"}
            include_lineups = operation.operation_type in {"fixture_lineups", "fixture_result", "fixture_result_by_id"}
            include_players = operation.operation_type == "player_match_stats"
            detail = await FootballLiveMatchService(self.client).get_match_center(
                fixture_id,
                time_scope=operation.time_scope or "fixture_id",
                include_events=include_events,
                include_stats=include_stats,
                include_lineups=include_lineups,
                include_players=include_players,
            )
            result.match_data = detail
            result.fixtures = [detail.fixture] if detail.fixture is not None else []
            result.events = detail.events
            result.statistics = detail.statistics
            result.lineups = detail.lineups
            result.fixture_players = detail.players
            result.endpoints.extend(detail.source_endpoints)
            result.resolved_entities["fixture"] = {"entity_type": "fixture", "id": fixture_id}
            if detail.fixture_id is None:
                result.outcome = FootballOutcome.NOT_FOUND
                result.notes.append("fixture_not_found")
            return result
        target_date_iso = _date_iso_for_operation(self.client, operation)
        league_row = None
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                result.outcome = league_outcome
                result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                    result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                else:
                    result.missing_inputs.append("league")
                return result
        if league_id is not None and target_date_iso is not None:
            season, season_outcome, season_notes = await self._resolve_season_for_target_date(
                league_id,
                target_date_iso,
                prior_season=season,
                league_row=league_row,
            )
            result.notes.extend(season_notes)
            if season_outcome != FootballOutcome.SELECTED or season is None:
                result.outcome = season_outcome
                result.missing_inputs.append("season")
                return result
        team_row, team_outcome = (None, FootballOutcome.RESOLUTION_FAILED)
        opponent_row = None
        if not operation.team_slots:
            if operation.operation_type == "live_watch_start" or operation.live or data_focus == "live_fixtures":
                result.fixtures = await self.client.get_live_fixtures(league_id=league_id, cache_ttl_seconds=30)
                result.endpoints.append("/fixtures?live=all")
                return result
            if data_focus == "today_fixtures" or operation.time_scope == "today":
                if season is None and league_id is not None:
                    season = await self.client.get_current_season(league_id)
                result.fixtures = await self.client.get_fixtures_on_date(
                    league_id=league_id,
                    season=season,
                    date_iso=_today_iso(self.client),
                )
                result.endpoints.append("/fixtures?date")
                return result
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_required_for_fixture_resolution")
            return result
        if operation.team_slots:
            rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=2 if _has_opponent_slot(operation) else 1)
            result.endpoints.append("/teams")
            if outcome != FootballOutcome.SELECTED or not rows:
                result.outcome = outcome
                result.notes.append("team_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                result.generic_rows = rows
                if outcome == FootballOutcome.AMBIGUOUS:
                    result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                else:
                    result.missing_inputs.append("team")
                return result
            team_row = rows[0]
            opponent_row = rows[1] if len(rows) > 1 else None
            team_outcome = FootballOutcome.SELECTED
        if team_outcome != FootballOutcome.SELECTED or team_row is None:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_required_for_fixture_resolution")
            return result
        team = team_row.get("team") if isinstance(team_row, dict) else {}
        opponent = opponent_row.get("team") if isinstance(opponent_row, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        opponent_id = opponent.get("id") if isinstance(opponent, dict) else None
        if not isinstance(team_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_id_missing")
            return result
        result.team_context_row = team_row
        result.resolved_entities["team"] = _entity_summary("team", team_row)
        if league_id is not None:
            result.resolved_entities["league"] = _entity_summary("league", league_row or {"league": {"id": league_id}})
        if operation.operation_type == "fixture_result":
            result.football_entity_context = {
                "entity_type": "team",
                "team_id": team_id,
                "team_name": team.get("name") if isinstance(team, dict) else None,
                "opponent_id": opponent_id,
                "opponent_name": opponent.get("name") if isinstance(opponent, dict) else None,
                "league_id": league_id,
                "league_name": _league_name_from_row(league_row),
                "season": season,
                "operation_type": "team_fixture_result",
                "time_scope": operation.time_scope,
                "date_hint": operation.date_hint,
                "date_iso": target_date_iso,
                "context_kind": "validated_query",
                "source_endpoint": "/fixtures",
                "ambiguous": False,
            }
        match_service = FootballLiveMatchService(self.client)
        use_h2h_fixture = (
            isinstance(opponent_id, int)
            and not operation.live
            and (operation.time_scope not in {"live", "today"} or (operation.operation_type == "fixture_events" and not operation.date_hint))
            and operation.operation_type in {"fixture_result", "fixture_events", "fixture_statistics", "fixture_lineups"}
        )
        if use_h2h_fixture:
            if hasattr(self.client, "get_head_to_head"):
                h2h = await self.client.get_head_to_head(team_a_id=team_id, team_b_id=opponent_id, last=5)
                result.endpoints.append("/fixtures/headtohead")
                terminal_only = operation.operation_type in {"fixture_result", "fixture_statistics"}
                filtered = filter_fixtures(h2h, team_id=None, terminal_only=terminal_only)
                if filtered:
                    match = build_match_data(
                        filtered[0],
                        time_scope=operation.time_scope or "recent_finished",
                        selected_team_id=team_id,
                        selected_opponent_id=opponent_id,
                        source_endpoints=list(result.endpoints),
                    )
                else:
                    result.outcome = FootballOutcome.NOT_FOUND
                    result.notes.append("h2h_fixture_not_found")
                    return result
            else:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
                result.notes.append("h2h_endpoint_missing")
                return result
        else:
            match = await match_service.find_recent_fixture_for_team(
                team_id,
                "live" if operation.live else (operation.time_scope or ("live" if operation.operation_type == "fixture_live" else "recent_finished")),
                operation.date_hint,
                opponent_id=opponent_id,
                league_id=league_id,
                season=season,
                today_iso=_today_iso(self.client),
            )
        include_events = operation.operation_type in {"fixture_events", "fixture_result", "player_match_stats"}
        include_stats = operation.operation_type in {"fixture_statistics", "fixture_result", "player_match_stats"}
        include_lineups = operation.operation_type in {"fixture_lineups", "fixture_result"}
        include_players = operation.operation_type == "player_match_stats"
        result.endpoints.extend(match.source_endpoints)
        result.notes.extend(match.notes)
        if match.fixture_id is None:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE if operation.operation_type == "fixture_result" else FootballOutcome.NOT_FOUND
            result.notes.append("fixture_not_found")
            return result
        if include_events or include_stats or include_lineups or include_players:
            if not hasattr(self.client, "get_fixture_by_id"):
                events = await self.client.get_fixture_events(fixture_id=match.fixture_id) if include_events and hasattr(self.client, "get_fixture_events") else []
                stats = await self.client.get_fixture_statistics(fixture_id=match.fixture_id) if include_stats and hasattr(self.client, "get_fixture_statistics") else []
                lineups = await self.client.get_fixture_lineups(fixture_id=match.fixture_id) if include_lineups and hasattr(self.client, "get_fixture_lineups") else []
                detail = replace(
                    match,
                    events=events,
                    statistics=stats,
                    lineups=lineups,
                    stats=normalize_match_statistics(stats) if stats else match.stats,
                    source_endpoints=[
                        *match.source_endpoints,
                        *(["/fixtures/events"] if events else []),
                        *(["/fixtures/statistics"] if stats else []),
                        *(["/fixtures/lineups"] if lineups else []),
                    ],
                )
            else:
                detail = await match_service.get_match_center(
                    match.fixture_id,
                    time_scope=match.time_scope,
                    include_events=include_events,
                    include_stats=include_stats,
                    include_lineups=include_lineups,
                    include_players=include_players,
                )
        else:
            detail = match
        result.match_data = detail
        result.fixtures = [detail.fixture] if detail.fixture is not None else []
        result.events = detail.events
        result.statistics = detail.statistics
        result.lineups = detail.lineups
        result.fixture_players = detail.players
        if operation.operation_type == "fixture_result":
            result.football_entity_context["context_kind"] = "selected_fixture"  # type: ignore[index]
            result.football_entity_context["fixture_id"] = detail.fixture_id  # type: ignore[index]
        result.endpoints.extend(endpoint for endpoint in detail.source_endpoints if endpoint not in result.endpoints)
        return result

    async def _execute_player(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        player_slot = operation.player_slots[0] if operation.player_slots else None
        if player_slot is None:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("player_candidate_missing")
            return result
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                result.outcome = league_outcome
                result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                    result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                else:
                    result.missing_inputs.append("league")
                return result

        lookup = await football_resolver.resolve_player(
            self.client,
            player_slot,
            league_id=league_id,
            season=season,
            team_id=None,
            explicit_context=league_id is not None,
            canonicalizer=self.player_canonicalizer,
            alias_cache=self.player_alias_cache,
            candidate_names=list(operation.player_slots[:1]),
            stat_focus=operation.stat_focus,
            team_hint=player_slot.team_hint,
            league_hint=player_slot.league_hint,
            country_hint=player_slot.country_hint,
            nationality_hint=player_slot.nationality_hint,
        )
        result.endpoints.append("/players/profiles" if hasattr(self.client, "search_player_profiles") else "/players")
        result.player_stat_focus = lookup.query.stat_focus
        picked = lookup.resolution
        if picked.ambiguous:
            result.outcome = FootballOutcome.AMBIGUOUS
            result.generic_rows = list(picked.matches)
            result.ambiguity_candidates = _ambiguity_from_rows("player", result.generic_rows)
            names = _player_candidate_names(list(picked.matches))
            result.notes.append(f"player_ambiguous={', '.join(names)}")
            return result
        if picked.selected is None:
            result.outcome = FootballOutcome.NOT_FOUND
            result.notes.append("player_not_found")
            result.missing_inputs.append("player")
            return result

        player_row = picked.selected
        player = player_row.get("player") if isinstance(player_row, dict) else {}
        player_id = player.get("id") if isinstance(player, dict) else None
        if not isinstance(player_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("player_id_missing")
            return result

        result.player_context_row = player_row
        result.resolved_entities["player"] = _entity_summary("player", player_row)
        stats_rows = player_row.get("statistics") if isinstance(player_row, dict) else []
        team_id = None
        team_row = None
        if operation.team_slots:
            team_row, team_outcome = await self.resolve_team(operation, league_id=league_id, season=season)
            result.endpoints.append("/teams")
            if team_outcome == FootballOutcome.SELECTED and team_row is not None:
                team = team_row.get("team") if isinstance(team_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                result.team_context_row = team_row
                result.resolved_entities["team"] = _entity_summary("team", team_row)
            elif team_outcome == FootballOutcome.AMBIGUOUS:
                result.notes.append("team_hint_ambiguous")
                result.ambiguity_candidates = _ambiguity_from_rows("team", [team_row] if team_row is not None else [])
            else:
                result.notes.append("team_hint_not_found")

        if operation.operation_type == "player_current_team":
            squads = await self._player_squads(player_id)
            if squads:
                result.generic_rows = squads
                result.generic_label = "FOOTBALL_PLAYER_SQUADS"
                result.endpoints.append("/players/squads")

        if operation.operation_type in {"player_teams", "player_career_history"} and getattr(self.client, "get_player_teams", None) is not None:
            result.generic_rows = await self.client.get_player_teams(build_player_teams_request(player_id=player_id))
            result.generic_label = "FOOTBALL_PLAYER_TEAMS"
            result.endpoints.append("/players/teams")
        elif operation.operation_type == "player_trophies":
            result.trophies = await self.client.get_trophies(build_trophy_request(player_id=player_id))
            result.generic_rows = result.trophies
            result.generic_label = "FOOTBALL_PLAYER_TROPHIES"
            result.endpoints.append("/trophies")
        elif operation.operation_type == "player_sidelined":
            result.sidelined = await self.client.get_sidelined(build_sidelined_request(player_id=player_id))
            result.generic_rows = result.sidelined
            result.generic_label = "FOOTBALL_PLAYER_SIDELINED"
            result.endpoints.append("/sidelined")
        elif operation.operation_type == "player_transfers":
            result.generic_rows = await self.client.get_transfers(build_transfer_request(player_id=player_id))
            result.generic_label = "FOOTBALL_PLAYER_TRANSFERS"
            result.endpoints.append("/transfers")
        elif operation.operation_type == "player_injuries":
            result.generic_rows = await self.client.get_injuries(build_injury_request(player_id=player_id, league_id=league_id, season=season))
            result.generic_label = "FOOTBALL_PLAYER_INJURIES"
            result.endpoints.append("/injuries")

        needs_stats = operation.operation_type in {
            "player_recent_stats",
            "player_previous_team",
        } or (operation.operation_type in {"player_current_team", "player_career_history"} and not result.generic_rows)
        scoped_lookup_attempted = bool(league_id is not None or team_id is not None)
        if needs_stats and stats_rows and scoped_lookup_attempted and not _stats_match_scope(stats_rows, league_id=league_id, team_id=team_id):
            result.notes.append("player_found_stats_missing_for_scope")
            stats_rows = []
        if needs_stats and not stats_rows:
            stats_rows = await self._fetch_player_stats_for_locked_identity(
                player_id,
                league_id=league_id,
                season=season,
                team_id=team_id,
                result=result,
            )
            if not stats_rows and scoped_lookup_attempted:
                result.notes.append("player_found_stats_missing_for_scope")
            if stats_rows:
                result.player_context_row = stats_rows[0]
            elif "player_stats_season_unresolved" in result.notes:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
                if "season" not in result.missing_inputs:
                    result.missing_inputs.append("season")
            else:
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                result.notes.append("player_stats_missing_for_recent_scope")

        stats_for_context = result.player_context_row.get("statistics") if isinstance(result.player_context_row, dict) else []
        first_stats = stats_for_context[0] if isinstance(stats_for_context, list) and stats_for_context else {}
        team = first_stats.get("team") if isinstance(first_stats, dict) else {}
        league = first_stats.get("league") if isinstance(first_stats, dict) else {}
        result.notes.append(f"player_found={player.get('name') if isinstance(player, dict) else 'unknown'}")
        result.football_entity_context = {
            "entity_type": "player",
            "player_id": player_id,
            "player_name": player.get("name") if isinstance(player, dict) else None,
            "team_id": team.get("id") if isinstance(team, dict) else team_id,
            "team_name": team.get("name") if isinstance(team, dict) else None,
            "league_id": league.get("id") if isinstance(league, dict) else league_id,
            "league_name": league.get("name") if isinstance(league, dict) else None,
            "season": season,
            "operation_type": operation.operation_type,
            "data_focus": operation.data_focus,
            "source_endpoint": "/players/profiles" if hasattr(self.client, "search_player_profiles") else "/players",
            "ambiguous": False,
        }
        if lookup.query.stat_focus == "penalties":
            result.notes.append("penalty_specific_data=missing")
        if operation.operation_type in {"player_teams", "player_trophies", "player_sidelined", "player_transfers", "player_injuries"} and not result.generic_rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append(f"{operation.operation_type}_missing_for_scope")
        return result

    async def _player_squads(self, player_id: int) -> list[dict[str, Any]]:
        method = getattr(self.client, "get_player_squads", None)
        if method is None:
            return []
        return await method(build_player_squads_request(player_id=player_id))

    async def _fetch_player_stats_for_locked_identity(
        self,
        player_id: int,
        *,
        league_id: int | None,
        season: int | None,
        team_id: int | None,
        result: FootballRetrievalResult,
    ) -> list[dict[str, Any]]:
        stats_method = getattr(self.client, "get_player_stats", None)
        if stats_method is None:
            result.notes.append("player_stats_endpoint_missing")
            return []
        if getattr(self.client, "get_player_seasons", None) is not None:
            result.endpoints.append("/players/seasons")
        stat_seasons = await self._player_stat_seasons(player_id, requested_season=season)
        if not stat_seasons:
            result.notes.append("player_stats_season_unresolved")
            return []
        for stat_season in stat_seasons:
            rows = await stats_method(
                build_player_stats_request(
                    player_id=player_id,
                    season=stat_season,
                    league_id=league_id,
                    team_id=team_id,
                )
            )
            result.endpoints.append("/players?id")
            if rows:
                return rows
        return []

    async def _player_stat_seasons(self, player_id: int, *, requested_season: int | None) -> list[int]:
        ordered: list[int] = []
        if isinstance(requested_season, int):
            ordered.append(requested_season)
        method = getattr(self.client, "get_player_seasons", None)
        if method is not None:
            rows = await method(build_player_seasons_request(player_id=player_id))
            seasons = sorted(
                {
                    int(row.get("season"))
                    for row in rows
                    if isinstance(row, dict) and isinstance(row.get("season"), int)
                },
                reverse=True,
            )
            ordered.extend(season for season in seasons if season not in ordered)
        return ordered


def _player_candidate_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for item in rows[:5]:
        player = item.get("player") if isinstance(item, dict) else {}
        if isinstance(player, dict):
            names.append(f"{player.get('name')}#{player.get('id')}"[:120])
    return names


def _stats_match_scope(stats_rows: Any, *, league_id: int | None, team_id: int | None) -> bool:
    if not isinstance(stats_rows, list) or not stats_rows:
        return False
    for stat in stats_rows:
        if not isinstance(stat, dict):
            continue
        team = stat.get("team") if isinstance(stat.get("team"), dict) else {}
        league = stat.get("league") if isinstance(stat.get("league"), dict) else {}
        if team_id is not None and team.get("id") != team_id:
            continue
        if league_id is not None and league.get("id") != league_id:
            continue
        return True
    return False


def _validate_recipe_slots(operation: FootballQueryOperation, recipe: FootballRecipe) -> FootballRetrievalResult | None:
    present = _present_slots(operation)
    missing = [slot for slot in recipe.required_slots if slot not in present]
    forbidden = sorted(present.intersection(recipe.forbidden_slots))
    if operation.operation_type == "player_match_stats":
        forbidden = [slot for slot in forbidden if slot != "player"]
    if not missing and not forbidden:
        return None
    notes = []
    if missing:
        notes.append(f"missing_slots={','.join(missing)}")
    if forbidden:
        notes.append(f"forbidden_slots={','.join(forbidden)}")
    return FootballRetrievalResult(
        outcome=FootballOutcome.RESOLUTION_FAILED if missing else FootballOutcome.UNSUPPORTED,
        notes=notes,
        missing_inputs=missing,
    )


def _present_slots(operation: FootballQueryOperation) -> set[str]:
    present: set[str] = set()
    if operation.player_slots:
        present.add("player")
    if operation.team_slots:
        present.add("team")
    if _has_opponent_slot(operation):
        present.add("opponent")
    if operation.league_slots:
        present.add("league")
    if operation.country_slots:
        present.add("country")
    if operation.fixture_focus:
        present.add("fixture")
    return present


def _has_opponent_slot(operation: FootballQueryOperation) -> bool:
    if len(operation.team_slots) < 2:
        return False
    if operation.operation_type == "h2h":
        return True
    if operation.operation_type not in {
        "fixture_live",
        "fixture_result",
        "fixture_events",
        "fixture_statistics",
        "fixture_lineups",
        "player_match_stats",
    }:
        return False
    first = football_resolver.normalize_key(operation.team_slots[0].name)
    second = football_resolver.normalize_key(operation.team_slots[1].name)
    first_canonical = football_resolver.normalize_key(football_resolver.canonical_team_query(operation.team_slots[0].name))
    second_canonical = football_resolver.normalize_key(football_resolver.canonical_team_query(operation.team_slots[1].name))
    if first == second or first_canonical == second_canonical:
        return False
    if first and second and (first in second or second in first):
        return False
    return True


def _requested_scope(
    operation: FootballQueryOperation,
    *,
    league_id: int | None,
    season: int | None,
    data_focus: str | None,
) -> dict[str, Any]:
    return {
        "operation_type": operation.operation_type,
        "data_focus": data_focus or operation.data_focus,
        "time_scope": operation.time_scope,
        "date_hint": operation.date_hint,
        "season_hint": operation.season_hint,
        "league_id": league_id,
        "season": season,
    }


def _build_request_plan(
    operation: FootballQueryOperation,
    recipe: FootballRecipe,
    *,
    data_focus: str | None,
) -> FootballRequestPlan:
    intent = getattr(operation, "capability_intent", None) or getattr(getattr(operation, "spec", None), "capability_intent", None)
    return FootballRequestPlan(
        canonical_operation=recipe.key,
        requested_operation=str(operation.operation_type or ""),
        requested_data_focus=data_focus or operation.data_focus,
        temporal_semantics=str(getattr(intent, "temporal_semantics", None) or operation.time_scope or ""),
        required_slots=recipe.required_slots,
        forbidden_slots=recipe.forbidden_slots,
        permitted_endpoints=recipe.permitted_endpoints,
        entity_evidence=_slot_evidence(operation),
        capability_evidence={
            "operation_family": getattr(intent, "operation_family", operation.operation_type),
            "data_focus": getattr(intent, "data_focus", operation.data_focus),
            "evidence": getattr(intent, "evidence", None),
            "planner_operation": getattr(intent, "planner_operation", None),
            "planner_data_focus": getattr(intent, "planner_data_focus", None),
            "planner_accepted": getattr(intent, "planner_accepted", False),
        },
    )


def _validate_request_plan(plan: FootballRequestPlan, operation: FootballQueryOperation) -> FootballRetrievalResult | None:
    focus = str(plan.requested_data_focus or "").casefold()
    op_type = str(operation.operation_type or "").casefold()
    if focus == "season_start" and plan.canonical_operation != "competition":
        return FootballRetrievalResult(
            outcome=FootballOutcome.RESOLUTION_FAILED,
            notes=["capability_recipe_mismatch=season_start"],
            missing_inputs=["competition"],
        )
    if op_type == "fixture_next" and focus == "season_start":
        return FootballRetrievalResult(
            outcome=FootballOutcome.RESOLUTION_FAILED,
            notes=["capability_temporal_mismatch=season_start_next_fixture"],
            missing_inputs=["capability"],
        )
    if op_type == "fixture_next" and str(plan.temporal_semantics or "").casefold() in {"past", "today", "yesterday", "past_or_date"}:
        return FootballRetrievalResult(
            outcome=FootballOutcome.RESOLUTION_FAILED,
            notes=["capability_temporal_mismatch=next_with_date_scope"],
            missing_inputs=["capability"],
        )
    return None


def _request_plan_payload(plan: FootballRequestPlan) -> dict[str, Any]:
    return {
        "canonical_operation": plan.canonical_operation,
        "requested_operation": plan.requested_operation,
        "requested_data_focus": plan.requested_data_focus,
        "temporal_semantics": plan.temporal_semantics,
        "required_slots": list(plan.required_slots),
        "forbidden_slots": list(plan.forbidden_slots),
        "permitted_endpoints": list(plan.permitted_endpoints),
        "entity_evidence": plan.entity_evidence,
        "capability_evidence": plan.capability_evidence,
    }


def _slot_evidence(operation: FootballQueryOperation) -> dict[str, list[dict[str, Any]]]:
    return {
        "player": [_slot_evidence_row(slot, "player") for slot in operation.player_slots],
        "team": [_slot_evidence_row(slot, "team") for slot in operation.team_slots],
        "league": [_slot_evidence_row(slot, "league") for slot in operation.league_slots],
        "country": [_slot_evidence_row(slot, "country") for slot in operation.country_slots],
    }


def _slot_evidence_row(slot: Any, entity_type: str) -> dict[str, Any]:
    label = getattr(slot, "full_name", None) if entity_type == "player" else getattr(slot, "name", None)
    return {
        "entity_type": entity_type,
        "label": label,
        "literal": getattr(slot, "literal", None),
        "authority": getattr(slot, "authority", None),
        "source": getattr(slot, "source", None),
        "source_component": getattr(slot, "source_component", None),
        "evidence": getattr(slot, "evidence", None),
        "equivalent_to": getattr(slot, "equivalent_to", None),
    }


def _fixture_id_from_operation(operation: FootballQueryOperation) -> int | None:
    raw = str(operation.fixture_focus or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _season_from_operation_hint(operation: FootballQueryOperation, current_season: int | None) -> int | None:
    raw = str(getattr(operation, "season_hint", None) or "").strip().casefold()
    if not raw:
        return current_season
    match = re.search(r"\b(20\d{2}|19\d{2})\b", raw)
    if match:
        return int(match.group(1))
    if current_season is not None and any(marker in raw for marker in ("pasado", "previous", "last", "anterior")):
        return current_season - 1
    if current_season is not None and any(marker in raw for marker in ("actual", "current", "este")):
        return current_season
    return current_season


def _date_iso_for_operation(client: Any, operation: FootballQueryOperation) -> str | None:
    raw_hint = str(operation.date_hint or "").strip()
    if raw_hint and len(raw_hint) >= 10 and raw_hint[:10].count("-") == 2:
        return raw_hint[:10]
    today = _today_iso(client)
    if operation.time_scope == "yesterday" or football_resolver.normalize_key(raw_hint) in {"ayer", "yesterday"}:
        try:
            return (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        except ValueError:
            return None
    if operation.time_scope == "today" or football_resolver.normalize_key(raw_hint) in {"hoy", "today"}:
        return today
    return None


def _fixture_matches_date(fixture: dict[str, Any], date_iso: str) -> bool:
    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
    raw_date = str(fixture_obj.get("date") or "")
    return bool(raw_date) and raw_date[:10] == date_iso


def _fixture_matches_league(fixture: dict[str, Any], league_id: int, league_row: dict[str, Any] | None) -> bool:
    league = fixture.get("league") if isinstance(fixture, dict) else {}
    if not isinstance(league, dict):
        return False
    fixture_league_id = league.get("id")
    if isinstance(fixture_league_id, int):
        return fixture_league_id == league_id
    expected_name = football_resolver.normalize_key(_league_name_from_row(league_row))
    fixture_name = football_resolver.normalize_key(league.get("name"))
    if expected_name and fixture_name:
        return expected_name == fixture_name
    return True


def _fixture_has_result_status(fixture: dict[str, Any]) -> bool:
    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
    status = fixture_obj.get("status") if isinstance(fixture_obj, dict) else {}
    short = str(status.get("short") if isinstance(status, dict) else "").upper()
    return short in {"FT", "AET", "PEN"}


def _league_name_from_row(row: dict[str, Any] | None) -> str | None:
    league = row.get("league") if isinstance(row, dict) else {}
    name = league.get("name") if isinstance(league, dict) else None
    return str(name) if name else None


def _season_metadata_from_rows(rows: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        seasons = row.get("seasons") if isinstance(row, dict) else None
        if not isinstance(seasons, list):
            continue
        for item in seasons:
            if not isinstance(item, dict):
                continue
            year = item.get("year")
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            if isinstance(year, int) and start and end:
                result.append({"year": year, "start": start[:10], "end": end[:10]})
    return result


def _seasons_containing_date(seasons: list[dict[str, Any]], target_date_iso: str) -> list[dict[str, Any]]:
    try:
        target = date.fromisoformat(target_date_iso[:10])
    except ValueError:
        return []
    compatible: list[dict[str, Any]] = []
    for season in seasons:
        try:
            start = date.fromisoformat(str(season.get("start") or "")[:10])
            end = date.fromisoformat(str(season.get("end") or "")[:10])
        except ValueError:
            continue
        if start <= target <= end:
            compatible.append(season)
    return compatible


def _select_team_rows(
    rows: list[dict[str, Any]],
    slots: tuple[Any, ...],
    *,
    max_teams: int,
) -> tuple[list[dict[str, Any]], FootballOutcome]:
    if not rows or not slots:
        return [], FootballOutcome.NOT_FOUND
    if max_teams == 1:
        scored = sorted(
            ((max((_team_row_score(row, slot) for slot in slots), default=0), row) for row in rows),
            key=lambda item: item[0],
            reverse=True,
        )
        scored = [(score, row) for score, row in scored if score > 0]
        if not scored:
            return [], FootballOutcome.NOT_FOUND
        top_score = scored[0][0]
        top_rows = [row for score, row in scored if score == top_score]
        if len(top_rows) > 1:
            return top_rows[:5], FootballOutcome.AMBIGUOUS
        return [scored[0][1]], FootballOutcome.SELECTED
    selected: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for slot in slots[:max_teams]:
        scored = sorted(
            ((_team_row_score(row, slot), row) for row in rows),
            key=lambda item: item[0],
            reverse=True,
        )
        scored = [(score, row) for score, row in scored if score > 0 and _team_row_id(row) not in used_ids]
        if not scored:
            return [], FootballOutcome.NOT_FOUND
        top_score = scored[0][0]
        top_rows = [row for score, row in scored if score == top_score]
        if len(top_rows) > 1:
            return top_rows[:5], FootballOutcome.AMBIGUOUS
        row = scored[0][1]
        team_id = _team_row_id(row)
        if isinstance(team_id, int):
            used_ids.add(team_id)
        selected.append(row)
    return selected, FootballOutcome.SELECTED


def _team_row_score(row: dict[str, Any], slot: Any) -> int:
    team = row.get("team") if isinstance(row, dict) else {}
    if not isinstance(team, dict):
        return 0
    name_key = football_resolver.normalize_key(team.get("name"))
    slot_name = str(getattr(slot, "name", "") or "")
    slot_key = football_resolver.normalize_key(slot_name)
    canonical_key = football_resolver.normalize_key(football_resolver.canonical_team_query(slot_name))
    score = 0
    if name_key and name_key in {slot_key, canonical_key}:
        score += 100
    elif slot_key and slot_key in name_key:
        score += 60
    elif canonical_key and canonical_key in name_key:
        score += 55
    elif name_key:
        score += 1
    country_hint = football_resolver.normalize_key(getattr(slot, "country_hint", None))
    if country_hint and football_resolver.normalize_key(team.get("country")) == country_hint:
        score += 25
    return score


def _team_row_id(row: dict[str, Any]) -> int | None:
    team = row.get("team") if isinstance(row, dict) else {}
    team_id = team.get("id") if isinstance(team, dict) else None
    return team_id if isinstance(team_id, int) else None


def _select_league_row(rows: list[dict[str, Any]], slots: tuple[Any, ...]) -> football_resolver.FootballResolution:
    if not rows or not slots:
        return football_resolver.FootballResolution(None)
    scored: list[tuple[int, dict[str, Any]]] = []
    seen_ids: set[int] = set()
    for row in rows:
        league = row.get("league") if isinstance(row, dict) else {}
        league_id = league.get("id") if isinstance(league, dict) else None
        if isinstance(league_id, int) and league_id in seen_ids:
            continue
        if isinstance(league_id, int):
            seen_ids.add(league_id)
        score = max((_league_row_score(row, slot) for slot in slots), default=0)
        if score > 0:
            scored.append((score, row))
    if not scored:
        return football_resolver.FootballResolution(None, tuple(rows[:5]), ambiguous=bool(rows))
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score = scored[0][0]
    top_rows = [row for score, row in scored if score == top_score]
    if len(top_rows) > 1:
        return football_resolver.FootballResolution(None, tuple(top_rows[:5]), ambiguous=True)
    return football_resolver.FootballResolution(scored[0][1], tuple(row for _, row in scored[:5]), ambiguous=False)


def _league_row_score(row: dict[str, Any], slot: Any) -> int:
    league = row.get("league") if isinstance(row, dict) else {}
    if not isinstance(league, dict):
        return 0
    name_key = football_resolver.normalize_key(league.get("name"))
    slot_key = football_resolver.normalize_key(getattr(slot, "name", None))
    score = 0
    if name_key and name_key == slot_key:
        score += 100
    elif slot_key and slot_key in name_key:
        score += 60
    elif name_key:
        score += 1
    country_hint = football_resolver.normalize_key(getattr(slot, "country_hint", None))
    country = football_resolver.normalize_key(league.get("country") or row.get("country"))
    if country_hint and country == country_hint:
        score += 25
    return score


def _ambiguity_from_rows(entity_type: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_entity_summary(entity_type, row) for row in rows[:5] if _entity_summary(entity_type, row)]


def _entity_summary(entity_type: str, row: dict[str, Any]) -> dict[str, Any]:
    key = "player" if entity_type == "player" else "league" if entity_type == "league" else "team"
    entity = row.get(key) if isinstance(row, dict) else {}
    if not isinstance(entity, dict):
        return {}
    summary: dict[str, Any] = {"entity_type": entity_type}
    entity_id = entity.get("id")
    if isinstance(entity_id, int):
        summary["id"] = entity_id
    name = entity.get("name")
    if name:
        summary["display_name"] = str(name)
    for source_key, target_key in (("country", "country"), ("nationality", "nationality")):
        value = entity.get(source_key)
        if value:
            summary[target_key] = str(value)
    team = row.get("team") if entity_type == "player" and isinstance(row.get("team"), dict) else None
    league = row.get("league") if isinstance(row.get("league"), dict) else None
    if isinstance(team, dict) and team.get("name"):
        summary["team"] = str(team.get("name"))
    if isinstance(league, dict) and league.get("name"):
        summary["league"] = str(league.get("name"))
    return summary


def _today_iso(client: Any) -> str:
    method = getattr(client, "today_iso", None)
    if callable(method):
        try:
            return str(method())
        except Exception:
            logging.debug("API-Football timezone date helper failed; falling back to host date")
            return date.today().isoformat()
    logging.debug("API-Football client has no today_iso; falling back to host date")
    return date.today().isoformat()


def _filter_unpermitted_endpoints(result: FootballRetrievalResult, recipe: FootballRecipe) -> None:
    permitted = set(recipe.permitted_endpoints)
    unexpected = [endpoint for endpoint in result.endpoints if endpoint not in permitted]
    if unexpected:
        result.notes.append(f"unexpected_endpoint={','.join(unexpected[:3])}")


def _coverage_from_league_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        seasons = row.get("seasons") if isinstance(row, dict) else None
        if isinstance(seasons, list):
            for season in seasons:
                if isinstance(season, dict) and isinstance(season.get("coverage"), dict):
                    return season["coverage"]
        coverage = row.get("coverage") if isinstance(row, dict) else None
        if isinstance(coverage, dict):
            return coverage
    return {}


def _coverage_supports(coverage: dict[str, Any], capability: str) -> bool | None:
    fixtures = coverage.get("fixtures") if isinstance(coverage.get("fixtures"), dict) else {}
    mapping = {
        "events": fixtures.get("events") if isinstance(fixtures, dict) else None,
        "lineups": fixtures.get("lineups") if isinstance(fixtures, dict) else None,
        "statistics_fixtures": fixtures.get("statistics_fixtures") if isinstance(fixtures, dict) else None,
        "statistics_players": fixtures.get("statistics_players") if isinstance(fixtures, dict) else None,
        "standings": coverage.get("standings"),
        "players": coverage.get("players"),
        "top_scorers": coverage.get("top_scorers"),
        "top_assists": coverage.get("top_assists"),
        "top_cards": coverage.get("top_cards"),
        "injuries": coverage.get("injuries"),
        "predictions": coverage.get("predictions"),
        "odds": coverage.get("odds"),
    }
    value = mapping.get(capability)
    return value if isinstance(value, bool) else None


def _compact_half_statistics(halves: dict[str, dict[int | str, dict[str, Any]]]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for half, teams in halves.items():
        compact[half] = {
            str(team): [
                {
                    "original_label": stat.original_label,
                    "normalized_label": stat.normalized_label,
                    "value": stat.display_value,
                    "numeric_value": stat.numeric_value,
                    "category": stat.category,
                    "derived": stat.derived,
                }
                for stat in stats.values()
            ]
            for team, stats in teams.items()
        }
    return compact
