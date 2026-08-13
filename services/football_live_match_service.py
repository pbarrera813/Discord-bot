from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import logging
import re
import unicodedata
from typing import Any

from services.football_formatter import fixture_datetime, fixture_score, fixture_status, fixture_teams
from services import football_resolver


TERMINAL_STATUS_SHORTS = {"FT", "AET", "PEN"}


@dataclass(frozen=True)
class MatchStat:
    team_id: int | None
    team_name: str
    original_label: str
    normalized_label: str
    raw_value: object
    display_value: str
    numeric_value: float | None = None
    category: str = "unknown"
    derived: bool = False


@dataclass(frozen=True)
class MatchLifecycle:
    status_short: str
    status_long: str
    phase: str
    elapsed: int | None = None
    extra: int | None = None
    is_scheduled: bool = False
    is_live: bool = False
    is_paused: bool = False
    is_shootout: bool = False
    is_terminal: bool = False
    is_interrupted: bool = False


@dataclass(frozen=True)
class NormalizedMatchEvent:
    event_key: str
    order: int
    event_type: str
    detail: str
    team_id: int | None
    team_name: str
    player_id: int | None
    player_name: str
    assist_id: int | None = None
    assist_name: str = ""
    elapsed: int | None = None
    extra: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShootoutAttempt:
    attempt_key: str
    order: int
    team_id: int | None
    team_name: str
    player_id: int | None
    player_name: str
    scored: bool
    detail: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShootoutSummary:
    home_penalties: int | None = None
    away_penalties: int | None = None
    attempts: list[ShootoutAttempt] = field(default_factory=list)
    aggregate_available: bool = False
    attempts_available: bool = False
    attempts_ambiguous: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FixturePlayerPerformance:
    team_id: int | None
    team_name: str
    player_id: int | None
    player_name: str
    metrics: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchData:
    fixture: dict[str, Any] | None
    fixture_id: int | None
    time_scope: str
    home_team_id: int | None = None
    home_team_name: str = ""
    away_team_id: int | None = None
    away_team_name: str = ""
    score: str = ""
    status: str = ""
    status_short: str = ""
    fixture_date: str | None = None
    selected_team_id: int | None = None
    selected_opponent_id: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    statistics: list[dict[str, Any]] = field(default_factory=list)
    lineups: list[dict[str, Any]] = field(default_factory=list)
    players: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[int | str, dict[str, MatchStat]] = field(default_factory=dict)
    lifecycle: MatchLifecycle | None = None
    normalized_events: list[NormalizedMatchEvent] = field(default_factory=list)
    shootout: ShootoutSummary | None = None
    half_statistics: dict[str, dict[int | str, dict[str, MatchStat]]] = field(default_factory=dict)
    player_performances: list[FixturePlayerPerformance] = field(default_factory=list)
    source_endpoints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def normalize_stat_label(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


STAT_SYNONYMS: dict[str, str] = {
    "tiro_a_gol": "shots_on_goal",
    "tiros_a_gol": "shots_on_goal",
    "tiro_a_puerta": "shots_on_goal",
    "tiros_a_puerta": "shots_on_goal",
    "shots_on_goal": "shots_on_goal",
    "shots_on_target": "shots_on_goal",
    "remates_a_puerta": "shots_on_goal",
    "tiros": "total_shots",
    "remates": "total_shots",
    "total_shots": "total_shots",
    "tiros_fuera": "shots_off_goal",
    "shots_off_goal": "shots_off_goal",
    "tiros_bloqueados": "blocked_shots",
    "blocked_shots": "blocked_shots",
    "posesion": "ball_possession",
    "posesion_del_balon": "ball_possession",
    "ball_possession": "ball_possession",
    "possession": "ball_possession",
    "corners": "corner_kicks",
    "corner": "corner_kicks",
    "tiros_de_esquina": "corner_kicks",
    "corner_kicks": "corner_kicks",
    "faltas": "fouls",
    "fouls": "fouls",
    "offsides": "offsides",
    "fuera_de_lugar": "offsides",
    "fueras_de_lugar": "offsides",
    "atajadas": "goalkeeper_saves",
    "salvadas": "goalkeeper_saves",
    "goalkeeper_saves": "goalkeeper_saves",
    "pases": "total_passes",
    "total_passes": "total_passes",
    "pases_acertados": "passes_accurate",
    "pases_completados": "passes_accurate",
    "passes_accurate": "passes_accurate",
    "porcentaje_de_pases": "passes_percent",
    "precision_de_pase": "passes_percent",
    "passes": "total_passes",
    "passes_percent": "passes_percent",
    "amarillas": "yellow_cards",
    "tarjetas_amarillas": "yellow_cards",
    "yellow_cards": "yellow_cards",
    "rojas": "red_cards",
    "tarjetas_rojas": "red_cards",
    "red_cards": "red_cards",
}


API_STAT_ALIASES: dict[str, str] = {
    "shots_on_goal": "shots_on_goal",
    "shots_on_target": "shots_on_goal",
    "total_shots": "total_shots",
    "shots_total": "total_shots",
    "shots_off_goal": "shots_off_goal",
    "blocked_shots": "blocked_shots",
    "ball_possession": "ball_possession",
    "possession": "ball_possession",
    "corner_kicks": "corner_kicks",
    "corners": "corner_kicks",
    "fouls": "fouls",
    "offsides": "offsides",
    "goalkeeper_saves": "goalkeeper_saves",
    "total_passes": "total_passes",
    "passes_accurate": "passes_accurate",
    "passes": "total_passes",
    "passes_percent": "passes_percent",
    "yellow_cards": "yellow_cards",
    "red_cards": "red_cards",
}


ADDITIVE_STAT_KEYS = {
    "shots_on_goal",
    "total_shots",
    "shots_off_goal",
    "blocked_shots",
    "fouls",
    "corner_kicks",
    "offsides",
    "goalkeeper_saves",
    "total_passes",
    "passes_accurate",
    "yellow_cards",
    "red_cards",
}
PERCENTAGE_STAT_KEYS = {"ball_possession", "passes_percent"}


def stat_category(normalized_label: str) -> str:
    if normalized_label in ADDITIVE_STAT_KEYS:
        return "additive"
    if normalized_label in PERCENTAGE_STAT_KEYS or normalized_label.endswith("_percent") or normalized_label.endswith("_percentage"):
        return "percentage"
    if any(token in normalized_label for token in ("rating", "average", "avg")):
        return "snapshot"
    return "unknown"


def requested_stat_key(value: str | None) -> str | None:
    normalized = normalize_stat_label(value)
    if not normalized:
        return None
    if normalized in STAT_SYNONYMS:
        return STAT_SYNONYMS[normalized]
    for phrase, key in STAT_SYNONYMS.items():
        if phrase and (phrase in normalized or normalized in phrase):
            return key
    return API_STAT_ALIASES.get(normalized, normalized)


def normalize_match_statistics(rows: list[dict[str, Any]]) -> dict[int | str, dict[str, MatchStat]]:
    parsed: dict[int | str, dict[str, MatchStat]] = {}
    for row in rows:
        team = row.get("team") if isinstance(row, dict) else {}
        if not isinstance(team, dict):
            continue
        team_id = team.get("id") if isinstance(team.get("id"), int) else None
        team_name = str(team.get("name") or "").strip()
        if team_id is None and not team_name:
            continue
        bucket_key: int | str = team_id if team_id is not None else team_name
        stats = row.get("statistics") if isinstance(row.get("statistics"), list) else []
        bucket = parsed.setdefault(bucket_key, {})
        for item in stats:
            if not isinstance(item, dict):
                continue
            original = str(item.get("type") or "").strip()
            if not original:
                continue
            normalized = canonical_api_stat_key(original)
            raw_value = item.get("value")
            display = "" if raw_value is None else str(raw_value).strip()
            numeric = numeric_stat_value(raw_value)
            if not display and numeric is None:
                continue
            bucket[normalized] = MatchStat(
                team_id=team_id,
                team_name=team_name,
                original_label=original,
                normalized_label=normalized,
                raw_value=raw_value,
                display_value=display,
                numeric_value=numeric,
                category=stat_category(normalized),
            )
    return parsed


def derive_second_half_statistics(
    full_match_rows: list[dict[str, Any]],
    first_half_rows: list[dict[str, Any]],
) -> dict[int | str, dict[str, MatchStat]]:
    full = normalize_match_statistics(full_match_rows)
    first = normalize_match_statistics(first_half_rows)
    derived: dict[int | str, dict[str, MatchStat]] = {}
    for team_key, full_stats in full.items():
        first_stats = first.get(team_key, {})
        for key, full_stat in full_stats.items():
            if full_stat.category != "additive":
                continue
            first_stat = first_stats.get(key)
            if first_stat is None or full_stat.numeric_value is None or first_stat.numeric_value is None:
                continue
            value = full_stat.numeric_value - first_stat.numeric_value
            if value < 0:
                continue
            display = str(int(value)) if value.is_integer() else str(value)
            derived.setdefault(team_key, {})[key] = replace(
                full_stat,
                raw_value=value,
                display_value=display,
                numeric_value=value,
                derived=True,
            )
    return derived


def normalize_half_statistics(
    *,
    full_match_rows: list[dict[str, Any]],
    first_half_rows: list[dict[str, Any]],
    second_half_rows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[int | str, dict[str, MatchStat]]]:
    return {
        "full": normalize_match_statistics(full_match_rows),
        "first_half": normalize_match_statistics(first_half_rows),
        "second_half": normalize_match_statistics(second_half_rows) if second_half_rows else derive_second_half_statistics(full_match_rows, first_half_rows),
    }


def match_requested_stat(
    stats: dict[int | str, dict[str, MatchStat]],
    requested: str | None,
) -> str | None:
    requested_key = requested_stat_key(requested)
    if not requested_key:
        return None
    available = {key for team_stats in stats.values() for key in team_stats}
    if requested_key in available:
        return requested_key
    requested_tokens = set(filter(None, requested_key.split("_")))
    if not requested_tokens:
        return None
    matches: list[str] = []
    for key in available:
        key_tokens = set(filter(None, key.split("_")))
        if requested_tokens == key_tokens or requested_tokens.issubset(key_tokens) or key_tokens.issubset(requested_tokens):
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


def canonical_api_stat_key(label: str) -> str:
    normalized = normalize_stat_label(label)
    lowered = str(label or "").casefold()
    if "pass" in normalized and ("%" in lowered or "percent" in lowered):
        return "passes_percent"
    return API_STAT_ALIASES.get(normalized, normalized)


def numeric_stat_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def stats_for_watch(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for team_stats in normalize_match_statistics(rows).values():
        team_name = ""
        values: dict[str, float] = {}
        for stat in team_stats.values():
            team_name = stat.team_name or team_name
            if stat.numeric_value is not None:
                values[stat.normalized_label] = stat.numeric_value
                legacy = {
                    "shots_on_goal": "shots_on_target",
                    "total_shots": "shots",
                    "corner_kicks": "corners",
                    "ball_possession": "possession",
                }.get(stat.normalized_label)
                if legacy:
                    values[legacy] = stat.numeric_value
        if team_name and values:
            result[team_name] = values
    return result


def build_lifecycle(fixture: dict[str, Any] | None) -> MatchLifecycle:
    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) and isinstance(fixture.get("fixture"), dict) else {}
    status = fixture_obj.get("status") if isinstance(fixture_obj.get("status"), dict) else {}
    short = str(status.get("short") or "").upper()
    long = str(status.get("long") or "").strip()
    elapsed = status.get("elapsed") if isinstance(status.get("elapsed"), int) else None
    extra = status.get("extra") if isinstance(status.get("extra"), int) else None
    phase = {
        "TBD": "scheduled",
        "NS": "scheduled",
        "1H": "first_half",
        "HT": "halftime",
        "2H": "second_half",
        "ET": "extra_time",
        "BT": "extra_time_break",
        "P": "shootout",
        "FT": "finished",
        "AET": "finished_extra_time",
        "PEN": "finished_penalties",
        "SUSP": "suspended",
        "INT": "interrupted",
        "PST": "postponed",
        "CANC": "cancelled",
        "ABD": "abandoned",
        "AWD": "awarded",
        "WO": "walkover",
        "LIVE": "live",
    }.get(short, "unknown")
    return MatchLifecycle(
        status_short=short,
        status_long=long,
        phase=phase,
        elapsed=elapsed,
        extra=extra,
        is_scheduled=short in {"TBD", "NS"},
        is_live=short in {"1H", "2H", "ET", "P", "LIVE"},
        is_paused=short in {"HT", "BT"},
        is_shootout=short == "P",
        is_terminal=short in {"FT", "AET", "PEN"},
        is_interrupted=short in {"SUSP", "INT", "PST", "CANC", "ABD", "AWD", "WO"},
    )


def match_event_key(event: dict[str, Any], order: int = 0) -> str:
    event_id = event.get("id") if isinstance(event, dict) else None
    if isinstance(event_id, (int, str)) and str(event_id).strip():
        return f"id:{event_id}"
    time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
    team = event.get("team") if isinstance(event.get("team"), dict) else {}
    player = event.get("player") if isinstance(event.get("player"), dict) else {}
    return "|".join(
        "" if part is None else str(part)
        for part in (
            order,
            time_info.get("elapsed"),
            time_info.get("extra"),
            team.get("id") or team.get("name"),
            player.get("id") or player.get("name"),
            event.get("type"),
            event.get("detail"),
        )
    )


def normalize_match_events(events: list[dict[str, Any]]) -> list[NormalizedMatchEvent]:
    normalized: list[NormalizedMatchEvent] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
        team = event.get("team") if isinstance(event.get("team"), dict) else {}
        player = event.get("player") if isinstance(event.get("player"), dict) else {}
        assist = event.get("assist") if isinstance(event.get("assist"), dict) else {}
        normalized.append(
            NormalizedMatchEvent(
                event_key=match_event_key(event, index),
                order=index,
                event_type=str(event.get("type") or "").strip(),
                detail=str(event.get("detail") or "").strip(),
                team_id=team.get("id") if isinstance(team.get("id"), int) else None,
                team_name=str(team.get("name") or ""),
                player_id=player.get("id") if isinstance(player.get("id"), int) else None,
                player_name=str(player.get("name") or ""),
                assist_id=assist.get("id") if isinstance(assist.get("id"), int) else None,
                assist_name=str(assist.get("name") or ""),
                elapsed=time_info.get("elapsed") if isinstance(time_info.get("elapsed"), int) else None,
                extra=time_info.get("extra") if isinstance(time_info.get("extra"), int) else None,
                raw=event,
            )
        )
    return normalized


def normalize_shootout(
    fixture: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    pre_shootout_event_keys: set[str] | None = None,
    live_entered_shootout: bool = False,
) -> ShootoutSummary:
    score = fixture.get("score") if isinstance(fixture, dict) and isinstance(fixture.get("score"), dict) else {}
    penalties = score.get("penalty") if isinstance(score.get("penalty"), dict) else {}
    home_pen = penalties.get("home")
    away_pen = penalties.get("away")
    aggregate_available = isinstance(home_pen, int) or isinstance(away_pen, int)
    lifecycle = build_lifecycle(fixture)
    baseline = pre_shootout_event_keys or set()
    attempts: list[ShootoutAttempt] = []
    for index, event in enumerate(events):
        key = match_event_key(event, index)
        if key in baseline or not _event_is_penalty_attempt(event):
            continue
        if lifecycle.status_short == "P" and live_entered_shootout:
            safe = True
        else:
            safe = _event_has_explicit_shootout_phase(event)
        if not safe:
            continue
        team = event.get("team") if isinstance(event.get("team"), dict) else {}
        player = event.get("player") if isinstance(event.get("player"), dict) else {}
        detail = str(event.get("detail") or "")
        attempts.append(
            ShootoutAttempt(
                attempt_key=f"shootout:{key}",
                order=len(attempts),
                team_id=team.get("id") if isinstance(team.get("id"), int) else None,
                team_name=str(team.get("name") or ""),
                player_id=player.get("id") if isinstance(player.get("id"), int) else None,
                player_name=str(player.get("name") or ""),
                scored="missed" not in detail.casefold(),
                detail=detail,
                raw=event,
            )
        )
    notes: list[str] = []
    if aggregate_available and not attempts:
        notes.append("shootout_attempt_detail_unavailable")
    if attempts and aggregate_available:
        home_id, away_id = _fixture_side_ids(fixture)
        home_scored = sum(1 for item in attempts if item.scored and item.team_id == home_id)
        away_scored = sum(1 for item in attempts if item.scored and item.team_id == away_id)
        if isinstance(home_pen, int) and home_id is not None and home_scored != home_pen:
            notes.append("shootout_home_attempts_do_not_match_aggregate")
        if isinstance(away_pen, int) and away_id is not None and away_scored != away_pen:
            notes.append("shootout_away_attempts_do_not_match_aggregate")
    return ShootoutSummary(
        home_penalties=home_pen if isinstance(home_pen, int) else None,
        away_penalties=away_pen if isinstance(away_pen, int) else None,
        attempts=attempts,
        aggregate_available=aggregate_available,
        attempts_available=bool(attempts),
        attempts_ambiguous=aggregate_available and not attempts,
        notes=notes,
    )


def normalize_fixture_players(rows: list[dict[str, Any]]) -> list[FixturePlayerPerformance]:
    performances: list[FixturePlayerPerformance] = []
    for row in rows:
        team = row.get("team") if isinstance(row, dict) and isinstance(row.get("team"), dict) else {}
        players = row.get("players") if isinstance(row, dict) and isinstance(row.get("players"), list) else []
        for item in players:
            if not isinstance(item, dict):
                continue
            player = item.get("player") if isinstance(item.get("player"), dict) else {}
            metrics: dict[str, Any] = {}
            stats_items = item.get("statistics") if isinstance(item.get("statistics"), list) else []
            for stats in stats_items:
                if isinstance(stats, dict):
                    _flatten_metrics(stats, metrics)
            performances.append(
                FixturePlayerPerformance(
                    team_id=team.get("id") if isinstance(team.get("id"), int) else None,
                    team_name=str(team.get("name") or ""),
                    player_id=player.get("id") if isinstance(player.get("id"), int) else None,
                    player_name=str(player.get("name") or ""),
                    metrics=metrics,
                    raw=item,
                )
            )
    return performances


def _flatten_metrics(value: dict[str, Any], target: dict[str, Any], prefix: str = "") -> None:
    for key, item in value.items():
        if key in {"team", "player"}:
            continue
        metric_key = normalize_stat_label(f"{prefix}_{key}" if prefix else str(key))
        if isinstance(item, dict):
            _flatten_metrics(item, target, metric_key)
        else:
            target[metric_key] = item


def _event_is_penalty_attempt(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").casefold()
    detail = str(event.get("detail") or "").casefold()
    return event_type == "goal" and ("penalty" in detail or "penal" in detail)


def _event_has_explicit_shootout_phase(event: dict[str, Any]) -> bool:
    for key in ("phase", "period", "comments"):
        value = str(event.get(key) or "").casefold()
        if "shootout" in value or value == "p":
            return True
    time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
    phase = str(time_info.get("phase") or time_info.get("period") or "").casefold()
    return "shootout" in phase or phase == "p"


def _fixture_side_ids(fixture: dict[str, Any] | None) -> tuple[int | None, int | None]:
    teams = fixture.get("teams") if isinstance(fixture, dict) and isinstance(fixture.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    return (
        home.get("id") if isinstance(home.get("id"), int) else None,
        away.get("id") if isinstance(away.get("id"), int) else None,
    )


class FootballLiveMatchService:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def find_live_or_today_fixture_for_team(
        self,
        team_id: int,
        date_iso: str,
        opponent_id: int | None = None,
        *,
        league_id: int | None = None,
        season: int | None = None,
    ) -> MatchData:
        endpoints: list[str] = []
        fixtures = await self.client.get_live_fixtures(league_id=league_id, team_id=team_id)
        endpoints.append("/fixtures?live=all")
        filtered = filter_fixtures(fixtures, team_id=team_id, opponent_id=opponent_id)
        if filtered:
            return build_match_data(filtered[0], time_scope="live", selected_team_id=team_id, selected_opponent_id=opponent_id, source_endpoints=endpoints)
        fixtures = await self.client.get_fixtures_on_date(league_id=league_id, season=season, team_id=team_id, date_iso=date_iso)
        endpoints.append("/fixtures?date")
        filtered = filter_fixtures(fixtures, team_id=team_id, opponent_id=opponent_id, date_iso=date_iso)
        if filtered:
            return build_match_data(
                filtered[0],
                time_scope="today",
                selected_team_id=team_id,
                selected_opponent_id=opponent_id,
                source_endpoints=endpoints,
                notes=["live_status_missing_same_day_fixture_found"],
            )
        return _match_or_empty(filtered, "today", team_id, opponent_id, endpoints)

    async def find_last_finished_fixture_for_team(
        self,
        team_id: int,
        opponent_id: int | None = None,
        *,
        league_id: int | None = None,
        season: int | None = None,
        last_count: int = 10,
    ) -> MatchData:
        fixtures = await self.client.get_last_fixtures(league_id=league_id, season=season, last_count=last_count, team_id=team_id)
        filtered = filter_fixtures(fixtures, team_id=team_id, opponent_id=opponent_id, terminal_only=True)
        return _match_or_empty(filtered, "last_finished_match", team_id, opponent_id, ["/fixtures?last"])

    async def find_fixture_by_team_and_date(
        self,
        team_id: int,
        date_iso: str,
        opponent_id: int | None = None,
        *,
        league_id: int | None = None,
        season: int | None = None,
    ) -> MatchData:
        fixtures = await self.client.get_fixtures_on_date(league_id=league_id, season=season, team_id=team_id, date_iso=date_iso)
        filtered = filter_fixtures(fixtures, team_id=team_id, opponent_id=opponent_id, date_iso=date_iso)
        return _match_or_empty(filtered, "specific_date", team_id, opponent_id, ["/fixtures?date"])

    async def find_recent_fixture_for_team(
        self,
        team_id: int,
        time_scope: str,
        date_hint: str | None = None,
        opponent_id: int | None = None,
        *,
        league_id: int | None = None,
        season: int | None = None,
        today_iso: str | None = None,
    ) -> MatchData:
        scope = str(time_scope or "").strip() or "recent_finished"
        local_today = today_iso or _client_today_iso(self.client)
        if scope == "live":
            return await self.find_live_or_today_fixture_for_team(team_id, local_today, opponent_id, league_id=league_id, season=season)
        if scope == "today":
            return await self.find_fixture_by_team_and_date(team_id, local_today, opponent_id, league_id=league_id, season=season)
        if scope == "yesterday":
            target = _relative_date(local_today, -1)
            return await self.find_fixture_by_team_and_date(team_id, target, opponent_id, league_id=league_id, season=season)
        if scope == "specific_date" and date_hint:
            return await self.find_fixture_by_team_and_date(team_id, date_hint, opponent_id, league_id=league_id, season=season)
        if scope == "next_match":
            fixtures = await self.client.get_next_fixtures(league_id=league_id, season=season, next_count=5, team_id=team_id)
            filtered = filter_fixtures(fixtures, team_id=team_id, opponent_id=opponent_id)
            return _match_or_empty(filtered, "next_match", team_id, opponent_id, ["/fixtures?next"])
        return await self.find_last_finished_fixture_for_team(team_id, opponent_id, league_id=league_id, season=season)

    async def get_match_center(
        self,
        fixture_id: int,
        *,
        time_scope: str,
        include_events: bool = False,
        include_stats: bool = False,
        include_lineups: bool = False,
        include_players: bool = False,
        include_half_stats: bool = False,
    ) -> MatchData:
        fixtures = await self.client.get_fixture_by_id(fixture_id=fixture_id)
        fixture = fixtures[0] if fixtures else None
        endpoints = ["/fixtures?id"]
        embedded_events = _embedded_list(fixture, "events")
        embedded_stats = _embedded_list(fixture, "statistics")
        embedded_lineups = _embedded_list(fixture, "lineups")
        embedded_players = _embedded_list(fixture, "players")
        events = embedded_events if include_events and embedded_events else []
        stats = embedded_stats if include_stats and embedded_stats else []
        lineups = embedded_lineups if include_lineups and embedded_lineups else []
        players = embedded_players if include_players and embedded_players else []
        half_stats: list[dict[str, Any]] = []
        if include_events and not events and hasattr(self.client, "get_fixture_events"):
            events = await self.client.get_fixture_events(fixture_id=fixture_id)
            endpoints.append("/fixtures/events")
        if include_stats and not stats and hasattr(self.client, "get_fixture_statistics"):
            stats = await self.client.get_fixture_statistics(fixture_id=fixture_id)
            endpoints.append("/fixtures/statistics")
        if include_half_stats and hasattr(self.client, "get_fixture_statistics"):
            half_stats = await self.client.get_fixture_statistics(fixture_id=fixture_id, half=True)
            endpoints.append("/fixtures/statistics?half=true")
        if include_lineups and not lineups and hasattr(self.client, "get_fixture_lineups"):
            lineups = await self.client.get_fixture_lineups(fixture_id=fixture_id)
            endpoints.append("/fixtures/lineups")
        if include_players and not players and hasattr(self.client, "get_fixture_players"):
            players = await self.client.get_fixture_players(fixture_id=fixture_id)
            endpoints.append("/fixtures/players")
        return build_match_data(
            fixture,
            time_scope=time_scope,
            events=events,
            statistics=stats,
            lineups=lineups,
            players=players,
            half_statistics=normalize_half_statistics(full_match_rows=stats, first_half_rows=half_stats) if include_half_stats and half_stats else {},
            source_endpoints=endpoints,
        )


def filter_fixtures(
    fixtures: list[dict[str, Any]],
    *,
    team_id: int | None,
    opponent_id: int | None = None,
    date_iso: str | None = None,
    terminal_only: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fixture in fixtures:
        ids = fixture_team_ids(fixture)
        if team_id is not None and team_id not in ids:
            continue
        if opponent_id is not None and opponent_id not in ids:
            continue
        if date_iso and not fixture_date_matches(fixture, date_iso):
            continue
        if terminal_only and fixture_status_short(fixture) not in TERMINAL_STATUS_SHORTS:
            continue
        result.append(fixture)
    return result


def fixture_team_ids(fixture: dict[str, Any]) -> set[int]:
    teams = fixture.get("teams") if isinstance(fixture, dict) else {}
    ids: set[int] = set()
    if not isinstance(teams, dict):
        return ids
    for side in ("home", "away"):
        team = teams.get(side) if isinstance(teams.get(side), dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        if isinstance(team_id, int):
            ids.add(team_id)
    return ids


def fixture_status_short(fixture: dict[str, Any]) -> str:
    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
    status = fixture_obj.get("status") if isinstance(fixture_obj, dict) and isinstance(fixture_obj.get("status"), dict) else {}
    return str(status.get("short") or "").upper()


def fixture_date_matches(fixture: dict[str, Any], date_iso: str) -> bool:
    fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
    raw = str(fixture_obj.get("date") or "")
    return bool(raw) and raw[:10] == date_iso


def build_match_data(
    fixture: dict[str, Any] | None,
    *,
    time_scope: str,
    selected_team_id: int | None = None,
    selected_opponent_id: int | None = None,
    events: list[dict[str, Any]] | None = None,
    statistics: list[dict[str, Any]] | None = None,
    lineups: list[dict[str, Any]] | None = None,
    players: list[dict[str, Any]] | None = None,
    half_statistics: dict[str, dict[int | str, dict[str, MatchStat]]] | None = None,
    source_endpoints: list[str] | None = None,
    notes: list[str] | None = None,
) -> MatchData:
    if not fixture:
        return MatchData(
            fixture=None,
            fixture_id=None,
            time_scope=time_scope,
            selected_team_id=selected_team_id,
            selected_opponent_id=selected_opponent_id,
            source_endpoints=source_endpoints or [],
            notes=(notes or []) + ["fixture_not_found"],
        )
    fixture_obj = fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {}
    teams = fixture.get("teams") if isinstance(fixture.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    status = fixture_obj.get("status") if isinstance(fixture_obj.get("status"), dict) else {}
    stats_rows = statistics or []
    event_rows = events or []
    player_rows = players or []
    lifecycle = build_lifecycle(fixture)
    return MatchData(
        fixture=fixture,
        fixture_id=fixture_obj.get("id") if isinstance(fixture_obj.get("id"), int) else None,
        time_scope=time_scope,
        home_team_id=home.get("id") if isinstance(home.get("id"), int) else None,
        home_team_name=str(home.get("name") or ""),
        away_team_id=away.get("id") if isinstance(away.get("id"), int) else None,
        away_team_name=str(away.get("name") or ""),
        score=fixture_score(fixture),
        status=fixture_status(fixture_obj),
        status_short=str(status.get("short") or "").upper(),
        fixture_date=fixture_datetime(fixture),
        selected_team_id=selected_team_id,
        selected_opponent_id=selected_opponent_id,
        events=event_rows,
        statistics=stats_rows,
        lineups=lineups or [],
        players=player_rows,
        stats=normalize_match_statistics(stats_rows),
        lifecycle=lifecycle,
        normalized_events=normalize_match_events(event_rows),
        shootout=normalize_shootout(fixture, event_rows, live_entered_shootout=lifecycle.status_short == "P"),
        half_statistics=half_statistics or {},
        player_performances=normalize_fixture_players(player_rows),
        source_endpoints=source_endpoints or [],
        notes=notes or [],
    )


def _embedded_list(fixture: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if not isinstance(fixture, dict):
        return []
    value = fixture.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def compact_match_data(data: MatchData, *, stat_key: str | None = None) -> dict[str, Any]:
    selected_stats: dict[str, list[dict[str, Any]]] = {}
    for team_key, stats in data.stats.items():
        values = stats.values() if stat_key is None else [stats[stat_key]] if stat_key in stats else []
        rendered = [
            {
                "original_label": stat.original_label,
                "normalized_label": stat.normalized_label,
                "value": stat.display_value,
                "numeric_value": stat.numeric_value,
            }
            for stat in values
        ]
        if rendered:
            selected_stats[str(team_key)] = rendered
    return {
        "time_scope": data.time_scope,
        "fixture": {
            "fixture_id": data.fixture_id,
            "home_team_id": data.home_team_id,
            "home": data.home_team_name,
            "away_team_id": data.away_team_id,
            "away": data.away_team_name,
            "score": data.score,
            "status": data.status,
            "status_short": data.status_short,
            "date": data.fixture_date,
            "lifecycle": data.lifecycle.__dict__ if data.lifecycle is not None else None,
        },
        "selected_team_id": data.selected_team_id,
        "selected_opponent_id": data.selected_opponent_id,
        "statistics": selected_stats,
        "events": data.events[:20],
        "lineups": data.lineups[:4],
        "players": data.players[:4],
        "shootout": _compact_shootout(data.shootout),
        "player_performances": [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "player_id": item.player_id,
                "player_name": item.player_name,
                "metrics": item.metrics,
            }
            for item in data.player_performances[:20]
        ],
        "source_endpoints": data.source_endpoints,
        "notes": data.notes,
    }


def _compact_shootout(shootout: ShootoutSummary | None) -> dict[str, Any] | None:
    if shootout is None:
        return None
    return {
        "home_penalties": shootout.home_penalties,
        "away_penalties": shootout.away_penalties,
        "aggregate_available": shootout.aggregate_available,
        "attempts_available": shootout.attempts_available,
        "attempts_ambiguous": shootout.attempts_ambiguous,
        "attempts": [
            {
                "order": item.order,
                "team_id": item.team_id,
                "team_name": item.team_name,
                "player_id": item.player_id,
                "player_name": item.player_name,
                "scored": item.scored,
                "detail": item.detail,
            }
            for item in shootout.attempts
        ],
        "notes": shootout.notes,
    }


def _match_or_empty(
    fixtures: list[dict[str, Any]],
    time_scope: str,
    team_id: int | None,
    opponent_id: int | None,
    endpoints: list[str],
) -> MatchData:
    if fixtures:
        return build_match_data(fixtures[0], time_scope=time_scope, selected_team_id=team_id, selected_opponent_id=opponent_id, source_endpoints=endpoints)
    return build_match_data(None, time_scope=time_scope, selected_team_id=team_id, selected_opponent_id=opponent_id, source_endpoints=endpoints, notes=["fixture_team_mismatch"])


def _relative_date(today_iso: str | None, offset: int) -> str:
    base = date.today()
    if today_iso:
        try:
            base = date.fromisoformat(today_iso)
        except ValueError:
            pass
    return (base + timedelta(days=offset)).isoformat()


def _client_today_iso(client: Any) -> str:
    method = getattr(client, "today_iso", None)
    if callable(method):
        try:
            return str(method())
        except Exception:
            logging.debug("API-Football timezone date helper failed; falling back to host date")
    else:
        logging.debug("API-Football client has no today_iso; falling back to host date")
    return date.today().isoformat()
