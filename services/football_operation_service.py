from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
import logging
import re
import secrets
from typing import Any, Awaitable, Callable
import unicodedata

from services import football_resolver
from services.football_api_request_compiler import (
    InvalidFootballApiRequest,
    build_coach_search_request,
    build_catalog_request,
    build_fixture_rounds_request,
    build_injury_request,
    build_league_teams_request,
    build_league_search_request,
    build_odds_request,
    build_odds_reference_request,
    build_player_teams_request,
    build_player_seasons_request,
    build_player_squads_request,
    build_player_stats_request,
    build_prediction_request,
    build_sidelined_request,
    build_team_seasons_request,
    build_team_statistics_request,
    build_trophy_request,
    build_transfer_request,
    build_venue_search_request,
    _make_country_slot,
    _make_league_slot,
    make_validated_date,
)
from services.football_live_match_service import FootballLiveMatchService, MatchData, build_match_data, compact_match_data, filter_fixtures, fixture_team_ids, normalize_match_statistics
from services.football_query_service import CANONICAL_FOOTBALL_TIME_SCOPES, FootballQueryOperation
from services.api_football import FootballApiError


class FootballOutcome(StrEnum):
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    NO_DATA_FOR_SCOPE = "NO_DATA_FOR_SCOPE"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    UNSUPPORTED_BY_COVERAGE = "UNSUPPORTED_BY_COVERAGE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


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
    fallback_evidence: dict[str, Any] | None = None
    repair_observations: list[dict[str, Any]] = field(default_factory=list)
    repair_scope_observations: list[dict[str, Any]] = field(default_factory=list)
    repair_failed_slots: list[dict[str, Any]] = field(default_factory=list)
    repair_feedback: dict[str, Any] = field(default_factory=dict)


FootballPlayerCanonicalizer = Callable[[football_resolver.PlayerQuery], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class FootballIdentityScopeCandidate:
    league_name: str | None = None
    country_name: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("league_name", "country_name"):
            value = getattr(self, field_name)
            if value is None:
                continue
            cleaned = " ".join(str(value).split())
            if not cleaned or len(cleaned) > 120 or any(ord(ch) < 32 for ch in cleaned):
                raise ValueError("invalid football identity scope candidate")
            if re.search(r"\b(?:id|team_id|league_id|country_id)\s*[:=]?\s*\d+\b", cleaned, re.IGNORECASE):
                raise ValueError("football identity scope candidate cannot contain IDs")
            object.__setattr__(self, field_name, cleaned)
        if self.league_name is None and self.country_name is None:
            raise ValueError("football identity scope candidate is empty")


@dataclass(frozen=True)
class FootballIdentityReplan:
    slot_kind: str
    slot_index: int
    candidate_names: tuple[str, ...]
    scope_candidates: tuple[FootballIdentityScopeCandidate, ...] = ()

    def __post_init__(self) -> None:
        if self.slot_kind not in {"team", "league"}:
            raise ValueError("unsupported football identity replan slot")
        if self.slot_index < 0 or not self.candidate_names or len(self.candidate_names) > 3:
            raise ValueError("invalid football identity replan")
        if len(self.scope_candidates) > 3:
            raise ValueError("invalid football identity replan scopes")
        if any(not isinstance(item, FootballIdentityScopeCandidate) for item in self.scope_candidates):
            raise ValueError("invalid football identity replan scope type")


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

    def __post_init__(self) -> None:
        if FootballOutcome.PROVIDER_FAILURE not in self.failure_outcomes:
            object.__setattr__(
                self,
                "failure_outcomes",
                (*self.failure_outcomes, FootballOutcome.PROVIDER_FAILURE),
            )


@dataclass(frozen=True)
class FootballRequestPlan:
    canonical_operation: str
    requested_operation: str
    requested_data_focus: str | None
    temporal_semantics: str | None
    required_slots: tuple[str, ...]
    forbidden_slots: tuple[str, ...]
    permitted_endpoints: tuple[str, ...]
    output_payload: tuple[str, ...]
    fallback_required_slots: tuple[str, ...]
    entity_evidence: dict[str, list[dict[str, Any]]]
    capability_evidence: dict[str, Any]


@dataclass(frozen=True)
class FootballTeamLeagueResolution:
    team_row: dict[str, Any] | None = None
    league_row: dict[str, Any] | None = None
    league_id: int | None = None
    season: int | None = None
    outcome: FootballOutcome = FootballOutcome.NOT_FOUND
    candidates: tuple[dict[str, Any], ...] = ()
    observed_candidates: tuple[dict[str, Any], ...] = ()
    observed_scopes: tuple[dict[str, int], ...] = ()
    failed_slot_indices: tuple[int, ...] = ()
    note: str = "current_league_not_found"


def football_web_fallback_eligible(result: FootballRetrievalResult) -> bool:
    """Decide fallback from typed provider state only; request text is irrelevant."""

    if result.outcome not in {
        FootballOutcome.NOT_FOUND,
        FootballOutcome.NO_DATA_FOR_SCOPE,
        FootballOutcome.UNSUPPORTED_BY_COVERAGE,
        FootballOutcome.PROVIDER_FAILURE,
    }:
        return False
    if result.missing_inputs or result.ambiguity_candidates:
        return False
    plan = result.requested_scope.get("request_plan")
    if not isinstance(plan, dict):
        return False
    required_slots = plan.get("fallback_required_slots", plan.get("required_slots", []))
    if not isinstance(required_slots, (list, tuple)):
        return False
    return all(
        isinstance(result.resolved_entities.get(slot), dict)
        and isinstance(result.resolved_entities[slot].get("id"), int)
        for slot in required_slots
        if isinstance(slot, str)
    )


class FootballOperationService:
    RECIPES: dict[str, FootballRecipe] = {
        "player": FootballRecipe(
            key="player",
            aliases=("player_profile", "player_recent_stats", "player_current_team", "player_previous_team", "player_career_history", "player_teams", "player_transfers", "player_injuries", "player_trophies", "player_sidelined"),
            required_inputs=("player",),
            permitted_endpoints=("/leagues", "/teams", "/players/profiles", "/players/seasons", "/players", "/players/squads", "/players/teams", "/transfers", "/injuries", "/trophies", "/sidelined"),
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
            permitted_endpoints=("/fixtures", "/fixtures?id", "/fixtures/events", "/fixtures/statistics", "/fixtures/lineups"),
            output_payload=("fixtures", "events", "statistics", "lineups", "match_data"),
            failure_outcomes=(FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED),
            handler_name="_execute_fixture_match",
            required_slots=("fixture",),
            allowed_optional_slots=(),
            forbidden_slots=("team", "opponent", "league", "country", "player"),
        ),
        "team": FootballRecipe(
            key="team",
            aliases=("team_profile", "team_squad", "team_seasons", "team_injuries", "team_transfers", "injuries", "transfers"),
            required_inputs=("team",),
            permitted_endpoints=("/teams", "/standings", "/fixtures", "/players/squads", "/teams/seasons", "/injuries", "/transfers"),
            output_payload=("team_context_row", "standing_row", "fixtures", "generic_rows", "reference_rows"),
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
            permitted_endpoints=("/teams", "/fixtures", "/fixtures?live=all", "/fixtures?date", "/fixtures?last", "/fixtures?next", "/fixtures/events", "/fixtures/statistics", "/fixtures/statistics?half=1", "/fixtures/lineups", "/fixtures/players", "/predictions", "/odds", "/odds/live"),
            output_payload=("match_data", "match_center", "events", "statistics", "lineups", "fixture_players", "predictions", "odds"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED, FootballOutcome.UNSUPPORTED_BY_COVERAGE),
            handler_name="_execute_fixture_optional",
            allowed_optional_slots=("team", "opponent", "league", "country", "fixture"),
            forbidden_slots=("player",),
        ),
        "odds": FootballRecipe(
            key="odds",
            aliases=("fixture_odds_pre_match", "fixture_odds_live", "odds_mapping"),
            required_inputs=("odds_scope",),
            permitted_endpoints=("/leagues", "/teams", "/fixtures", "/fixtures?live=all", "/fixtures?date", "/fixtures?next", "/odds", "/odds/live", "/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/odds/mapping"),
            output_payload=("odds", "reference_rows", "generic_rows", "team_context_row", "league_context_row", "match_data"),
            failure_outcomes=(FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND, FootballOutcome.NO_DATA_FOR_SCOPE, FootballOutcome.RESOLUTION_FAILED, FootballOutcome.UNSUPPORTED_BY_COVERAGE),
            handler_name="_execute_odds",
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
            permitted_endpoints=("/timezone", "/countries", "/leagues/seasons", "/teams/countries", "/odds/bookmakers", "/odds/bets", "/odds/live/bets"),
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
        player_selector: football_resolver.FootballPlayerSelector | None = None,
        identity_selector: football_resolver.FootballIdentitySelector | None = None,
    ) -> None:
        self.client = client
        self.player_canonicalizer = player_canonicalizer
        self.player_alias_cache = player_alias_cache
        self.player_selector = player_selector
        self.identity_selector = identity_selector
        self._active_result: FootballRetrievalResult | None = None
        self._identity_replans: tuple[FootballIdentityReplan, ...] = ()

    @staticmethod
    def _retain_repair_resolution(result: FootballRetrievalResult, relation: FootballTeamLeagueResolution) -> None:
        result.repair_observations.extend(relation.observed_candidates)
        result.repair_scope_observations.extend(relation.observed_scopes)
        result.repair_failed_slots.extend(
            {"kind": "team", "index": index}
            for index in relation.failed_slot_indices
        )

    @classmethod
    def canonical_operation_key(cls, operation: FootballQueryOperation) -> str | None:
        focus = str(operation.data_focus or "").casefold()
        op_type = str(operation.operation_type or "").casefold()
        if op_type == "fixture_result":
            if operation.fixture_focus:
                return "fixture_result_by_id"
            if operation.team_slots:
                return "team_fixture_result"
            if operation.league_slots:
                return "league_fixture_results"
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
        if focus in {"prediction", "predictions", "shootout", "match_center", "half_statistics", "fixture_players"}:
            return "fixture_optional"
        if focus in {"odds", "pre_match_odds", "live_odds", "odds_mapping", "mapping"}:
            return "odds"
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
        recipe = cls.RECIPES.get(key or "")
        if recipe is not None and operation.operation_type == "standings" and operation.team_slots:
            return replace(
                recipe,
                required_inputs=("team",),
                required_slots=("team",),
                permitted_endpoints=tuple(dict.fromkeys((*recipe.permitted_endpoints, "/teams"))),
                output_payload=("league_context_row", "standing_row", "standings_table", "generic_rows"),
                allowed_optional_slots=tuple(dict.fromkeys((*recipe.allowed_optional_slots, "team"))),
                forbidden_slots=tuple(slot for slot in recipe.forbidden_slots if slot != "team"),
            )
        return recipe

    async def resolve_current_domestic_league(
        self, team_row: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, FootballOutcome]:
        team = team_row.get("team") or {}
        country = football_resolver.normalize_key(team.get("country"))
        if not country or team.get("national") is True:
            return None, FootballOutcome.RESOLUTION_FAILED
        rows = await self.client.search_leagues(build_league_search_request(team_id=team["id"], current=True))
        domestic = [row for row in _unique_current_league_rows(rows)
                    if football_resolver.normalize_key((row.get("country") or {}).get("name")) == country
                    and _season_from_league_row(row) is not None]
        if len(domestic) != 1:
            return None, FootballOutcome.AMBIGUOUS if domestic else FootballOutcome.NOT_FOUND
        return domestic[0], FootballOutcome.SELECTED

    async def resolve_team_rows(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        max_teams: int = 2,
        allow_global: bool = True,
    ) -> tuple[list[dict[str, Any]], FootballOutcome]:
        if self.identity_selector is not None:
            selected: list[dict[str, Any]] = []
            for slot_index in range(min(len(operation.team_slots), max_teams)):
                rows, outcome = await self.resolve_team_slot_rows(
                    operation, slot_index=slot_index, league_id=league_id,
                    season=season, allow_global=allow_global,
                )
                if outcome != FootballOutcome.SELECTED:
                    return rows, outcome
                if any(_team_row_id(row) == _team_row_id(rows[0]) for row in selected):
                    return [], FootballOutcome.RESOLUTION_FAILED
                selected.extend(rows)
            return selected, FootballOutcome.SELECTED if selected else FootballOutcome.NOT_FOUND
        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        saw_ambiguous = False
        saw_candidate = False
        for use_search_fallback in (False, True):
            for slot_index, slot in enumerate(operation.team_slots):
                slot_variants = _identity_slot_variants(
                    operation.team_slots,
                    slot_index,
                    self._identity_replans,
                    "team",
                )
                for variant in slot_variants:
                    lookup_slot_operation = replace(
                        operation,
                        team_slots=tuple(
                            variant if index == slot_index else other
                            for index, other in enumerate(operation.team_slots)
                        ),
                    )
                    saw_candidate = True
                    try:
                        lookup = await football_resolver.resolve_team_candidate(
                            self.client,
                            variant,
                            league_id=league_id,
                            season=season,
                            allow_global=allow_global,
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
                    selected, outcome = _select_team_rows(
                        rows,
                        lookup_slot_operation.team_slots,
                        max_teams=max_teams,
                    )
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

    async def resolve_team_slot_rows(
        self,
        operation: FootballQueryOperation,
        *,
        slot_index: int,
        league_id: int | None,
        season: int | None,
        allow_global: bool = True,
    ) -> tuple[list[dict[str, Any]], FootballOutcome]:
        if slot_index < 0 or slot_index >= len(operation.team_slots):
            return [], FootballOutcome.RESOLUTION_FAILED
        slot = operation.team_slots[slot_index]
        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        saw_ambiguous = False
        for use_search_fallback in ((True,) if self.identity_selector is not None else (False, True)):
            for variant in _identity_slot_variants(
                operation.team_slots,
                slot_index,
                self._identity_replans,
                "team",
            ):
                lookup = await football_resolver.resolve_team_candidate(
                    self.client,
                    variant,
                    league_id=league_id,
                    season=season,
                    allow_global=allow_global,
                    use_search_fallback=use_search_fallback,
                )
                if self.identity_selector is not None:
                    picked = await football_resolver.select_football_identity(
                        list(lookup.rows), variant, entity_type="team", selector=self.identity_selector,
                    )
                    if picked.selected is not None:
                        return [picked.selected], FootballOutcome.SELECTED
                    saw_ambiguous = saw_ambiguous or picked.ambiguous
                for row in lookup.rows:
                    team = row.get("team") if isinstance(row, dict) else {}
                    team_id = team.get("id") if isinstance(team, dict) else None
                    if isinstance(team_id, int) and team_id in seen_ids:
                        continue
                    if isinstance(team_id, int):
                        seen_ids.add(team_id)
                    rows.append(row)
                saw_ambiguous = saw_ambiguous or lookup.resolution.ambiguous
        if self.identity_selector is not None:
            return rows[:5], FootballOutcome.AMBIGUOUS if saw_ambiguous else FootballOutcome.NOT_FOUND
        selected, outcome = _select_team_rows(rows, (slot,), max_teams=1)
        if selected:
            return selected, outcome
        if rows:
            return rows[:5], FootballOutcome.AMBIGUOUS if saw_ambiguous or len(rows) > 1 else FootballOutcome.NOT_FOUND
        return rows, FootballOutcome.AMBIGUOUS if saw_ambiguous else FootballOutcome.NOT_FOUND

    async def resolve_team_with_explicit_league(
        self,
        operation: FootballQueryOperation,
        *,
        league_rows: tuple[dict[str, Any], ...] = (),
        season: int | None = None,
    ) -> FootballTeamLeagueResolution:
        candidate_rows = list(league_rows)
        if not candidate_rows:
            for slot_index, slot in enumerate(operation.league_slots):
                if getattr(slot, "authority", None) == "PLANNER_INFERENCE":
                    continue
                for variant in _identity_slot_variants(
                    operation.league_slots,
                    slot_index,
                    self._identity_replans,
                    "league",
                ):
                    lookup = await football_resolver.resolve_league_candidate(
                        self.client,
                        variant,
                        country=operation.country_slots[0] if operation.country_slots else None,
                        season=season,
                        selector=self.identity_selector,
                    )
                    candidate_rows.extend(lookup.matches or ((lookup.row,) if lookup.row is not None else ()))

        ranked_pairs: dict[tuple[int, int], tuple[int, dict[str, Any], dict[str, Any], int]] = {}
        rejected_roster_rows: list[dict[str, Any]] = []
        observed_scopes: list[dict[str, int]] = []
        for league_row in _unique_league_rows(candidate_rows)[:5]:
            league = league_row.get("league") if isinstance(league_row, dict) else {}
            league_id = league.get("id") if isinstance(league, dict) else None
            if type(league_id) is not int:
                continue
            resolved_season = season or _season_from_league_row(league_row)
            if resolved_season is None:
                resolved_season = await self.client.get_current_season(league_id)
            if type(league_id) is int and type(resolved_season) is int:
                observed_scopes.append({"league_id": league_id, "season": resolved_season})
            roster_rows = await self.client.search_teams(
                build_league_teams_request(league_id=league_id, season=resolved_season)
            )
            logging.info("AI football league roster league_id=%s season=%s candidate_count=%s", league_id, resolved_season, len(roster_rows))
            if self.identity_selector is not None and len(operation.team_slots) == 1:
                picked = await football_resolver.select_football_identity(
                    roster_rows, operation.team_slots[0], entity_type="team", selector=self.identity_selector,
                )
                if picked.selected is not None:
                    ranked_pairs[(_team_row_id(picked.selected), league_id)] = (1, picked.selected, league_row, resolved_season)
                else:
                    rejected_roster_rows.extend(
                        {"row": row, "league_id": league_id, "season": resolved_season}
                        for row in picked.matches
                    )
                continue
            for roster_index, team_row in enumerate(roster_rows):
                team = team_row.get("team") if isinstance(team_row, dict) else {}
                venue = team_row.get("venue") if isinstance(team_row, dict) else {}
                if not isinstance(team, dict):
                    team = {}
                if not isinstance(venue, dict):
                    venue = {}
                team_id = _team_row_id(team_row)
                slot_scores = [
                    (str(getattr(slot, "name", ""))[:120], _team_roster_identity_score(team_row, slot))
                    for slot in operation.team_slots
                ]
                score = max((item[1] for item in slot_scores), default=0)
                accepted = type(team_id) is int and score > 0
                logging.debug(
                    "AI football team identity candidate evaluated lookup_mode=league_roster league_id=%s season=%s "
                    "row_index=%s team_id=%r team_name=%r team_code=%r team_country=%r team_national=%r "
                    "venue_city=%r candidate_scores=%s accepted=%s rejection=%s",
                    league_id,
                    resolved_season,
                    roster_index,
                    team_id,
                    str(team.get("name") or "")[:120],
                    team.get("code"),
                    team.get("country"),
                    team.get("national"),
                    str(venue.get("city") or "")[:120],
                    slot_scores,
                    accepted,
                    "none" if accepted else ("non_integer_id" if type(team_id) is not int else "identity_score_zero"),
                )
                if not accepted:
                    rejected_roster_rows.append({
                        "row": team_row,
                        "league_id": league_id,
                        "season": resolved_season,
                    })
                    continue
                pair_key = (team_id, league_id)
                prior = ranked_pairs.get(pair_key)
                if prior is None or score > prior[0]:
                    ranked_pairs[pair_key] = (score, team_row, league_row, resolved_season)

        if not ranked_pairs:
            return FootballTeamLeagueResolution(
                outcome=FootballOutcome.NOT_FOUND,
                observed_candidates=tuple(rejected_roster_rows[:5]),
                observed_scopes=tuple(observed_scopes[:5]),
                failed_slot_indices=tuple(range(len(operation.team_slots))),
                note="team_not_found",
            )
        if len(ranked_pairs) != 1:
            ambiguous_rows = list(ranked_pairs.values())[:5]
            return FootballTeamLeagueResolution(
                outcome=FootballOutcome.AMBIGUOUS,
                candidates=tuple(item[1] for item in ambiguous_rows),
                observed_candidates=tuple(
                    {"row": team_row, "league_id": league.get("id"), "season": resolved_season}
                    for _score, team_row, league_row, resolved_season in ambiguous_rows
                    for league in (league_row.get("league") if isinstance(league_row, dict) else {},)
                    if isinstance(league, dict)
                ),
                observed_scopes=tuple(observed_scopes),
                note="team_ambiguous",
            )
        _score, team_row, league_row, resolved_season = next(iter(ranked_pairs.values()))
        league = league_row.get("league") if isinstance(league_row, dict) else {}
        league_id = league.get("id") if isinstance(league, dict) else None
        if type(league_id) is not int:
            return FootballTeamLeagueResolution(
                outcome=FootballOutcome.RESOLUTION_FAILED,
                candidates=(team_row,),
                note="league_id_missing",
            )
        return FootballTeamLeagueResolution(
            team_row=team_row,
            league_row=league_row,
            league_id=league_id,
            season=resolved_season,
            outcome=FootballOutcome.SELECTED,
            candidates=(team_row,),
            note="",
        )

    async def resolve_team_with_replanned_scope(
        self,
        operation: FootballQueryOperation,
        *,
        season: int | None = None,
    ) -> FootballTeamLeagueResolution | None:
        replan = next((item for item in self._identity_replans if item.slot_kind == "team" and item.slot_index == 0), None)
        if (
            replan is None
            or replan.slot_kind != "team"
            or replan.slot_index != 0
            or len(operation.team_slots) != 1
            or _authoritative_league_slots(operation.league_slots)
            or not replan.scope_candidates
        ):
            return None

        pairs: dict[tuple[int, int], tuple[dict[str, Any], dict[str, Any], int]] = {}
        attempted_scopes: list[dict[str, int]] = []
        team_slot = operation.team_slots[0]
        unique_team_variants: list[Any] = []
        seen_variant_names: set[str] = set()
        for variant in (team_slot, *_identity_slot_variants(operation.team_slots, 0, replan, "team")):
            variant_name = football_resolver.normalize_key(getattr(variant, "name", None))
            if variant_name and variant_name not in seen_variant_names:
                seen_variant_names.add(variant_name)
                unique_team_variants.append(variant)
        for scope in replan.scope_candidates:
            if not scope.league_name:
                logging.info(
                    "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                    "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                    "decision=rejected rejection=league_name_missing",
                    scope.league_name,
                    scope.country_name,
                    None,
                    season,
                    0,
                )
                continue
            try:
                scope_league = _make_league_slot(
                    scope.league_name,
                    source="planner",
                    authority="PLANNER_INFERENCE",
                    country_hint=scope.country_name,
                )
                scope_country = (
                    _make_country_slot(
                        scope.country_name,
                        source="planner",
                        authority="PLANNER_INFERENCE",
                    )
                    if scope.country_name
                    else None
                )
                lookup = await football_resolver.resolve_league_candidate(
                    self.client,
                    scope_league,
                    country=scope_country,
                    season=season,
                    selector=self.identity_selector,
                )
            except (AttributeError, TypeError, FootballApiError, InvalidFootballApiRequest) as exc:
                logging.info(
                    "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                    "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                    "decision=rejected rejection=league_resolution_error error=%s",
                    scope.league_name,
                    scope.country_name,
                    None,
                    season,
                    0,
                    str(exc)[:120],
                )
                continue
            league_rows = list(lookup.matches or ((lookup.row,) if lookup.row is not None else ()))
            unique_leagues = _unique_league_rows(league_rows)
            if lookup.ambiguous or len(unique_leagues) != 1:
                logging.info(
                    "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                    "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                    "decision=rejected rejection=league_ambiguous",
                    scope.league_name,
                    scope.country_name,
                    None,
                    season,
                    0,
                )
                continue
            league_row = unique_leagues[0]
            league = league_row.get("league") if isinstance(league_row, dict) else {}
            league_id = league.get("id") if isinstance(league, dict) else None
            if type(league_id) is not int:
                continue
            resolved_season = season or lookup.season or _season_from_league_row(league_row)
            if resolved_season is None:
                try:
                    resolved_season = await self.client.get_current_season(league_id)
                except (AttributeError, TypeError, FootballApiError, InvalidFootballApiRequest) as exc:
                    logging.info(
                        "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                        "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                        "decision=rejected rejection=season_resolution_error error=%s",
                        scope.league_name,
                        scope.country_name,
                        league_id,
                        None,
                        0,
                        str(exc)[:120],
                    )
                    continue
            if type(resolved_season) is not int:
                logging.info(
                    "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                    "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                    "decision=rejected rejection=season_missing",
                    scope.league_name,
                    scope.country_name,
                    league_id,
                    resolved_season,
                    0,
                )
                continue
            try:
                attempted_scopes.append({"league_id": league_id, "season": resolved_season})
                roster_rows = await self.client.search_teams(
                    build_league_teams_request(league_id=league_id, season=resolved_season)
                )
            except (AttributeError, TypeError, FootballApiError, InvalidFootballApiRequest) as exc:
                logging.info(
                    "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                    "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                    "decision=rejected rejection=roster_resolution_error error=%s",
                    scope.league_name,
                    scope.country_name,
                    league_id,
                    resolved_season,
                    0,
                    str(exc)[:120],
                )
                continue
            matches: dict[int, tuple[int, dict[str, Any]]] = {}
            if self.identity_selector is not None:
                picked = await football_resolver.select_football_identity(
                    roster_rows, team_slot, entity_type="team", selector=self.identity_selector,
                )
                roster_rows = [picked.selected] if picked.selected is not None else []
            for roster_row in roster_rows:
                score = 1 if self.identity_selector is not None else max(
                    (_team_roster_identity_score(roster_row, variant) for variant in unique_team_variants),
                    default=0,
                )
                roster_team_id = _team_row_id(roster_row)
                if type(roster_team_id) is int and score > 0:
                    prior = matches.get(roster_team_id)
                    if prior is None or score > prior[0]:
                        matches[roster_team_id] = (score, roster_row)
            logging.info(
                "AI football identity scope candidate evaluated slot_kind=team slot_index=0 "
                "league_name=%r country_name=%r league_id=%r season=%r roster_match_count=%s "
                "decision=%s rejection=%s",
                scope.league_name,
                scope.country_name,
                league_id,
                resolved_season,
                len(matches),
                "accepted" if len(matches) == 1 else "rejected",
                "none" if len(matches) == 1 else ("team_ambiguous" if len(matches) > 1 else "team_not_found"),
            )
            for team_id, (score, roster_row) in matches.items():
                pairs[(team_id, league_id)] = (roster_row, league_row, resolved_season)
        if len(pairs) != 1:
            ambiguous_rows = list(pairs.values())[:5]
            return FootballTeamLeagueResolution(
                outcome=FootballOutcome.AMBIGUOUS if len(pairs) > 1 else FootballOutcome.NOT_FOUND,
                candidates=tuple(item[0] for item in ambiguous_rows),
                observed_candidates=tuple(
                    {"row": team_row, "league_id": league.get("id"), "season": resolved_season}
                    for team_row, league_row, resolved_season in ambiguous_rows
                    for league in (league_row.get("league") if isinstance(league_row, dict) else {},)
                    if isinstance(league, dict)
                ),
                observed_scopes=tuple(attempted_scopes),
                note="team_ambiguous" if len(pairs) > 1 else "team_not_found",
            )
        team_row, league_row, resolved_season = next(iter(pairs.values()))
        league = league_row.get("league") if isinstance(league_row, dict) else {}
        league_id = league.get("id") if isinstance(league, dict) else None
        if not isinstance(league_id, int):
            return FootballTeamLeagueResolution(outcome=FootballOutcome.RESOLUTION_FAILED, note="league_id_missing")
        return FootballTeamLeagueResolution(
            team_row=team_row,
            league_row=league_row,
            league_id=league_id,
            season=resolved_season,
            outcome=FootballOutcome.SELECTED,
            candidates=(team_row,),
            note="",
        )

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
        identity_replan: FootballIdentityReplan | tuple[FootballIdentityReplan, ...] | None = None,
    ) -> FootballRetrievalResult:
        self._identity_replans = (
            (identity_replan,) if isinstance(identity_replan, FootballIdentityReplan)
            else tuple(identity_replan or ())
        )
        recipe = self.recipe_for(operation)
        if recipe is None:
            return FootballRetrievalResult(
                outcome=FootballOutcome.UNSUPPORTED,
                notes=["unsupported_recipe"],
                requested_scope=_requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus),
            )
        temporal_error = _validate_router_temporal_fields(operation)
        if temporal_error is not None:
            return FootballRetrievalResult(
                outcome=FootballOutcome.RESOLUTION_FAILED,
                notes=[temporal_error],
                missing_inputs=["temporal_scope"],
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
        self._active_result = None
        try:
            result = await handler(operation, league_id=league_id, season=season, data_focus=data_focus)
        except FootballApiError as exc:
            result = self._active_result or FootballRetrievalResult()
            result.outcome = FootballOutcome.PROVIDER_FAILURE
            result.notes.extend(
                [
                    f"provider_error_status={getattr(exc, 'status', None)}",
                    f"provider_retryable={getattr(exc, 'retryable', False)}",
                ]
            )
        for key, value in _requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus).items():
            result.requested_scope.setdefault(key, value)
        result.requested_scope.setdefault("request_plan", _request_plan_payload(request_plan))
        _filter_unpermitted_endpoints(result, recipe)
        result.repair_feedback = _repair_feedback(operation, result)
        self._active_result = None
        self._identity_replans = ()
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
        self._active_result = result
        league_row = None
        team_row = None
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                if league_outcome == FootballOutcome.AMBIGUOUS and operation.team_slots and self.identity_selector is None:
                    relation = await self.resolve_team_with_explicit_league(operation)
                    result.endpoints.extend(("/teams", "/leagues"))
                    if relation.outcome == FootballOutcome.SELECTED:
                        team_row, league_row, league_id, season = relation.team_row, relation.league_row, relation.league_id, relation.season
                        result.league_context_row = league_row
                    else:
                        self._retain_repair_resolution(result, relation)
                        result.outcome = relation.outcome
                        result.notes.append(relation.note)
                        result.generic_rows = list(relation.candidates)
                        result.missing_inputs.append("league")
                        if relation.outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                        return result
                else:
                    result.outcome = league_outcome
                    result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                    if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                        result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                    else:
                        result.missing_inputs.append("league")
                    return result
        team_id = None
        if operation.team_slots:
            if team_row is None:
                team_rows, team_outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=1)
                team_row = team_rows[0] if team_outcome == FootballOutcome.SELECTED and team_rows else None
                result.endpoints.append("/teams")
                if team_outcome != FootballOutcome.SELECTED or team_row is None:
                    result.outcome = team_outcome
                    result.notes.append("team_ambiguous" if team_outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                    result.generic_rows = team_rows
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
        fixture_count = operation.fixture_count or (1 if focus_key.startswith("single_") else (5 if focus_key.startswith("schedule_") or data_focus == "season_start" else 3))
        calendar_date = operation.date_hint if operation.date_hint and re.fullmatch(r"\d{4}-\d{2}-\d{2}", operation.date_hint) else None
        if calendar_date is not None or operation.fixture_round is not None:
            params = {"league_id": league_id, "season": season, "team_id": team_id,
                      "date_iso": make_validated_date(calendar_date).value if calendar_date else None}
            if operation.fixture_round is not None:
                params["round_name"] = operation.fixture_round
            result.fixtures = await self.client.get_fixtures_on_date(**params)
            result.fixtures = filter_fixtures(result.fixtures, team_id=team_id, date_iso=calendar_date)
            result.fixtures = [item for item in result.fixtures
                               if (league_id is None or (item.get("league") or {}).get("id", league_id) == league_id)
                               and (season is None or (item.get("league") or {}).get("season", season) == season)
                               and (operation.fixture_round is None or (item.get("league") or {}).get("round") == operation.fixture_round)]
        elif operation.operation_type == "fixture_last":
            result.fixtures = await self.client.get_last_fixtures(league_id=league_id, season=season, last_count=fixture_count, team_id=team_id)
        else:
            result.fixtures = await self.client.get_next_fixtures(
                league_id=league_id,
                season=season,
                next_count=fixture_count,
                team_id=team_id,
            )
        if operation.operation_type == "fixture_next" and str(data_focus or "").casefold() in {"next_match", "single_next_match"}:
            result.fixtures = _nearest_fixture(result.fixtures)
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
        self._active_result = result
        rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=2,
                                                     allow_global=league_id is None)
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
        result.resolved_entities = {key: _entity_summary("team", row) for key, row in zip(("team", "opponent"), rows)}
        result.generic_rows = await self.client.get_head_to_head(team_a_id=team_ids[0], team_b_id=team_ids[1],
                                                                last=operation.fixture_count or 10)
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
        self._active_result = result
        team_row = None
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                if league_outcome == FootballOutcome.AMBIGUOUS and operation.team_slots and self.identity_selector is None:
                    relation = await self.resolve_team_with_explicit_league(operation)
                    result.endpoints.extend(("/teams", "/leagues"))
                    if relation.outcome == FootballOutcome.SELECTED:
                        team_row, league_row, league_id, season = relation.team_row, relation.league_row, relation.league_id, relation.season
                        result.league_context_row = league_row
                    else:
                        self._retain_repair_resolution(result, relation)
                        result.outcome = relation.outcome
                        result.notes.append(relation.note)
                        result.generic_rows = list(relation.candidates)
                        result.missing_inputs.append("league")
                        if relation.outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                        return result
                else:
                    result.outcome = league_outcome
                    result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                    if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                        result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                    else:
                        result.missing_inputs.append("league")
                    return result
        if team_row is None:
            scoped_relation = await self.resolve_team_with_replanned_scope(operation, season=season)
            if scoped_relation is not None:
                result.endpoints.extend(("/leagues", "/teams"))
                if scoped_relation.outcome != FootballOutcome.SELECTED or scoped_relation.team_row is None:
                    self._retain_repair_resolution(result, scoped_relation)
                    result.outcome = scoped_relation.outcome
                    result.notes.append(scoped_relation.note)
                    result.generic_rows = list(scoped_relation.candidates)
                    if scoped_relation.outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                    else:
                        result.missing_inputs.append("team")
                    return result
                team_row = scoped_relation.team_row
                league_id = scoped_relation.league_id
                season = scoped_relation.season
                result.league_context_row = scoped_relation.league_row
            else:
                team_rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=1)
                team_row = team_rows[0] if outcome == FootballOutcome.SELECTED and team_rows else None
                result.endpoints.append("/teams")
                if outcome != FootballOutcome.SELECTED or team_row is None:
                    result.outcome = outcome
                    result.notes.append("team_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                    result.generic_rows = team_rows
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
        result.resolved_entities["team"] = _entity_summary("team", team_row)
        if league_id is not None:
            result.resolved_entities["league"] = _entity_summary("league", {"league": {"id": league_id}})
        if operation.operation_type == "team_profile":
            if league_id is not None:
                if season is None:
                    season = await self.client.get_current_season(league_id)
                unsupported = await self.check_coverage(result, "standings", league_id=league_id, season=season)
                if not unsupported:
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
            if await self.check_coverage(result, "injuries", league_id=league_id, season=season):
                return result
            result.generic_rows = await self.client.get_injuries(build_injury_request(league_id=league_id, season=season, team_id=team_id))
            result.generic_label = "FOOTBALL_INJURIES"
            result.endpoints.append("/injuries")
        elif operation.operation_type in {"team_squad", "team_seasons"}:
            await self.load_profile_section(result, operation.operation_type)
        else:
            result.generic_rows = await self.client.get_transfers(build_transfer_request(team_id=team_id))
            result.generic_label = "FOOTBALL_TRANSFERS"
            result.endpoints.append("/transfers")
        if operation.operation_type in {"team_squad", "team_seasons", "team_injuries", "team_transfers", "injuries", "transfers"} and not result.generic_rows and result.outcome == FootballOutcome.SELECTED:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
        return result

    async def load_profile_section(self, result: FootballRetrievalResult, focus: str) -> FootballRetrievalResult:
        # Profile menus reuse a resolved identity; opening a section must not search its name again.
        team_sections = {"team_squad", "team_seasons", "team_coach", "team_venue"}
        player_sections = {"player_teams", "player_transfers", "player_trophies", "player_sidelined"}
        if focus not in team_sections | player_sections:
            raise ValueError("Unknown football profile section")
        kind = "team" if focus in team_sections else "player"
        row = result.team_context_row if kind == "team" else result.player_context_row
        entity_id = (row or {}).get(kind, {}).get("id")
        if type(entity_id) is not int or entity_id < 1:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append(f"{kind}_id_missing")
            return result
        result.resolved_entities[kind] = _entity_summary(kind, row)
        if focus == "team_squad":
            rows = await self.client.get_player_squads(build_player_squads_request(team_id=entity_id))
            result.generic_rows = [item for item in rows if (item.get("team") or {}).get("id") == entity_id]
            if rows and not result.generic_rows:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.generic_label = "FOOTBALL_TEAM_SQUAD"
            result.endpoints.append("/players/squads")
        elif focus == "team_seasons":
            result.reference_rows = await self.client.get_team_seasons(build_team_seasons_request(team_id=entity_id))
            result.generic_rows = result.reference_rows
            result.generic_label = "FOOTBALL_TEAM_SEASONS"
            result.endpoints.append("/teams/seasons")
        elif focus == "team_coach":
            rows = await self.client.search_coaches(build_coach_search_request(team_id=entity_id))
            result.generic_rows = [item for item in rows if (item.get("team") or {}).get("id") == entity_id or any(
                (career.get("team") or {}).get("id") == entity_id for career in item.get("career") or [])]
            if rows and not result.generic_rows:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.endpoints.append("/coachs")
        elif focus == "team_venue":
            venue = row.get("venue")
            result.venues = [venue] if isinstance(venue, dict) and venue else []
            result.generic_rows = result.venues
            result.generic_label = "FOOTBALL_VENUE"
        elif focus == "player_teams":
            result.generic_rows = await self.client.get_player_teams(build_player_teams_request(player_id=entity_id))
            result.generic_label = "FOOTBALL_PLAYER_TEAMS"
            result.endpoints.append("/players/teams")
        elif focus == "player_transfers":
            rows = await self.client.get_transfers(build_transfer_request(player_id=entity_id))
            result.generic_rows = [item for item in rows if (item.get("player") or {}).get("id") in {None, entity_id}]
            if rows and not result.generic_rows:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.generic_label = "FOOTBALL_PLAYER_TRANSFERS"
            result.endpoints.append("/transfers")
        elif focus == "player_trophies":
            result.trophies = await self.client.get_trophies(build_trophy_request(player_id=entity_id))
            result.generic_rows = result.trophies
            result.generic_label = "FOOTBALL_PLAYER_TROPHIES"
            result.endpoints.append("/trophies")
        elif focus == "player_sidelined":
            result.sidelined = await self.client.get_sidelined(build_sidelined_request(player_id=entity_id))
            result.generic_rows = result.sidelined
            result.generic_label = "FOOTBALL_PLAYER_SIDELINED"
            result.endpoints.append("/sidelined")
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
        authoritative_leagues = _authoritative_league_slots(operation.league_slots)
        if authoritative_leagues:
            league_tiers = (authoritative_leagues, tuple(
                slot for slot in operation.league_slots
                if getattr(slot, "authority", None) == "PLANNER_INFERENCE"
            ))
        else:
            league_tiers = (operation.league_slots,)
        if not any(league_tiers):
            return None, season, None, FootballOutcome.RESOLUTION_FAILED
        for tier_index, league_slots in enumerate(league_tiers):
            if not league_slots:
                continue
            for slot_index, slot in enumerate(league_slots):
                slot_variants = _identity_slot_variants(
                    league_slots,
                    slot_index,
                    self._identity_replans,
                    "league",
                )
                for variant in slot_variants:
                    league_key = football_resolver.normalize_league_key(variant.name) if football_resolver.slot_allows_static_alias(variant) else None
                    if league_key:
                        resolved_id, resolved_season = await self.resolve_league_key(league_key)
                        return resolved_id, _season_from_operation_hint(operation, season or resolved_season), {"league": {"name": football_resolver.league_label(league_key, "en")}}, FootballOutcome.SELECTED
            rows: list[dict[str, Any]] = []
            saw_ambiguous = False
            lookup_slots = tuple(
                variant
                for slot_index, slot in enumerate(league_slots)
                for variant in _identity_slot_variants(
                    league_slots,
                    slot_index,
                    self._identity_replans,
                    "league",
                )
            )
            for slot in lookup_slots:
                lookup = await football_resolver.resolve_league_candidate(
                    self.client,
                    slot,
                    country=operation.country_slots[0] if operation.country_slots else None,
                    season=season,
                    selector=self.identity_selector,
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
            picked = _select_league_row(rows, league_slots)
            if picked.ambiguous or saw_ambiguous:
                return None, season, picked.matches[0] if picked.matches else (rows[0] if rows else None), FootballOutcome.AMBIGUOUS
            league = picked.selected.get("league") if isinstance(picked.selected, dict) else {}
            selected_id = league.get("id") if isinstance(league, dict) else None
            if isinstance(selected_id, int):
                resolved_season = season or await self.client.get_current_season(selected_id)
                return selected_id, _season_from_operation_hint(operation, resolved_season), picked.selected, FootballOutcome.SELECTED
            logging.info(
                "AI football league candidate tier exhausted tier=%s candidates=%s response_rows=%s",
                tier_index,
                [str(getattr(slot, "name", ""))[:120] for slot in league_slots],
                len(rows),
            )
        return None, season, None, FootballOutcome.NOT_FOUND

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
        if operation.operation_type == "standings" and operation.team_slots:
            return await self._execute_team_standings(operation, league_id=league_id, season=season, data_focus=data_focus)
        result = FootballRetrievalResult()
        self._active_result = result
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
        result.resolved_entities["league"] = _entity_summary(
            "league",
            league_row or {"league": {"id": league_id}},
        )
        result.requested_scope = _requested_scope(
            operation,
            league_id=league_id,
            season=season,
            data_focus=data_focus,
        )
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
            if await self.check_coverage(result, "standings", league_id=league_id, season=season):
                return result
            standings_raw = await self.client.get_standings(league_id=league_id, season=season)
            result.standings_table = football_resolver.extract_table_rows(standings_raw)
            if standings_raw and isinstance(standings_raw[0].get("league"), dict):
                result.league_context_row = {"league": {
                    key: value for key, value in standings_raw[0]["league"].items() if key != "standings"
                }}
            result.endpoints.append("/standings")
            return result
        if focus in {"top_assists", "assists", "asistencias"}:
            capability, endpoint, label, fetch = "top_assists", "/players/topassists", "FOOTBALL_TOP_ASSISTS", self.client.get_top_assists
        elif focus in {"top_yellow_cards", "yellowcards", "yellow_cards", "yellow", "amarillas"}:
            capability, endpoint, label, fetch = "top_cards", "/players/topyellowcards", "FOOTBALL_TOP_YELLOW_CARDS", self.client.get_top_yellow_cards
        elif focus in {"top_red_cards", "redcards", "red_cards", "red", "rojas"}:
            capability, endpoint, label, fetch = "top_cards", "/players/topredcards", "FOOTBALL_TOP_RED_CARDS", self.client.get_top_red_cards
        else:
            capability, endpoint, label, fetch = "top_scorers", "/players/topscorers", "FOOTBALL_SCORERS", self.client.get_top_scorers
        if await self.check_coverage(result, capability, league_id=league_id, season=season):
            return result
        result.generic_rows = await fetch(league_id=league_id, season=season)
        result.generic_label = label
        result.endpoints.append(endpoint)
        return result

    async def _execute_team_standings(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        self._active_result = result
        authoritative_leagues = _authoritative_league_slots(operation.league_slots)
        resolution_path = "explicit_league_resolution" if authoritative_leagues else "team_current_league_resolution"
        resolution_reason = "authoritative_league_slots_present" if authoritative_leagues else "no_authoritative_league_slots"
        logging.info(
            "AI football standings resolution path selected operation=%s path=%s reason=%s "
            "league_candidates=%s team_candidates=%s league_slot_authorities=%s",
            operation.operation_type,
            resolution_path,
            resolution_reason,
            [str(getattr(slot, "name", ""))[:120] for slot in operation.league_slots[:4]],
            [str(getattr(slot, "name", ""))[:120] for slot in operation.team_slots[:4]],
            [getattr(slot, "authority", None) for slot in operation.league_slots],
        )
        league_row: dict[str, Any] | None = None
        team_row: dict[str, Any] | None = None

        if authoritative_leagues:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(
                operation,
                league_id=league_id,
                season=season,
            )
            result.endpoints.append("/leagues")
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                if league_outcome == FootballOutcome.AMBIGUOUS and operation.team_slots and self.identity_selector is None:
                    relation = await self.resolve_team_with_explicit_league(operation)
                    result.endpoints.append("/teams")
                    if relation.outcome != FootballOutcome.SELECTED or relation.team_row is None:
                        result.outcome = relation.outcome
                        result.notes.append(relation.note)
                        result.missing_inputs.append("team")
                        self._retain_repair_resolution(result, relation)
                        if relation.outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                        return result
                    team_row = relation.team_row
                    league_row = relation.league_row
                    league_id = relation.league_id
                    season = season or relation.season
                    result.league_context_row = league_row
                else:
                    result.outcome = league_outcome
                    result.notes.append("league_ambiguous" if league_outcome == FootballOutcome.AMBIGUOUS else "league_not_found")
                    result.missing_inputs.append("league")
                    if league_outcome == FootballOutcome.AMBIGUOUS and league_row is not None:
                        result.ambiguity_candidates = _ambiguity_from_rows("league", [league_row])
                    return result
        else:
            scoped_relation = await self.resolve_team_with_replanned_scope(operation, season=season)
            if scoped_relation is not None:
                result.endpoints.extend(("/leagues", "/teams"))
                if scoped_relation.outcome != FootballOutcome.SELECTED or scoped_relation.team_row is None:
                    self._retain_repair_resolution(result, scoped_relation)
                    result.outcome = scoped_relation.outcome
                    result.notes.append(scoped_relation.note)
                    result.generic_rows = list(scoped_relation.candidates)
                    if scoped_relation.outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", list(scoped_relation.candidates))
                    else:
                        result.missing_inputs.append("team")
                    return result
                team_row = scoped_relation.team_row
                league_row = scoped_relation.league_row
                league_id = scoped_relation.league_id
                season = scoped_relation.season
            else:
                team_rows, team_outcome = await self.resolve_team_rows(operation, league_id=None, season=None, max_teams=1)
                team_row = team_rows[0] if team_outcome == FootballOutcome.SELECTED and team_rows else None
                result.endpoints.append("/teams")
                if team_outcome != FootballOutcome.SELECTED or team_row is None:
                    result.outcome = team_outcome
                    result.notes.append("team_ambiguous" if team_outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                    result.missing_inputs.append("team")
                    if team_outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", team_rows)
                    return result
                team = team_row.get("team") if isinstance(team_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if not isinstance(team_id, int):
                    result.outcome = FootballOutcome.RESOLUTION_FAILED
                    result.notes.append("team_id_missing")
                    return result
                current_rows = await self.client.search_leagues(
                    build_league_search_request(team_id=team_id, current=True)
                )
                result.endpoints.append("/leagues")
                current_leagues = _unique_current_league_rows(current_rows)
                if len(current_leagues) > 1 and self.identity_selector is not None:
                    picked = await football_resolver.select_football_identity(
                        current_leagues, operation.team_slots[0], entity_type="league", selector=self.identity_selector,
                    )
                    if picked.selected is not None:
                        current_leagues = [picked.selected]
                if len(current_leagues) != 1:
                    result.outcome = FootballOutcome.AMBIGUOUS if len(current_leagues) > 1 else FootballOutcome.NOT_FOUND
                    result.notes.append("current_league_ambiguous" if len(current_leagues) > 1 else "current_league_not_found")
                    result.missing_inputs.append("league")
                    if len(current_leagues) > 1:
                        result.ambiguity_candidates = _ambiguity_from_rows("league", current_leagues)
                    return result
                league_row = current_leagues[0]
                league = league_row.get("league") if isinstance(league_row, dict) else {}
                league_id = league.get("id") if isinstance(league, dict) else None
                if not isinstance(league_id, int):
                    result.outcome = FootballOutcome.RESOLUTION_FAILED
                    result.notes.append("league_id_missing")
                    return result
                season = season or _season_from_league_row(league_row)
                if season is None:
                    season = await self.client.get_current_season(league_id)
        if season is None and league_id is not None:
            season = await self.client.get_current_season(league_id)
        if league_id is None or season is None:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("league_or_season_missing")
            result.missing_inputs.append("season" if league_id is not None else "league")
            return result

        if team_row is None:
            if authoritative_leagues:
                team_rows, team_outcome = await self.resolve_team_rows(
                    operation,
                    league_id=league_id,
                    season=season,
                    max_teams=1,
                    allow_global=False,
                )
            else:
                team_rows, team_outcome = await self.resolve_team_rows(
                    operation,
                    league_id=league_id,
                    season=season,
                    max_teams=1,
                )
            team_row = team_rows[0] if team_outcome == FootballOutcome.SELECTED and team_rows else None
            if "/teams" not in result.endpoints:
                result.endpoints.append("/teams")
            if team_outcome != FootballOutcome.SELECTED or team_row is None:
                if authoritative_leagues:
                    fallback_league_row = league_row or {"league": {"id": league_id, "type": "League"}}
                    relation = await self.resolve_team_with_explicit_league(
                        operation,
                        league_rows=(fallback_league_row,),
                        season=season,
                    )
                    if relation.outcome == FootballOutcome.SELECTED and relation.team_row is not None:
                        team_row = relation.team_row
                        league_row = relation.league_row
                        league_id = relation.league_id
                        season = relation.season
                    else:
                        result.outcome = relation.outcome
                        result.notes.append(relation.note)
                        result.missing_inputs.append("team")
                        self._retain_repair_resolution(result, relation)
                        if relation.outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                        return result
                else:
                    result.outcome = team_outcome
                    result.notes.append("team_ambiguous" if team_outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                    result.missing_inputs.append("team")
                    if team_outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", team_rows)
                    return result

        team = team_row.get("team") if isinstance(team_row, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, int):
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_id_missing")
            return result
        result.team_context_row = team_row
        result.league_context_row = league_row
        result.resolved_entities["team"] = _entity_summary("team", team_row)
        result.resolved_entities["league"] = _entity_summary("league", league_row or {"league": {"id": league_id}})
        result.requested_scope = _requested_scope(operation, league_id=league_id, season=season, data_focus=data_focus)

        standings_raw = await self.client.get_standings(league_id=league_id, season=season)
        result.standings_table = football_resolver.extract_table_rows(standings_raw)
        result.standing_row = football_resolver.find_team_row(result.standings_table, team_id)
        team_name = str(team.get("name") or "") if isinstance(team, dict) else ""
        team_tokens = _team_identity_tokens(team_name)
        name_candidates = []
        for standing in result.standings_table[:50]:
            standing_team = standing.get("team") if isinstance(standing, dict) else {}
            if not isinstance(standing_team, dict):
                continue
            standing_tokens = _team_identity_tokens(standing_team.get("name"))
            if standing_tokens and (
                standing_tokens == team_tokens
                or _team_identity_contains(standing_tokens, team_tokens)
                or _team_identity_contains(team_tokens, standing_tokens)
            ):
                name_candidates.append({
                    "id": standing_team.get("id"),
                    "name": str(standing_team.get("name") or "")[:120],
                })
        unique_name_candidates = {
            int(item["id"]): item
            for item in name_candidates
            if isinstance(item.get("id"), int)
        }
        if not authoritative_leagues and result.standing_row is None and len(unique_name_candidates) == 1:
            selected_standing_id = next(iter(unique_name_candidates))
            result.standing_row = football_resolver.find_team_row(
                result.standings_table,
                selected_standing_id,
            )
            if result.standing_row is not None:
                standing_team = result.standing_row.get("team")
                if isinstance(standing_team, dict):
                    result.team_context_row = {"team": dict(standing_team)}
                    result.resolved_entities["team"] = _entity_summary(
                        "team", result.team_context_row
                    )
        elif not authoritative_leagues and result.standing_row is None and len(unique_name_candidates) > 1:
            result.outcome = FootballOutcome.AMBIGUOUS
            result.notes.append("standing_team_ambiguous")
            result.ambiguity_candidates = [
                {"entity_type": "team", **candidate}
                for candidate in list(unique_name_candidates.values())[:5]
            ]
        logging.info(
            "AI football standings identity join evaluated league_id=%s season=%s resolved_team_id=%r "
            "resolved_team_name=%r exact_id_match=%s name_candidates=%s final=%s rejection=%s",
            league_id,
            season,
            team_id,
            team_name[:120],
            result.standing_row is not None,
            name_candidates[:10],
            result.standing_row is not None,
            "none" if result.standing_row is not None else (
                "name_join_ambiguous" if len(unique_name_candidates) > 1 else "id_join_missing"
            ),
        )
        result.endpoints.append("/standings")
        if result.standing_row is None:
            if result.outcome != FootballOutcome.AMBIGUOUS:
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                result.notes.append("standing_row_missing")
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
        self._active_result = result
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
        self._active_result = result
        resolved_league_id, resolved_season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
        result.endpoints.append("/leagues")
        result.league_context_row = league_row
        if league_outcome != FootballOutcome.SELECTED or resolved_league_id is None or resolved_season is None:
            if league_outcome == FootballOutcome.AMBIGUOUS and operation.team_slots and self.identity_selector is None:
                relation = await self.resolve_team_with_explicit_league(operation)
                result.endpoints.extend(("/teams", "/leagues"))
                if relation.outcome == FootballOutcome.SELECTED:
                    resolved_league_id, resolved_season, league_row = relation.league_id, relation.season, relation.league_row
                    result.league_context_row = league_row
                    team_row = relation.team_row
                else:
                    self._retain_repair_resolution(result, relation)
                    result.outcome = relation.outcome
                    result.notes.append(relation.note)
                    result.generic_rows = list(relation.candidates)
                    result.missing_inputs.append("league")
                    if relation.outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                    return result
            else:
                result.outcome = league_outcome
                result.missing_inputs.append("league")
                return result
        else:
            team_row = None
        if team_row is None:
            team_rows, team_outcome = await self.resolve_team_rows(operation, league_id=resolved_league_id, season=resolved_season, max_teams=1)
            team_row = team_rows[0] if team_outcome == FootballOutcome.SELECTED and team_rows else None
            result.endpoints.append("/teams")
            if team_outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = team_outcome
                result.generic_rows = team_rows
                if team_outcome == FootballOutcome.AMBIGUOUS:
                    result.ambiguity_candidates = _ambiguity_from_rows("team", team_rows)
                else:
                    result.missing_inputs.append("team")
                return result
        return await self.load_team_statistics(result, team_row=team_row, league_id=resolved_league_id, season=resolved_season)

    async def load_team_statistics(self, result: FootballRetrievalResult, *, team_row: dict,
                                    league_id: int, season: int) -> FootballRetrievalResult:
        team = team_row.get("team") or {}
        team_id = team.get("id")
        if type(team_id) is not int:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.notes.append("team_id_missing")
            return result
        result.team_context_row = team_row
        rows = await self.client.get_team_statistics(request=build_team_statistics_request(
            league_id=league_id, season=season, team_id=team_id))
        result.endpoints.append("/teams/statistics")
        result.team_season_statistics = rows[0] if rows else None
        if not rows:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("team_statistics_missing_for_scope")
        return result

    async def get_fixture_preview(self, match: MatchData) -> FootballRetrievalResult:
        result = FootballRetrievalResult(match_data=match)
        fixture = match.fixture or {}
        league = fixture.get("league") or {}
        league_id, season = league.get("id"), league.get("season")
        if type(league_id) is int and type(season) is int:
            try:
                guard = FootballRetrievalResult(match_data=match)
                if await self.check_coverage(guard, "standings", league_id=league_id, season=season):
                    result.notes.append("standings_unavailable")
                else:
                    result.standings_table = football_resolver.extract_table_rows(
                        await self.client.get_standings(league_id=league_id, season=season))
                    result.endpoints.append("/standings")
                    if not result.standings_table:
                        result.notes.append("standings_unavailable")
                result.endpoints.extend(endpoint for endpoint in guard.endpoints if endpoint not in result.endpoints)
            except FootballApiError:
                result.notes.append("standings_unavailable")
        else:
            result.notes.append("standings_scope_missing")
        if type(match.home_team_id) is int and type(match.away_team_id) is int:
            try:
                rows = await self.client.get_head_to_head(team_a_id=match.home_team_id, team_b_id=match.away_team_id, last=5)
                result.endpoints.append("/fixtures/headtohead")
                # The preview sample contains completed earlier meetings, never the selected fixture.
                result.fixtures = [row for row in rows if (row.get("fixture") or {}).get("id") != match.fixture_id
                                   and build_match_data(row, time_scope="fixture_id").lifecycle.is_terminal]
            except FootballApiError:
                result.notes.append("h2h_unavailable")
        else:
            result.notes.append("h2h_unavailable")
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
        self._active_result = result
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
            unsupported = await self.check_coverage(base, "predictions", league_id=league_id, season=season)
            if unsupported:
                return unsupported
            base.predictions = await self.client.get_predictions(build_prediction_request(fixture_id=fixture_id))
            base.endpoints.append("/predictions")
            if not base.predictions:
                base.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                base.notes.append("prediction_missing_for_fixture")
        if focus in {"odds", "pre_match_odds"} or operation.operation_type == "fixture_odds_pre_match":
            unsupported = await self.check_coverage(base, "odds", league_id=league_id, season=season)
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

    async def _resolve_fixture_relationship(
        self,
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
        *,
        operation: FootballQueryOperation,
        league_id: int | None,
        season: int | None,
        live: bool,
        result: FootballRetrievalResult,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, MatchData | None, FootballOutcome]:
        pairs = await self._fixture_relationship_candidates(
            left_rows,
            right_rows,
            operation=operation,
            league_id=league_id,
            season=season,
            live=live,
            result=result,
        )
        if len(pairs) != 1:
            return None, None, None, FootballOutcome.AMBIGUOUS
        left, right, fixture = pairs[0]
        left_team = left.get("team") if isinstance(left.get("team"), dict) else {}
        right_team = right.get("team") if isinstance(right.get("team"), dict) else {}
        match = build_match_data(
            fixture,
            time_scope="live" if live else ("specific_date" if operation.date_hint else "next_match"),
            selected_team_id=left_team.get("id"),
            selected_opponent_id=right_team.get("id"),
            source_endpoints=list(result.endpoints),
        )
        return left, right, match, FootballOutcome.SELECTED

    async def _fixture_relationship_candidates(
        self,
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
        *,
        operation: FootballQueryOperation,
        league_id: int | None,
        season: int | None,
        live: bool,
        result: FootballRetrievalResult,
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        if not left_rows or not right_rows:
            return []
        anchor_rows, other_rows = (
            (left_rows, right_rows) if len(left_rows) <= len(right_rows) else (right_rows, left_rows)
        )
        pairs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        scope = "live" if live else ("specific_date" if operation.date_hint else "next_match")
        for anchor in anchor_rows[:5]:
            anchor_team = anchor.get("team") if isinstance(anchor, dict) else {}
            anchor_id = anchor_team.get("id") if isinstance(anchor_team, dict) else None
            if not isinstance(anchor_id, int):
                continue
            if live:
                fixtures = await self.client.get_live_fixtures(league_id=league_id, team_id=anchor_id)
                endpoint = "/fixtures?live=all"
            elif operation.date_hint:
                fixtures = await self.client.get_fixtures_on_date(
                    league_id=league_id,
                    season=season,
                    date_iso=_date_iso_for_operation(self.client, operation),
                    team_id=anchor_id,
                )
                endpoint = "/fixtures?date"
            else:
                fixtures = await self.client.get_next_fixtures(
                    league_id=league_id,
                    season=season,
                    next_count=5,
                    team_id=anchor_id,
                )
                endpoint = "/fixtures?next"
            if endpoint not in result.endpoints:
                result.endpoints.append(endpoint)
            for fixture in fixtures:
                ids = fixture_team_ids(fixture)
                if anchor_id not in ids:
                    continue
                for other in other_rows[:5]:
                    other_team = other.get("team") if isinstance(other, dict) else {}
                    other_id = other_team.get("id") if isinstance(other_team, dict) else None
                    if not isinstance(other_id, int) or other_id == anchor_id or other_id not in ids:
                        continue
                    pair = (anchor, other, fixture) if anchor_rows is left_rows else (other, anchor, fixture)
                    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
                    fixture_id = fixture_obj.get("id") if isinstance(fixture_obj, dict) else None
                    if not isinstance(fixture_id, int):
                        continue
                    if not any(
                        existing[2].get("fixture", {}).get("id") == fixture_id
                        and existing[0].get("team", {}).get("id") == pair[0].get("team", {}).get("id")
                        and existing[1].get("team", {}).get("id") == pair[1].get("team", {}).get("id")
                        for existing in pairs
                    ):
                        pairs.append(pair)
        return pairs

    async def _resolve_team_scoped_fixture_season_candidates(
        self,
        operation: FootballQueryOperation,
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
        *,
        result: FootballRetrievalResult,
    ) -> tuple[dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]], list[str]]:
        target_date = _date_iso_for_operation(self.client, operation)
        if not target_date:
            return {}, ["fixture_date_missing_for_season"]

        metadata_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
        notes: list[str] = []

        async def seasons_for_rows(team_rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
            by_year: dict[int, list[dict[str, Any]]] = {}
            for team_row in team_rows[:5]:
                team = team_row.get("team") if isinstance(team_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if not isinstance(team_id, int):
                    continue
                try:
                    league_rows = await self.client.search_leagues(
                        build_league_search_request(team_id=team_id)
                    )
                except (AttributeError, FootballApiError, InvalidFootballApiRequest) as exc:
                    notes.append(f"fixture_season_metadata_failed={str(exc)[:120]}")
                    continue
                if "/leagues" not in result.endpoints:
                    result.endpoints.append("/leagues")
                seen_competition_ids: set[int] = set()
                for league_row in league_rows:
                    league = league_row.get("league") if isinstance(league_row, dict) else {}
                    league_id = league.get("id") if isinstance(league, dict) else None
                    if not isinstance(league_id, int) or league_id in seen_competition_ids:
                        continue
                    seen_competition_ids.add(league_id)
                    cache_key = (team_id, league_id)
                    if cache_key not in metadata_cache:
                        metadata = _season_metadata_from_rows([league_row])
                        if not metadata:
                            try:
                                metadata = _season_metadata_from_rows(
                                    await self.client.get_league_by_id(league_id=league_id)
                                )
                            except (AttributeError, FootballApiError, InvalidFootballApiRequest) as exc:
                                notes.append(f"fixture_season_metadata_failed={str(exc)[:120]}")
                                continue
                            if "/leagues" not in result.endpoints:
                                result.endpoints.append("/leagues")
                        metadata_cache[cache_key] = metadata
                    for item in _seasons_containing_date(metadata_cache[cache_key], target_date):
                        year = item.get("year")
                        if isinstance(year, int) and not any(
                            _team_row_id(existing) == team_id for existing in by_year.setdefault(year, [])
                        ):
                            by_year[year].append(team_row)
            return by_year

        left_by_year = await seasons_for_rows(left_rows)
        right_by_year = await seasons_for_rows(right_rows)
        compatible = {
            year: (left_by_year[year], right_by_year[year])
            for year in sorted(set(left_by_year) & set(right_by_year))
            if left_by_year[year] and right_by_year[year]
        }
        if not compatible:
            notes.append("fixture_season_unresolved")
        return compatible, notes

    async def _execute_odds(
        self,
        operation: FootballQueryOperation,
        *,
        league_id: int | None,
        season: int | None,
        data_focus: str | None = None,
    ) -> FootballRetrievalResult:
        result = FootballRetrievalResult()
        self._active_result = result
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        season = _season_from_operation_hint(operation, season)
        if focus in {"odds_mapping", "mapping"} or operation.operation_type == "odds_mapping":
            page_number = operation.reference_page or 1
            page = await self.client.get_odds_mapping(page=page_number)
            result.reference_rows = list(getattr(page, "rows", page) or [])
            result.generic_rows = result.reference_rows
            result.generic_label = "FOOTBALL_ODDS_MAPPING"
            result.endpoints.append("/odds/mapping")
            result.requested_scope["page"] = page_number
            result.requested_scope["paging"] = _paging_payload(page)
            if not result.reference_rows:
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            return result

        live = focus == "live_odds" or operation.operation_type == "fixture_odds_live"
        fixture_id = _fixture_id_from_operation(operation)
        team_row = None
        opponent_row = None
        if fixture_id is not None:
            result.resolved_entities["fixture"] = {"entity_type": "fixture", "id": fixture_id}
        if fixture_id is None and operation.team_slots:
            result.endpoints.append("/teams")
            if _has_opponent_slot(operation):
                left_rows, left_outcome = await self.resolve_team_slot_rows(
                    operation, slot_index=0, league_id=league_id, season=season
                )
                right_rows, right_outcome = await self.resolve_team_slot_rows(
                    operation, slot_index=1, league_id=league_id, season=season
                )
                result.generic_rows = [*left_rows[:5], *right_rows[:5]]
                relation_outcome = FootballOutcome.SELECTED
                if operation.date_hint:
                    if season is None:
                        season_candidates, season_notes = await self._resolve_team_scoped_fixture_season_candidates(
                            operation,
                            left_rows[:5],
                            right_rows[:5],
                            result=result,
                        )
                        result.notes.extend(season_notes)
                    else:
                        season_candidates = {season: (left_rows[:5], right_rows[:5])}
                    relationships: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
                    seen_relationships: set[tuple[int, int, int, int]] = set()
                    for candidate_season, (candidate_left_rows, candidate_right_rows) in season_candidates.items():
                        for candidate_left, candidate_right, fixture in await self._fixture_relationship_candidates(
                            candidate_left_rows,
                            candidate_right_rows,
                            operation=operation,
                            league_id=league_id,
                            season=candidate_season,
                            live=live,
                            result=result,
                        ):
                            fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
                            fixture_id = fixture_obj.get("id") if isinstance(fixture_obj, dict) else None
                            left_id = _team_row_id(candidate_left)
                            right_id = _team_row_id(candidate_right)
                            if not isinstance(fixture_id, int) or not isinstance(left_id, int) or not isinstance(right_id, int):
                                continue
                            key = (candidate_season, fixture_id, left_id, right_id)
                            if key not in seen_relationships:
                                seen_relationships.add(key)
                                relationships.append((candidate_season, candidate_left, candidate_right, fixture))
                    if not season_candidates:
                        relation_outcome = FootballOutcome.RESOLUTION_FAILED
                    elif len(relationships) == 1:
                        season, team_row, opponent_row, fixture = relationships[0]
                        fixture_league = fixture.get("league") if isinstance(fixture, dict) else None
                        fixture_league_id = fixture_league.get("id") if isinstance(fixture_league, dict) else None
                        fixture_season = fixture_league.get("season") if isinstance(fixture_league, dict) else None
                        if not isinstance(fixture_league_id, int):
                            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                            result.notes.append("fixture_league_id_missing")
                            return result
                        if not isinstance(fixture_season, int):
                            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                            result.notes.append("fixture_league_season_missing")
                            return result
                        authoritative_leagues = _authoritative_league_slots(operation.league_slots)
                        if authoritative_leagues:
                            fixture_name = football_resolver.normalize_key(
                                fixture_league.get("name") if isinstance(fixture_league, dict) else None
                            )
                            fixture_country = football_resolver.normalize_key(
                                fixture_league.get("country") if isinstance(fixture_league, dict) else None
                            )
                            scope_matches = any(
                                fixture_name
                                and fixture_name == football_resolver.normalize_key(getattr(slot, "name", None))
                                and (
                                    not getattr(slot, "country_hint", None)
                                    or fixture_country == football_resolver.normalize_key(getattr(slot, "country_hint", None))
                                )
                                for slot in authoritative_leagues
                            )
                            if not scope_matches:
                                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                                result.notes.append("fixture_league_scope_mismatch")
                                return result
                        fixture_league_context = {"league": dict(fixture_league)}
                        fixture_league_context["seasons"] = [{"year": fixture_season}]
                        league_id = fixture_league_id
                        season = fixture_season
                        result.league_context_row = fixture_league_context
                        result.resolved_entities["league"] = _entity_summary("league", fixture_league_context)
                        left_team = team_row.get("team") if isinstance(team_row.get("team"), dict) else {}
                        right_team = opponent_row.get("team") if isinstance(opponent_row.get("team"), dict) else {}
                        result.match_data = build_match_data(
                            fixture,
                            time_scope="specific_date",
                            selected_team_id=left_team.get("id"),
                            selected_opponent_id=right_team.get("id"),
                            source_endpoints=list(result.endpoints),
                        )
                        result.notes.append(f"fixture_season_selected={season}")
                    else:
                        relation_outcome = FootballOutcome.NOT_FOUND if not relationships else FootballOutcome.AMBIGUOUS
                elif left_outcome == FootballOutcome.SELECTED and right_outcome == FootballOutcome.SELECTED:
                    team_row, opponent_row = left_rows[0], right_rows[0]
                else:
                    team_row, opponent_row, resolved_match, relation_outcome = await self._resolve_fixture_relationship(
                        left_rows,
                        right_rows,
                        operation=operation,
                        league_id=league_id,
                        season=season,
                        live=live,
                        result=result,
                    )
                    if relation_outcome == FootballOutcome.SELECTED:
                        result.match_data = resolved_match
                if relation_outcome != FootballOutcome.SELECTED or team_row is None or opponent_row is None:
                    result.outcome = relation_outcome
                    if relation_outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                    elif relation_outcome == FootballOutcome.RESOLUTION_FAILED and season is None:
                        result.missing_inputs.append("season")
                    else:
                        result.missing_inputs.append("team")
                    return result
            else:
                team_rows, team_outcome = await self.resolve_team_rows(
                    operation,
                    league_id=league_id,
                    season=season,
                    max_teams=1,
                )
                team_row = team_rows[0] if team_outcome == FootballOutcome.SELECTED and team_rows else None
                if team_outcome != FootballOutcome.SELECTED or team_row is None:
                    result.outcome = team_outcome
                    result.generic_rows = team_rows
                    if team_outcome == FootballOutcome.AMBIGUOUS:
                        result.ambiguity_candidates = _ambiguity_from_rows("team", team_rows)
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
            opponent = opponent_row.get("team") if isinstance(opponent_row, dict) else {}
            opponent_id = opponent.get("id") if isinstance(opponent, dict) else None
            if _has_opponent_slot(operation) and not isinstance(opponent_id, int):
                result.outcome = FootballOutcome.RESOLUTION_FAILED
                result.notes.append("opponent_id_missing")
                return result
            result.resolved_entities["team"] = _entity_summary("team", team_row)
            if opponent_row is not None:
                result.resolved_entities["opponent"] = _entity_summary("team", opponent_row)
            if result.match_data is None:
                fixture_scope = "live" if live else ("specific_date" if operation.date_hint else "next_match")
                match = await FootballLiveMatchService(self.client).find_recent_fixture_for_team(
                    team_id,
                    fixture_scope,
                    operation.date_hint,
                    opponent_id,
                    league_id=league_id,
                    season=season,
                    today_iso=_today_iso(self.client),
                )
                result.endpoints.extend(endpoint for endpoint in match.source_endpoints if endpoint not in result.endpoints)
                fixture_id = match.fixture_id
                if fixture_id is None:
                    result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                    result.notes.append("fixture_missing_for_odds")
                    return result
                result.match_data = match
            else:
                fixture_id = result.match_data.fixture_id
                if fixture_id is None:
                    result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                    result.notes.append("fixture_missing_for_odds")
                    return result
            result.resolved_entities["fixture"] = {"entity_type": "fixture", "id": fixture_id}

        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                result.outcome = league_outcome
                result.missing_inputs.append("league")
                return result
        if league_id is not None and season is None and not live:
            season = await self.client.get_current_season(league_id)

        unsupported = await self.check_coverage(result, "odds", league_id=league_id, season=season)
        if unsupported:
            return unsupported

        bookmaker_id = None
        bet_id = None
        reference_candidates = operation.odds_bookmaker_candidates if not live else ()
        if reference_candidates:
            rows: list[dict[str, Any]] = []
            for candidate in reference_candidates[:2]:
                rows.extend(await self.client.get_odds_bookmakers(build_odds_reference_request(search=candidate)))
            bookmaker_id, reference_outcome = _select_odds_reference(rows, reference_candidates[0], "bookmaker")
            result.endpoints.append("/odds/bookmakers")
            if reference_outcome != FootballOutcome.SELECTED:
                result.outcome = reference_outcome
                result.generic_rows = rows
                result.ambiguity_candidates = rows[:5]
                return result
        bet_candidates = operation.odds_bet_candidates
        if bet_candidates:
            rows = []
            getter = self.client.get_live_odds_bets if live else self.client.get_odds_bets
            endpoint = "/odds/live/bets" if live else "/odds/bets"
            for candidate in bet_candidates[:2]:
                rows.extend(await getter(build_odds_reference_request(search=candidate)))
            bet_id, reference_outcome = _select_odds_reference(rows, bet_candidates[0], "bet")
            result.endpoints.append(endpoint)
            if reference_outcome != FootballOutcome.SELECTED:
                result.outcome = reference_outcome
                result.generic_rows = rows
                result.ambiguity_candidates = rows[:5]
                return result

        request = build_odds_request(
            fixture_id=fixture_id,
            league_id=league_id,
            season=None if live else season,
            date_iso=_date_iso_for_operation(self.client, operation) if not live else None,
            bookmaker_id=bookmaker_id,
            bet_id=bet_id,
            page=operation.reference_page if not live else None,
            live=live,
        )
        if live:
            result.odds = await self.client.get_live_odds(request)
            result.endpoints.append("/odds/live")
        elif hasattr(self.client, "get_odds_page"):
            page = await self.client.get_odds_page(request)
            result.odds = list(getattr(page, "rows", page) or [])
            result.requested_scope["paging"] = _paging_payload(page)
            result.endpoints.append("/odds")
        else:
            result.odds = await self.client.get_odds(request)
            result.endpoints.append("/odds")
        if not result.odds:
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.notes.append("live_odds_missing_for_scope" if live else "odds_missing_for_scope")
        return result

    async def check_coverage(
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
        coverage = _coverage_from_league_rows(rows, league_id=resolved_league_id, season=resolved_season)
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
        self._active_result = result
        team_id = None
        rows: list[dict[str, Any]] = []
        if operation.coach_candidates:
            for candidate in operation.coach_candidates:
                for search_text in football_resolver.provider_search_variants(candidate):
                    logging.info("AI football coach resolution attempt query=%s", search_text)
                    rows.extend(await self.client.search_coaches(build_coach_search_request(search=search_text)))
                    if rows:
                        break
            result.endpoints.append("/coachs")
        elif operation.team_slots:
            team_rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=1)
            team_row = team_rows[0] if outcome == FootballOutcome.SELECTED and team_rows else None
            result.endpoints.append("/teams")
            if outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = outcome
                result.generic_rows = team_rows
                if outcome == FootballOutcome.AMBIGUOUS:
                    result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                return result
            result.team_context_row = team_row
            team = team_row.get("team") if isinstance(team_row, dict) else {}
            team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, int) and not operation.coach_candidates:
            result.outcome = FootballOutcome.RESOLUTION_FAILED
            result.missing_inputs.append("team")
            return result
        if not operation.coach_candidates:
            await self.load_profile_section(result, "team_coach")
            rows = result.generic_rows
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
        if not rows and result.outcome == FootballOutcome.SELECTED:
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
        self._active_result = result
        if operation.venue_candidates:
            rows: list[dict[str, Any]] = []
            for candidate in operation.venue_candidates:
                rows.extend(await self.client.search_venues(build_venue_search_request(search=candidate)))
            result.endpoints.append("/venues")
            result.venues = rows
            result.generic_rows = rows
            result.generic_label = "FOOTBALL_VENUE"
            if rows:
                return result
            result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
            result.missing_inputs.append("venue")
            return result
        if operation.team_slots:
            team_rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=1)
            team_row = team_rows[0] if outcome == FootballOutcome.SELECTED and team_rows else None
            result.endpoints.append("/teams")
            if outcome != FootballOutcome.SELECTED or team_row is None:
                result.outcome = outcome
                result.generic_rows = team_rows
                return result
            result.team_context_row = team_row
            venue = team_row.get("venue") if isinstance(team_row, dict) and isinstance(team_row.get("venue"), dict) else None
            if venue:
                return await self.load_profile_section(result, "team_venue")
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
        self._active_result = result
        focus = str(data_focus or operation.data_focus or operation.operation_type or "").casefold()
        query = operation.reference_query
        if focus in {"football_timezones", "timezones"}:
            result.reference_rows = await self.client.get_timezones()
            result.endpoints.append("/timezone")
        elif focus in {"football_countries", "countries"}:
            country_filter = operation.country_filter if operation.country_filter in {"name", "code", "search"} else None
            result.reference_rows = await self.client.get_countries(build_catalog_request(search=query, filter=country_filter))
            result.endpoints.append("/countries")
        elif focus in {"league_seasons", "seasons"}:
            result.reference_rows = await self.client.get_league_seasons()
            result.endpoints.append("/leagues/seasons")
        elif focus == "team_countries":
            result.reference_rows = await self.client.get_team_countries()
            result.endpoints.append("/teams/countries")
        elif focus in {"bookmakers", "odds_bookmakers"}:
            result.reference_rows = await self.client.get_odds_bookmakers(build_odds_reference_request(search=query))
            result.endpoints.append("/odds/bookmakers")
        elif focus in {"bets", "odds_bets"}:
            result.reference_rows = await self.client.get_odds_bets(build_odds_reference_request(search=query))
            result.endpoints.append("/odds/bets")
        elif focus in {"live_bets", "odds_live_bets"}:
            result.reference_rows = await self.client.get_live_odds_bets(build_odds_reference_request(search=query))
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
        self._active_result = result
        fixture_id = _fixture_id_from_operation(operation)
        venue_only = str(data_focus or operation.data_focus or "").casefold() in {"venue", "stadium", "team_venue"}
        if fixture_id is not None:
            include_events = not venue_only and operation.operation_type in {"fixture_events", "fixture_result", "player_match_stats", "fixture_result_by_id"}
            include_stats = not venue_only and operation.operation_type in {"fixture_statistics", "fixture_result", "player_match_stats", "fixture_result_by_id"}
            include_lineups = not venue_only and operation.operation_type in {"fixture_lineups", "fixture_result", "fixture_result_by_id"}
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
            elif venue_only:
                fixture_payload = detail.fixture.get("fixture") if isinstance(detail.fixture, dict) else None
                venue = fixture_payload.get("venue") if isinstance(fixture_payload, dict) else None
                if not isinstance(venue, dict) or not str(venue.get("name") or "").strip():
                    result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                    result.notes.append("fixture_venue_missing")
            return result
        target_date_iso = _date_iso_for_operation(self.client, operation)
        league_row = None
        team_row = None
        if operation.league_slots and league_id is None:
            league_id, season, league_row, league_outcome = await self.resolve_league_and_season(operation, league_id=league_id, season=season)
            result.endpoints.append("/leagues")
            result.league_context_row = league_row
            if league_outcome != FootballOutcome.SELECTED or league_id is None:
                if league_outcome == FootballOutcome.AMBIGUOUS and operation.team_slots and self.identity_selector is None:
                    relation = await self.resolve_team_with_explicit_league(operation)
                    result.endpoints.extend(("/teams", "/leagues"))
                    if relation.outcome == FootballOutcome.SELECTED:
                        team_row = relation.team_row
                        league_row = relation.league_row
                        league_id = relation.league_id
                        season = relation.season
                        result.league_context_row = league_row
                    else:
                        self._retain_repair_resolution(result, relation)
                        result.outcome = relation.outcome
                        result.notes.append(relation.note)
                        result.generic_rows = list(relation.candidates)
                        result.missing_inputs.append("league")
                        if relation.outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", list(relation.candidates))
                        return result
                else:
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
        team_outcome = FootballOutcome.RESOLUTION_FAILED
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
            if team_row is None:
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
            else:
                if _has_opponent_slot(operation):
                    rows, outcome = await self.resolve_team_rows(operation, league_id=league_id, season=season, max_teams=2)
                    if "/teams" not in result.endpoints:
                        result.endpoints.append("/teams")
                    if outcome != FootballOutcome.SELECTED or len(rows) < 2:
                        result.outcome = outcome if outcome != FootballOutcome.SELECTED else FootballOutcome.RESOLUTION_FAILED
                        result.notes.append("team_ambiguous" if outcome == FootballOutcome.AMBIGUOUS else "team_not_found")
                        result.generic_rows = rows
                        if outcome == FootballOutcome.AMBIGUOUS:
                            result.ambiguity_candidates = _ambiguity_from_rows("team", result.generic_rows)
                        return result
                    team_row, opponent_row = rows[0], rows[1]
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
        include_events = not venue_only and operation.operation_type in {"fixture_events", "fixture_result", "player_match_stats"}
        include_stats = not venue_only and operation.operation_type in {"fixture_statistics", "fixture_result", "player_match_stats"}
        include_lineups = not venue_only and operation.operation_type in {"fixture_lineups", "fixture_result"}
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
        if venue_only:
            fixture_payload = detail.fixture.get("fixture") if isinstance(detail.fixture, dict) else None
            venue = fixture_payload.get("venue") if isinstance(fixture_payload, dict) else None
            if not isinstance(venue, dict) or not str(venue.get("name") or "").strip():
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                result.notes.append("fixture_venue_missing")
        if operation.operation_type == "fixture_result":
            result.football_entity_context["context_kind"] = "selected_fixture"
            result.football_entity_context["fixture_id"] = detail.fixture_id
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
        self._active_result = result
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

        team_id = None
        if operation.team_slots:
            team_rows, team_outcome = await self.resolve_team_rows(
                operation, league_id=league_id, season=season, max_teams=1, allow_global=league_id is None,
            )
            result.endpoints.append("/teams")
            if team_outcome != FootballOutcome.SELECTED or not team_rows:
                result.outcome = team_outcome
                result.notes.append("team_hint_ambiguous" if team_outcome == FootballOutcome.AMBIGUOUS else "team_hint_not_found")
                result.ambiguity_candidates = _ambiguity_from_rows("team", team_rows)
                result.missing_inputs.append("team")
                return result
            team_row = team_rows[0]
            team_id = (team_row.get("team") or {}).get("id")
            if type(team_id) is not int:
                result.outcome = FootballOutcome.RESOLUTION_FAILED
                result.notes.append("team_id_missing")
                result.missing_inputs.append("team")
                return result
            result.team_context_row = team_row
            result.resolved_entities["team"] = _entity_summary("team", team_row)

            if league_id is not None:
                if season is None:
                    season = await self.client.get_current_season(league_id)
                    result.requested_scope["season"] = season
                if await self.check_coverage(result, "players", league_id=league_id, season=season):
                    return result

        lookup = await football_resolver.resolve_player(
            self.client,
            player_slot,
            league_id=league_id,
            season=season,
            team_id=team_id,
            explicit_context=league_id is not None or team_id is not None,
            canonicalizer=self.player_canonicalizer,
            alias_cache=self.player_alias_cache,
            candidate_names=list(operation.player_slots[:1]),
            stat_focus=operation.stat_focus,
            team_hint=player_slot.team_hint,
            league_hint=player_slot.league_hint,
            country_hint=player_slot.country_hint,
            nationality_hint=player_slot.nationality_hint,
            selector=self.player_selector,
        )
        result.endpoints.append("/players/profiles" if hasattr(self.client, "search_player_profiles") else "/players")
        if any(search.get("stage") == "scope_search" for search in lookup.searches):
            result.endpoints.append("/players")
        if any(search.get("stage") == "squad_scope" for search in lookup.searches):
            result.endpoints.append("/players/squads")
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
        if operation.operation_type == "player_current_team":
            squads = await self._player_squads(player_id)
            if squads:
                result.generic_rows = squads
                result.generic_label = "FOOTBALL_PLAYER_SQUADS"
                result.endpoints.append("/players/squads")

        if operation.operation_type in {"player_teams", "player_career_history"} and getattr(self.client, "get_player_teams", None) is not None:
            await self.load_profile_section(result, "player_teams")
        elif operation.operation_type in {"player_trophies", "player_sidelined", "player_transfers"}:
            await self.load_profile_section(result, operation.operation_type)
        elif operation.operation_type == "player_injuries":
            result.generic_rows = await self.client.get_injuries(build_injury_request(player_id=player_id, league_id=league_id, season=season))
            result.generic_label = "FOOTBALL_PLAYER_INJURIES"
            result.endpoints.append("/injuries")

        needs_stats = operation.operation_type in {
            "player_recent_stats",
            "player_previous_team",
        } or (operation.operation_type in {"player_current_team", "player_career_history"} and not result.generic_rows)
        if needs_stats:
            result.player_context_row = {**player_row, "statistics": []}
            stats_rows = await self._fetch_player_stats_for_locked_identity(
                player_id, league_id=league_id, season=season, team_id=team_id, result=result,
            )
            if stats_rows:
                result.player_context_row = {
                    **player_row,
                    "player": {**(stats_rows[0].get("player") or {}), **player},
                    "statistics": [stat for row in stats_rows for stat in row.get("statistics", [])],
                }
            elif result.outcome == FootballOutcome.SELECTED:
                result.outcome = FootballOutcome.NO_DATA_FOR_SCOPE
                result.notes.append("player_stats_missing_for_requested_scope")

        stats_for_context = result.player_context_row.get("statistics") if isinstance(result.player_context_row, dict) else []
        teams = [stat.get("team") or {} for stat in stats_for_context or [] if isinstance(stat, dict)]
        leagues = [stat.get("league") or {} for stat in stats_for_context or [] if isinstance(stat, dict)]
        team = teams[0] if teams and len({item.get("id") for item in teams}) == 1 else {}
        league = leagues[0] if leagues and len({item.get("id") for item in leagues}) == 1 else {}
        result.notes.append(f"player_found={player.get('name') if isinstance(player, dict) else 'unknown'}")
        result.football_entity_context = {
            "entity_type": "player",
            "player_id": player_id,
            "player_name": player.get("name") if isinstance(player, dict) else None,
            "team_id": team.get("id", team_id),
            "team_name": team.get("name") if isinstance(team, dict) else None,
            "league_id": league.get("id", league_id),
            "league_name": league.get("name") if isinstance(league, dict) else None,
            "season": result.requested_scope.get("season", season),
            "operation_type": operation.operation_type,
            "data_focus": operation.data_focus,
            "source_endpoint": "/players/profiles" if hasattr(self.client, "search_player_profiles") else "/players",
            "ambiguous": False,
        }
        if lookup.query.stat_focus == "penalties":
            result.notes.append("penalty_specific_data=missing")
        if operation.operation_type in {"player_teams", "player_trophies", "player_sidelined", "player_transfers", "player_injuries"} and not result.generic_rows and result.outcome == FootballOutcome.SELECTED:
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
        if season is None and getattr(self.client, "get_player_seasons", None) is not None:
            result.endpoints.append("/players/seasons")
        stat_seasons = await self._player_stat_seasons(player_id, requested_season=season)
        if not stat_seasons:
            result.notes.append("player_stats_season_unresolved")
            return []
        selected_season = stat_seasons[0]
        result.requested_scope["season"] = selected_season
        if league_id is not None:
            unsupported = await self.check_coverage(result, "players", league_id=league_id, season=selected_season)
            if unsupported:
                return []
        rows = await stats_method(build_player_stats_request(
            player_id=player_id, season=selected_season, league_id=league_id, team_id=team_id,
        ))
        result.endpoints.append("/players?id")
        selected = []
        for row in rows:
            if (row.get("player") or {}).get("id") != player_id:
                result.notes.append("player_stats_identity_mismatch")
                continue
            statistics = [stat for stat in (row.get("statistics") or []) if
                          isinstance(stat, dict) and
                          (stat.get("league") or {}).get("season") == selected_season and
                          (league_id is None or (stat.get("league") or {}).get("id") == league_id) and
                          (team_id is None or (stat.get("team") or {}).get("id") == team_id)]
            if statistics:
                selected.append({**row, "statistics": statistics})
        return selected

    async def _player_stat_seasons(self, player_id: int, *, requested_season: int | None) -> list[int]:
        if requested_season is not None:
            return [requested_season]
        method = getattr(self.client, "get_player_seasons", None)
        if method is None:
            return []
        rows = await method(build_player_seasons_request(player_id=player_id))
        seasons = sorted({row["season"] for row in rows
                          if isinstance(row, dict) and type(row.get("season")) is int}, reverse=True)
        return seasons[:1]


def _repair_feedback(operation: FootballQueryOperation, result: FootballRetrievalResult) -> dict[str, Any]:
    """Repair-only provider evidence; never an executable identity."""
    missing_identity_kinds = [slot for slot in ("team", "league") if slot in result.missing_inputs]
    failed_kind = missing_identity_kinds[0] if len(missing_identity_kinds) == 1 else None
    slots = getattr(operation, f"{failed_kind}_slots", ()) if failed_kind else ()
    failed_slots = list(result.repair_failed_slots[:5])
    if not failed_slots and failed_kind and len(slots) == 1:
        failed_slots = [{"kind": failed_kind, "index": 0}]
    failed_slot = failed_slots[0] if len(failed_slots) == 1 else None
    observations = result.repair_observations[:5]
    scope_observations = result.repair_scope_observations
    valid_scope_observations = [
        item for item in scope_observations
        if isinstance(item, dict) and type(item.get("league_id")) is int and type(item.get("season")) is int
    ]
    scoped_values = {
        (item.get("league_id"), item.get("season"))
        for item in valid_scope_observations
        if isinstance(item, dict) and type(item.get("league_id")) is int and type(item.get("season")) is int
    }
    scope = {}
    if scope_observations and len(valid_scope_observations) == len(scope_observations) and len(scoped_values) == 1:
        league_id, season = next(iter(scoped_values))
        scope = {"league_id": league_id, "season": season}
    candidates: list[dict[str, Any]] = []
    observed_identity_ids: set[tuple[str, int]] = set()
    for observation in observations:
        row = observation.get("row") if isinstance(observation, dict) else None
        team = row.get("team") if isinstance(row, dict) else None
        provider_id = team.get("id") if isinstance(team, dict) else None
        name = str(team.get("name") or "").strip() if isinstance(team, dict) else ""
        league_id = observation.get("league_id") if isinstance(observation, dict) else None
        season = observation.get("season") if isinstance(observation, dict) else None
        if not name or type(provider_id) is not int or type(league_id) is not int or type(season) is not int:
            continue
        candidates.append({
            "ref": "fbref:" + secrets.token_urlsafe(9),
            "entity_type": "team",
            "display_name": name[:120],
            "provider_id": provider_id,
            "scope": {"league_id": league_id, "season": season},
        })
        observed_identity_ids.add(("team", provider_id))
    for item in result.ambiguity_candidates[: max(0, 5 - len(candidates))]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or item.get("name") or "").strip()
        entity_type = str(item.get("entity_type") or "").strip()
        provider_id = item.get("id")
        if not name or entity_type not in {"team", "league"} or type(provider_id) is not int:
            continue
        if (entity_type, provider_id) in observed_identity_ids:
            continue
        candidates.append({
            "ref": "fbref:" + secrets.token_urlsafe(9),
            "entity_type": entity_type,
            "display_name": name[:120],
            "provider_id": provider_id,
            "scope": {},
        })
        observed_identity_ids.add((entity_type, provider_id))
    omissions = []
    if not failed_slots and len(missing_identity_kinds) > 1:
        omissions.append("failed_identity_slot_not_unique")
    elif not failed_slots:
        omissions.append("failed_identity_slot")
    elif failed_slot is None:
        omissions.append("failed_identity_slot_not_unique")
    if not candidates:
        omissions.append("observed_provider_candidates")
    if not scope:
        omissions.append("unique_provider_scope")
    resolved = {
        kind: {
            "id": value.get("id"),
            "display_name": str(value.get("display_name") or "")[:120],
        }
        for kind, value in result.resolved_entities.items()
        if kind in {"team", "league"} and isinstance(value, dict) and type(value.get("id")) is int
    }
    return {
        "failed_slot": failed_slot,
        "failed_slots": failed_slots,
        "outcome": result.outcome.value,
        "scope": scope,
        "resolved_entities": resolved,
        "observed_candidates": candidates,
        "omissions": omissions,
    }


def _player_candidate_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for item in rows[:5]:
        player = item.get("player") if isinstance(item, dict) else {}
        if isinstance(player, dict):
            names.append(f"{player.get('name')}#{player.get('id')}"[:120])
    return names


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
    if _fixture_id_from_operation(operation) is not None:
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
        "fixture_odds_pre_match",
        "fixture_odds_live",
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
        **({"fixture_count": operation.fixture_count} if operation.fixture_count is not None else {}),
        **({"fixture_round": operation.fixture_round} if operation.fixture_round is not None else {}),
        "league_id": league_id,
        "season": season,
    }


def _validate_router_temporal_fields(operation: FootballQueryOperation) -> str | None:
    if getattr(operation, "semantic_source", "compatibility") != "router":
        return None
    date_hint = operation.date_hint
    if date_hint is not None:
        if not isinstance(date_hint, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_hint) is None:
            return "invalid_router_date_hint"
        try:
            date.fromisoformat(date_hint)
        except ValueError:
            return "invalid_router_date_hint"
    season_hint = operation.season_hint
    if season_hint is not None and (
        not isinstance(season_hint, str)
        or re.fullmatch(r"(?:19|20)\d{2}", season_hint) is None
    ):
        return "invalid_router_season_hint"
    if operation.time_scope is not None and operation.time_scope not in CANONICAL_FOOTBALL_TIME_SCOPES:
        return "invalid_router_time_scope"
    return None


def _build_request_plan(
    operation: FootballQueryOperation,
    recipe: FootballRecipe,
    *,
    data_focus: str | None,
) -> FootballRequestPlan:
    intent = getattr(operation, "capability_intent", None) or getattr(getattr(operation, "spec", None), "capability_intent", None)
    fallback_required_slots = _fallback_required_slots(operation, recipe)
    return FootballRequestPlan(
        canonical_operation=recipe.key,
        requested_operation=str(operation.operation_type or ""),
        requested_data_focus=data_focus or operation.data_focus,
        temporal_semantics=str(getattr(intent, "temporal_semantics", None) or operation.time_scope or ""),
        required_slots=recipe.required_slots,
        forbidden_slots=recipe.forbidden_slots,
        permitted_endpoints=recipe.permitted_endpoints,
        output_payload=recipe.output_payload,
        fallback_required_slots=fallback_required_slots,
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


def _fallback_required_slots(operation: FootballQueryOperation, recipe: FootballRecipe) -> tuple[str, ...]:
    if recipe.key != "odds":
        return recipe.required_slots
    if operation.operation_type == "odds_mapping":
        return ()
    if _fixture_id_from_operation(operation) is not None:
        return ("fixture",)
    if _has_opponent_slot(operation):
        return ("team", "opponent", "fixture")
    if operation.team_slots:
        return ("team", "fixture")
    return ()


def _validate_request_plan(plan: FootballRequestPlan, operation: FootballQueryOperation) -> FootballRetrievalResult | None:
    focus = str(plan.requested_data_focus or "").casefold()
    op_type = str(operation.operation_type or "").casefold()
    reference_match_scope = (
        bool(operation.date_hint)
        or bool(operation.fixture_focus)
        or (op_type in {"odds_bets", "odds_live_bets"} and len(operation.team_slots) > 1)
    )
    if plan.canonical_operation == "reference" and reference_match_scope:
        return FootballRetrievalResult(
            outcome=FootballOutcome.UNSUPPORTED,
            notes=["capability_recipe_mismatch=reference_with_match_scope"],
        )
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
        "output_payload": list(plan.output_payload),
        "fallback_required_slots": list(plan.fallback_required_slots),
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


def _identity_slot_variants(
    slots: tuple[Any, ...],
    slot_index: int,
    replan: FootballIdentityReplan | tuple[FootballIdentityReplan, ...] | None,
    slot_kind: str,
) -> tuple[Any, ...]:
    if isinstance(replan, tuple):
        replan = next((item for item in replan if item.slot_kind == slot_kind and item.slot_index == slot_index), None)
    if (
        replan is None
        or replan.slot_kind != slot_kind
        or replan.slot_index != slot_index
        or slot_index >= len(slots)
    ):
        return (slots[slot_index],)
    original = slots[slot_index]
    variants: list[Any] = []
    seen: set[str] = set()
    for candidate in replan.candidate_names:
        key = football_resolver.normalize_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        variants.append(replace(original, name=candidate, authority="PLANNER_INFERENCE"))
    return tuple(variants) or (original,)


def _season_from_operation_hint(operation: FootballQueryOperation, current_season: int | None) -> int | None:
    raw = str(getattr(operation, "season_hint", None) or "").strip().casefold()
    if not raw:
        return current_season
    if getattr(operation, "semantic_source", "compatibility") == "router":
        return int(raw) if re.fullmatch(r"(?:19|20)\d{2}", raw) else current_season
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
    if getattr(operation, "semantic_source", "compatibility") == "router":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_hint):
            try:
                date.fromisoformat(raw_hint)
            except ValueError:
                return None
            return raw_hint
        if operation.time_scope in {"today", "yesterday", "tomorrow"}:
            try:
                base_date = date.fromisoformat(_today_iso(client))
            except ValueError:
                return None
            offset = {"today": 0, "yesterday": -1, "tomorrow": 1}[operation.time_scope]
            return (base_date + timedelta(days=offset)).isoformat()
        return None
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


def _nearest_fixture(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dated = []
    for item in fixtures:
        fixture = item.get("fixture") if isinstance(item, dict) else {}
        raw_date = str(fixture.get("date") or "") if isinstance(fixture, dict) else ""
        if raw_date:
            dated.append((raw_date, item))
    if not dated:
        return fixtures[:1]
    return [min(dated, key=lambda item: item[0])[1]]


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


def _paging_payload(page: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for key in ("current", "total"):
        value = getattr(page, key, None)
        if isinstance(value, int):
            result[key] = value
    return result


def _select_odds_reference(
    rows: list[dict[str, Any]],
    candidate: str,
    kind: str,
) -> tuple[int | None, FootballOutcome]:
    target = football_resolver.normalize_key(candidate)
    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get("id")
        label = row.get("name")
        if isinstance(item_id, int) and football_resolver.normalize_key(str(label or "")) == target:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]["id"], FootballOutcome.SELECTED
    if len(matches) > 1:
        return None, FootballOutcome.AMBIGUOUS
    return None, FootballOutcome.NOT_FOUND


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
    _log_team_identity_candidates(rows, slots, lookup_mode="team_resolution")
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


def _log_team_identity_candidates(rows: list[dict[str, Any]], slots: tuple[Any, ...], *, lookup_mode: str) -> None:
    for index, row in enumerate(rows[:50]):
        team = row.get("team") if isinstance(row, dict) else {}
        venue = row.get("venue") if isinstance(row, dict) else {}
        if not isinstance(team, dict):
            team = {}
        if not isinstance(venue, dict):
            venue = {}
        slot_scores = [
            (str(getattr(slot, "name", ""))[:120], _team_row_score(row, slot))
            for slot in slots[:4]
        ]
        team_id = _team_row_id(row)
        score = max((item[1] for item in slot_scores), default=0)
        accepted = isinstance(team_id, int) and score > 0
        rejection = "none" if accepted else (
            "non_integer_id" if not isinstance(team_id, int) else "name_mismatch"
        )
        logging.debug(
            "AI football team identity candidate evaluated lookup_mode=%s row_index=%s team_id=%r "
            "team_name=%r team_code=%r team_country=%r team_national=%r venue_city=%r "
            "team_candidates=%s candidate_scores=%s accepted=%s rejection=%s",
            lookup_mode,
            index,
            team_id,
            str(team.get("name") or "")[:120],
            team.get("code"),
            team.get("country"),
            team.get("national"),
            str(venue.get("city") or "")[:120],
            [str(getattr(slot, "name", ""))[:120] for slot in slots[:4]],
            slot_scores,
            accepted,
            rejection,
        )


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


def _team_roster_identity_score(row: dict[str, Any], slot: Any) -> int:
    """Compare only provider identity fields within an already-selected league roster."""
    team = row.get("team") if isinstance(row, dict) else {}
    venue = row.get("venue") if isinstance(row, dict) else {}
    if not isinstance(team, dict):
        return 0
    if not isinstance(venue, dict):
        venue = {}
    slot_tokens = _team_identity_tokens(getattr(slot, "name", None))
    if not slot_tokens:
        return 0
    team_name_tokens = _team_identity_tokens(team.get("name"))
    venue_city_tokens = _team_identity_tokens(venue.get("city"))
    slot_key = "".join(slot_tokens)
    if team_name_tokens == slot_tokens:
        return 300
    if football_resolver.normalize_key(team.get("code")) == slot_key:
        return 250
    if venue_city_tokens == slot_tokens:
        return 200
    if _team_identity_contains(team_name_tokens, slot_tokens) or _team_identity_contains(venue_city_tokens, slot_tokens):
        return 100
    return 0


def _team_identity_tokens(value: object) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    while tokens:
        if tokens[-1] in {"fc", "afc", "cf"}:
            tokens.pop()
        elif len(tokens) >= 2 and tokens[-2:] == ["f", "c"]:
            del tokens[-2:]
        else:
            break
    return tuple(tokens)


def _team_identity_contains(provider_tokens: tuple[str, ...], requested_tokens: tuple[str, ...]) -> bool:
    if not provider_tokens or not requested_tokens or len(requested_tokens) > len(provider_tokens):
        return False
    return provider_tokens[: len(requested_tokens)] == requested_tokens or set(requested_tokens).issubset(provider_tokens)


def _team_row_id(row: dict[str, Any]) -> int | None:
    team = row.get("team") if isinstance(row, dict) else {}
    team_id = team.get("id") if isinstance(team, dict) else None
    return team_id if type(team_id) is int else None


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


def _authoritative_league_slots(slots: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(slot for slot in slots if getattr(slot, "authority", None) != "PLANNER_INFERENCE")


def _unique_current_league_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _unique_league_rows(rows)


def _unique_league_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for row in rows:
        league = row.get("league") if isinstance(row, dict) else None
        if not isinstance(league, dict) or str(league.get("type") or "").casefold() != "league":
            continue
        league_id = league.get("id")
        if type(league_id) is not int or league_id in seen_ids:
            continue
        seen_ids.add(league_id)
        selected.append(row)
    return selected


def _season_from_league_row(row: dict[str, Any]) -> int | None:
    seasons = row.get("seasons") if isinstance(row, dict) else None
    years = [
        item.get("year")
        for item in seasons or ()
        if isinstance(item, dict) and item.get("current") is True and isinstance(item.get("year"), int)
    ]
    return max(years) if years else None


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


def _coverage_from_league_rows(rows: list[dict[str, Any]], *, league_id: int | None = None,
                               season: int | None = None) -> dict[str, Any]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if league_id is not None and (row.get("league") or {}).get("id") != league_id:
            continue
        seasons = row.get("seasons") if isinstance(row, dict) else None
        if isinstance(seasons, list):
            for entry in seasons:
                if isinstance(entry, dict) and (season is None or entry.get("year") == season) and isinstance(entry.get("coverage"), dict):
                    return entry["coverage"]
            continue
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
