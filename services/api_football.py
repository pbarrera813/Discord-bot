from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from services.football_api_request_compiler import (
    CoachSearchRequest,
    FixtureIdsRequest,
    FixturePlayersRequest,
    FixtureRoundsRequest,
    FixtureStatisticsRequest,
    InjuryRequest,
    InvalidFootballApiRequest,
    LeagueSearchRequest,
    MultiEntityRequest,
    OddsReferenceRequest,
    OddsRequest,
    PlayerProfileRequest,
    PlayerSeasonsRequest,
    PlayerSearchRequest,
    PlayerSquadsRequest,
    PlayerStatsRequest,
    PlayerTeamsRequest,
    PredictionRequest,
    TeamSearchRequest,
    TeamSeasonsRequest,
    TeamStatisticsRequest,
    TransferRequest,
    VenueSearchRequest,
    _make_country_slot,
    _make_league_slot,
    build_fixture_players_request,
    build_fixture_statistics_request,
    build_league_search_request,
    build_team_statistics_request,
    validate_positive_int,
)


class FootballApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class _CompiledApiFootballRequest:
    endpoint: str
    params: tuple[tuple[str, object], ...] = ()
    cache_ttl_seconds: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("/"):
            raise InvalidFootballApiRequest("compiled request endpoint is invalid.")
        if not isinstance(self.params, tuple):
            raise InvalidFootballApiRequest("compiled request params must be immutable.")
        if isinstance(self.cache_ttl_seconds, bool) or not isinstance(self.cache_ttl_seconds, int) or self.cache_ttl_seconds < 0:
            raise InvalidFootballApiRequest("compiled cache ttl is invalid.")
        for key, value in self.params:
            if not isinstance(key, str) or not key:
                raise InvalidFootballApiRequest("compiled request param key is invalid.")
            if value is None:
                continue
            if isinstance(value, bool):
                if key != "current":
                    raise InvalidFootballApiRequest(f"{key} must not be boolean.")
                continue
            if not isinstance(value, (int, str)):
                raise InvalidFootballApiRequest(f"{key} has an invalid compiled value.")


def _envelope(endpoint: str, params: dict[str, object | None] | None = None, *, cache_ttl_seconds: int = 0) -> _CompiledApiFootballRequest:
    clean = tuple((key, value) for key, value in (params or {}).items() if value is not None)
    return _CompiledApiFootballRequest(endpoint=endpoint, params=clean, cache_ttl_seconds=cache_ttl_seconds)


def _optional_positive_int(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    return validate_positive_int(value, label)


def _season_int(value: int) -> int:
    value = validate_positive_int(value, "season")
    if value < 1900 or value > 2100:
        raise InvalidFootballApiRequest("season is outside supported range.")
    return value


def _optional_season_int(value: int | None) -> int | None:
    return _season_int(value) if value is not None else None


def _season_required_for_league_scope(league_id: int | None, season: int | None, label: str) -> None:
    if league_id is not None and season is None:
        raise InvalidFootballApiRequest(f"{label} with league scope requires season.")


def _bounded_count(value: int, label: str, *, max_value: int) -> int:
    value = validate_positive_int(value, label)
    if value > max_value:
        raise InvalidFootballApiRequest(f"{label} is outside supported range.")
    return value


def _date_string(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFootballApiRequest("date must be YYYY-MM-DD.")
    cleaned = value.strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", cleaned):
        raise InvalidFootballApiRequest("date must be YYYY-MM-DD.")
    try:
        datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise InvalidFootballApiRequest("date is invalid.") from exc
    return cleaned




class ApiFootballClient:
    _LEAGUE_DEFAULT_IDS: dict[str, int] = {
        "ligamx": 262,
        "premier": 39,
        "laliga": 140,
        "champions": 2,
        "worldcup": 1,
        "expansionmx": 263,
    }
    _LEAGUE_SEARCH_PROFILES: dict[str, list[dict[str, Any]]] = {
        "ligamx": [
            {"country": "Mexico", "name": "Liga MX"},
            {"search": "Liga MX"},
        ],
        "premier": [
            {"country": "England", "name": "Premier League"},
            {"search": "Premier League"},
        ],
        "laliga": [
            {"country": "Spain", "name": "La Liga"},
            {"country": "Spain", "name": "Primera Division"},
            {"search": "La Liga"},
        ],
        "champions": [
            {"country": "World", "name": "UEFA Champions League"},
            {"search": "Champions League"},
        ],
        "concacaf": [
            {"country": "World", "name": "CONCACAF Champions Cup"},
            {"country": "World", "name": "CONCACAF Champions League"},
            {"search": "CONCACAF Champions"},
            {"search": "Champions Cup"},
        ],
        "worldcup": [
            {"country": "World", "name": "World Cup"},
            {"search": "World Cup"},
        ],
        "expansionmx": [
            {"country": "Mexico", "name": "Liga de Expansion MX"},
            {"country": "Mexico", "name": "Liga de Expansión MX"},
            {"search": "Liga de Expansion MX"},
            {"search": "Liga de Expansión"},
        ],
    }
    _LEAGUE_NAME_HINTS: dict[str, tuple[str, ...]] = {
        "ligamx": ("liga", "mx"),
        "premier": ("premier",),
        "laliga": ("liga",),
        "champions": ("champions",),
        "concacaf": ("concacaf", "champions"),
        "worldcup": ("world", "cup"),
        "expansionmx": ("expansi",),
    }

    def __init__(self, *, api_key: str, base_url: str = "https://v3.football.api-sports.io", timezone_name: str = "America/Mexico_City") -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timezone_name = timezone_name.strip() or "America/Mexico_City"
        self._display_timezone = self._local_timezone(self.timezone_name)
        self._timeout = aiohttp.ClientTimeout(total=30)
        self._session: aiohttp.ClientSession | None = None
        self._league_id_cache: dict[str, tuple[float, int]] = {}
        self._season_cache: dict[int, tuple[float, int]] = {}
        self._live_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._response_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, list[dict[str, Any]]]] = {}

    def today_iso(self) -> str:
        return datetime.now(self._display_timezone).date().isoformat()

    @staticmethod
    def _local_timezone(timezone_name: str) -> timezone:
        try:
            return ZoneInfo(timezone_name)  # type: ignore[return-value]
        except ZoneInfoNotFoundError:
            if timezone_name == "America/Mexico_City":
                return timezone(timedelta(hours=-6), "CST")
            logging.warning("Invalid API-Football timezone configured; falling back to CST")
            return timezone(timedelta(hours=-6), "CST")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        request: _CompiledApiFootballRequest,
    ) -> list[dict[str, Any]]:
        if not isinstance(request, _CompiledApiFootballRequest):
            raise InvalidFootballApiRequest("API-Football transport requires a compiled request envelope.")
        if not self.api_key:
            raise FootballApiError("API-Football key is not configured.")

        endpoint = request.endpoint
        clean_params = {key: value for key, value in request.params if value is not None}
        cache_ttl_seconds = request.cache_ttl_seconds
        cache_key = self._cache_key(endpoint, clean_params)
        now = time.monotonic()
        if cache_ttl_seconds > 0:
            cached = self._response_cache.get(cache_key)
            if cached and now - cached[0] <= cache_ttl_seconds:
                logging.info(
                    "API-Football request endpoint=%s params=%s response_count=%s cache_hit=true",
                    endpoint,
                    self._safe_log_params(clean_params),
                    len(cached[1]),
                )
                return [dict(item) for item in cached[1]]

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        headers = {"x-apisports-key": self.api_key}
        last_error: FootballApiError | None = None
        for attempt in range(3):
            try:
                async with session.get(url, params=clean_params, headers=headers) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status >= 400:
                        retryable = resp.status == 429 or resp.status >= 500
                        raise FootballApiError(f"API-Football error ({resp.status})", status=resp.status, retryable=retryable)
                    errors = self._extract_errors(payload)
                    if errors:
                        raise FootballApiError(errors)
                    rows = self._extract_response(payload)
                    logging.info(
                        "API-Football request endpoint=%s params=%s response_count=%s cache_hit=false",
                        endpoint,
                        self._safe_log_params(clean_params),
                        len(rows),
                    )
                    if cache_ttl_seconds > 0:
                        self._response_cache[cache_key] = (time.monotonic(), rows)
                    return rows
            except FootballApiError as exc:
                last_error = exc
                if not exc.retryable or attempt >= 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = FootballApiError("API-Football request failed.", retryable=True)
                logging.warning("API-Football transient request failure endpoint=%s attempt=%s error=%s", endpoint, attempt + 1, type(exc).__name__)
                if attempt >= 2:
                    raise last_error from exc
                await asyncio.sleep(0.25 * (attempt + 1))
        raise last_error or FootballApiError("API-Football request failed.")

    @staticmethod
    def _cache_key(endpoint: str, params: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return endpoint, tuple(sorted((str(key), str(value)) for key, value in params.items()))

    @staticmethod
    def _safe_log_params(params: dict[str, Any]) -> dict[str, str]:
        safe: dict[str, str] = {}
        for key, value in params.items():
            key_text = str(key)
            if "key" in key_text.casefold() or "token" in key_text.casefold():
                continue
            safe[key_text] = str(value)[:80]
        return safe

    @staticmethod
    def _extract_errors(payload: Any) -> str:
        if not isinstance(payload, dict):
            return "Unexpected API-Football payload."
        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts = []
            for key, value in errors.items():
                if isinstance(value, str) and value.strip():
                    parts.append(f"{key}: {value.strip()}")
                elif value:
                    parts.append(f"{key}: {value}")
            return " | ".join(parts)
        if isinstance(errors, list):
            return " | ".join(str(item) for item in errors if item)
        if isinstance(errors, str):
            return errors.strip()
        return ""

    @staticmethod
    def _extract_response(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise FootballApiError("Unexpected API-Football response format.")
        response = payload.get("response")
        if response is None:
            return []
        if isinstance(response, dict):
            return [response]
        if not isinstance(response, list):
            raise FootballApiError("Unexpected API-Football response format.")
        items: list[dict[str, Any]] = []
        for item in response:
            if isinstance(item, dict):
                items.append(item)
            elif isinstance(item, (int, str)):
                items.append({"value": item})
        return items

    async def resolve_league_id(
        self,
        league_key: str,
    ) -> int:
        key = str(league_key or "").strip().casefold()

        now = time.monotonic()
        cached = self._league_id_cache.get(key)
        if cached and now - cached[0] <= 21600:
            return cached[1]

        selected = await self._resolve_league_id_from_api(key)
        if selected is None:
            selected = self._LEAGUE_DEFAULT_IDS.get(key)
        if not isinstance(selected, int):
            raise FootballApiError(f"Unsupported league key: {league_key}")
        self._league_id_cache[key] = (now, selected)
        return selected

    async def search_leagues(
        self,
        request: LeagueSearchRequest,
    ) -> list[dict[str, Any]]:
        if not isinstance(request, LeagueSearchRequest):
            raise InvalidFootballApiRequest("search_leagues requires a compiled league request.")
        params: dict[str, object | None] = {
            "name": request.name.value if request.name is not None else None,
            "country": request.country.value if request.country is not None else None,
            "search": request.search.value if request.search is not None else None,
        }
        if request.current is not None:
            params["current"] = "true" if request.current else "false"
        return await self._request(_envelope("/leagues", params, cache_ttl_seconds=21600))

    async def get_league_by_id(self, *, league_id: int, season: int | None = None) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _optional_season_int(season)
        return await self._request(_envelope("/leagues", {"id": league_id, "season": season}, cache_ttl_seconds=3600))

    async def _resolve_league_id_from_api(self, league_key: str) -> int | None:
        profiles = self._LEAGUE_SEARCH_PROFILES.get(league_key, [])
        for params in profiles:
            league_candidate = _make_league_slot(params.get("name") or params.get("search") or league_key, source="alias")
            country_candidate = _make_country_slot(params["country"], source="alias") if params.get("country") else None
            rows = await self.search_leagues(
                build_league_search_request(
                    league_candidate if params.get("name") else None,
                    country=country_candidate,
                    search=league_candidate if params.get("search") else None,
                )
            )
            picked = self._pick_league_id(rows, league_key)
            if isinstance(picked, int):
                return picked
        return None

    @classmethod
    def _pick_league_id(cls, leagues: list[dict[str, Any]], league_key: str) -> int | None:
        hints = cls._LEAGUE_NAME_HINTS.get(league_key, ())
        for item in leagues:
            league = item.get("league")
            if not isinstance(league, dict):
                continue
            league_id = league.get("id")
            name = str(league.get("name", "")).strip().casefold()
            league_type = str(league.get("type", "")).strip().casefold()
            if not isinstance(league_id, int):
                continue
            if league_type not in ("league", "cup"):
                continue
            if all(hint in name for hint in hints):
                return league_id
        for item in leagues:
            league = item.get("league")
            if not isinstance(league, dict):
                continue
            league_id = league.get("id")
            if isinstance(league_id, int):
                return league_id
        return None

    @classmethod
    def _pick_ligamx_league_id(cls, leagues: list[dict[str, Any]]) -> int | None:
        return cls._pick_league_id(leagues, "ligamx")

    async def get_current_season(self, league_id: int) -> int:
        now = time.monotonic()
        cached = self._season_cache.get(league_id)
        if cached and now - cached[0] <= 21600:
            return cached[1]

        league_id = validate_positive_int(league_id, "league_id")
        rows = await self._request(_envelope("/leagues", {"id": league_id, "current": "true"}))
        season = self._extract_current_season(rows)
        if season is None:
            season = datetime.now(timezone.utc).year
        self._season_cache[league_id] = (now, season)
        return season

    @staticmethod
    def _extract_current_season(rows: list[dict[str, Any]]) -> int | None:
        for item in rows:
            seasons = item.get("seasons")
            if not isinstance(seasons, list):
                continue
            for season in seasons:
                if not isinstance(season, dict):
                    continue
                if season.get("current") is True:
                    year = season.get("year")
                    if isinstance(year, int):
                        return year
        for item in rows:
            seasons = item.get("seasons")
            if not isinstance(seasons, list) or not seasons:
                continue
            year = seasons[-1].get("year") if isinstance(seasons[-1], dict) else None
            if isinstance(year, int):
                return year
        return None

    async def get_live_fixtures(
        self,
        *,
        league_id: int | None = None,
        team_id: int | None = None,
        cache_ttl_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        cache_id = league_id or -(team_id or 1)
        cached = self._live_cache.get(cache_id)
        if cached and now - cached[0] <= cache_ttl_seconds:
            return [dict(item) for item in cached[1]]

        league_id = _optional_positive_int(league_id, "league_id")
        team_id = _optional_positive_int(team_id, "team_id")
        rows = await self._request(
            _envelope("/fixtures", {"league": league_id, "team": team_id, "live": "all", "timezone": self.timezone_name})
        )
        self._live_cache[cache_id] = (now, rows)
        return rows

    async def get_fixtures_on_date(
        self,
        *,
        league_id: int | None = None,
        season: int | None = None,
        date_iso: str,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        league_id = _optional_positive_int(league_id, "league_id")
        season = _optional_season_int(season)
        _season_required_for_league_scope(league_id, season, "fixture date lookup")
        team_id = _optional_positive_int(team_id, "team_id")
        date_iso = _date_string(date_iso)
        return await self._request(
            _envelope(
                "/fixtures",
                {"league": league_id, "season": season, "team": team_id, "date": date_iso, "timezone": self.timezone_name},
                cache_ttl_seconds=120,
            )
        )

    async def get_next_fixtures(
        self,
        *,
        league_id: int | None = None,
        season: int | None = None,
        next_count: int,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        league_id = _optional_positive_int(league_id, "league_id")
        season = _optional_season_int(season)
        _season_required_for_league_scope(league_id, season, "next fixtures lookup")
        team_id = _optional_positive_int(team_id, "team_id")
        next_count = _bounded_count(next_count, "next_count", max_value=50)
        params: dict[str, object | None] = {
            "league": league_id,
            "season": season,
            "next": next_count,
            "timezone": self.timezone_name,
        }
        if team_id is not None:
            params["team"] = team_id
        return await self._request(_envelope("/fixtures", params, cache_ttl_seconds=180))

    async def get_last_fixtures(
        self,
        *,
        league_id: int | None = None,
        season: int | None = None,
        last_count: int,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        league_id = _optional_positive_int(league_id, "league_id")
        season = _optional_season_int(season)
        _season_required_for_league_scope(league_id, season, "last fixtures lookup")
        team_id = _optional_positive_int(team_id, "team_id")
        last_count = _bounded_count(last_count, "last_count", max_value=50)
        params: dict[str, object | None] = {
            "league": league_id,
            "season": season,
            "last": last_count,
            "timezone": self.timezone_name,
        }
        if team_id is not None:
            params["team"] = team_id
        return await self._request(_envelope("/fixtures", params, cache_ttl_seconds=3600))

    async def get_standings(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _season_int(season)
        return await self._request(_envelope("/standings", {"league": league_id, "season": season}, cache_ttl_seconds=600))

    async def search_teams(
        self,
        request: TeamSearchRequest,
    ) -> list[dict[str, Any]]:
        if not isinstance(request, TeamSearchRequest):
            raise InvalidFootballApiRequest("search_teams requires a compiled team request.")
        return await self._request(
            _envelope(
                "/teams",
                {
                    "name": request.name.value if request.name is not None else None,
                    "search": request.search.value if request.search is not None else None,
                    "league": request.league_id,
                    "season": request.season.value if request.season is not None else None,
                },
                cache_ttl_seconds=21600,
            )
        )

    async def get_top_scorers(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _season_int(season)
        return await self._request(_envelope("/players/topscorers", {"league": league_id, "season": season}, cache_ttl_seconds=900))

    async def get_fixture_by_id(self, *, fixture_id: int) -> list[dict[str, Any]]:
        fixture_id = validate_positive_int(fixture_id, "fixture_id")
        return await self._request(_envelope("/fixtures", {"id": fixture_id, "timezone": self.timezone_name}, cache_ttl_seconds=120))

    async def get_fixtures_by_ids(self, request: FixtureIdsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, FixtureIdsRequest):
            raise InvalidFootballApiRequest("get_fixtures_by_ids requires a compiled fixture IDs request.")
        return await self._request(
            _envelope(
                "/fixtures",
                {"ids": "-".join(str(item) for item in request.fixture_ids), "timezone": self.timezone_name},
                cache_ttl_seconds=60,
            )
        )

    async def get_fixture_events(self, *, fixture_id: int) -> list[dict[str, Any]]:
        fixture_id = validate_positive_int(fixture_id, "fixture_id")
        return await self._request(_envelope("/fixtures/events", {"fixture": fixture_id}, cache_ttl_seconds=60))

    async def get_fixture_lineups(self, *, fixture_id: int) -> list[dict[str, Any]]:
        fixture_id = validate_positive_int(fixture_id, "fixture_id")
        return await self._request(_envelope("/fixtures/lineups", {"fixture": fixture_id}, cache_ttl_seconds=300))

    async def get_fixture_statistics(
        self,
        *,
        fixture_id: int | None = None,
        request: FixtureStatisticsRequest | None = None,
        half: bool = False,
        team_id: int | None = None,
        stat_type: str | None = None,
    ) -> list[dict[str, Any]]:
        compiled = request if request is not None else build_fixture_statistics_request(fixture_id=fixture_id, half=half, team_id=team_id, stat_type=stat_type)  # type: ignore[arg-type]
        if not isinstance(compiled, FixtureStatisticsRequest):
            raise InvalidFootballApiRequest("get_fixture_statistics requires a compiled fixture statistics request.")
        return await self._request(
            _envelope(
                "/fixtures/statistics",
                {
                    "fixture": compiled.fixture_id,
                    "team": compiled.team_id,
                    "type": compiled.stat_type,
                    "half": "true" if compiled.half else None,
                },
                cache_ttl_seconds=60,
            )
        )

    async def get_fixture_players(
        self,
        *,
        fixture_id: int | None = None,
        request: FixturePlayersRequest | None = None,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        compiled = request if request is not None else build_fixture_players_request(fixture_id=fixture_id, team_id=team_id)  # type: ignore[arg-type]
        if not isinstance(compiled, FixturePlayersRequest):
            raise InvalidFootballApiRequest("get_fixture_players requires a compiled fixture players request.")
        return await self._request(
            _envelope("/fixtures/players", {"fixture": compiled.fixture_id, "team": compiled.team_id}, cache_ttl_seconds=300)
        )

    async def search_players(self, request: PlayerSearchRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerSearchRequest):
            raise InvalidFootballApiRequest("search_players requires a compiled player request.")
        params: dict[str, object | None] = {
            "search": request.name.value,
            "league": request.league_id,
            "season": request.season.value if request.season is not None else None,
            "team": request.team_id,
        }
        return await self._request(_envelope("/players", params, cache_ttl_seconds=21600))

    async def search_player_profiles(self, request: PlayerProfileRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerProfileRequest):
            raise InvalidFootballApiRequest("search_player_profiles requires a compiled profile request.")
        return await self._request(_envelope("/players/profiles", {"search": request.lastname.value}, cache_ttl_seconds=21600))

    async def get_player_stats(self, request: PlayerStatsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerStatsRequest):
            raise InvalidFootballApiRequest("get_player_stats requires a compiled player stats request.")
        params: dict[str, object | None] = {
            "id": request.player_id,
            "season": request.season.value,
            "league": request.league_id,
            "team": request.team_id,
        }
        return await self._request(_envelope("/players", params, cache_ttl_seconds=3600))

    async def get_player_seasons(self, request: PlayerSeasonsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerSeasonsRequest):
            raise InvalidFootballApiRequest("get_player_seasons requires a compiled player seasons request.")
        rows = await self._request(_envelope("/players/seasons", {"player": request.player_id}, cache_ttl_seconds=21600))
        normalized: list[dict[str, Any]] = []
        for row in rows:
            season = row.get("season") if isinstance(row, dict) else None
            if not isinstance(season, int) and isinstance(row, dict):
                season = row.get("value")
            if isinstance(season, int):
                normalized.append({"season": season})
        return normalized

    async def get_player_squads(self, request: PlayerSquadsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerSquadsRequest):
            raise InvalidFootballApiRequest("get_player_squads requires a compiled squad request.")
        return await self._request(_envelope("/players/squads", {"player": request.player_id, "team": request.team_id}, cache_ttl_seconds=21600))

    async def get_player_teams(self, request: PlayerTeamsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PlayerTeamsRequest):
            raise InvalidFootballApiRequest("get_player_teams requires a compiled player teams request.")
        return await self._request(_envelope("/players/teams", {"player": request.player_id}, cache_ttl_seconds=21600))

    async def get_top_assists(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _season_int(season)
        return await self._request(_envelope("/players/topassists", {"league": league_id, "season": season}, cache_ttl_seconds=900))

    async def get_top_yellow_cards(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _season_int(season)
        return await self._request(_envelope("/players/topyellowcards", {"league": league_id, "season": season}, cache_ttl_seconds=900))

    async def get_top_red_cards(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        league_id = validate_positive_int(league_id, "league_id")
        season = _season_int(season)
        return await self._request(_envelope("/players/topredcards", {"league": league_id, "season": season}, cache_ttl_seconds=900))

    async def get_injuries(self, request: InjuryRequest) -> list[dict[str, Any]]:
        if not isinstance(request, InjuryRequest):
            raise InvalidFootballApiRequest("get_injuries requires a compiled injury request.")
        params: dict[str, object | None] = {
            "league": request.league_id,
            "season": request.season.value if request.season is not None else None,
            "team": request.team_id,
            "player": request.player_id,
            "fixture": request.fixture_id,
        }
        return await self._request(_envelope("/injuries", params, cache_ttl_seconds=1800))

    async def get_transfers(self, request: TransferRequest) -> list[dict[str, Any]]:
        if not isinstance(request, TransferRequest):
            raise InvalidFootballApiRequest("get_transfers requires a compiled transfer request.")
        return await self._request(_envelope("/transfers", {"team": request.team_id, "player": request.player_id}, cache_ttl_seconds=3600))

    async def get_head_to_head(self, *, team_a_id: int, team_b_id: int, last: int = 10) -> list[dict[str, Any]]:
        team_a_id = validate_positive_int(team_a_id, "team_a_id")
        team_b_id = validate_positive_int(team_b_id, "team_b_id")
        last = _bounded_count(last, "last", max_value=50)
        return await self._request(
            _envelope(
                "/fixtures/headtohead",
                {"h2h": f"{team_a_id}-{team_b_id}", "last": last, "timezone": self.timezone_name},
                cache_ttl_seconds=3600,
            )
        )

    async def get_team_statistics(
        self,
        *,
        league_id: int | None = None,
        season: int | None = None,
        team_id: int | None = None,
        date_iso: str | None = None,
        request: TeamStatisticsRequest | None = None,
    ) -> list[dict[str, Any]]:
        compiled = request if request is not None else build_team_statistics_request(league_id=league_id, season=season, team_id=team_id, date_iso=date_iso)  # type: ignore[arg-type]
        if not isinstance(compiled, TeamStatisticsRequest):
            raise InvalidFootballApiRequest("get_team_statistics requires a compiled team statistics request.")
        return await self._request(
            _envelope(
                "/teams/statistics",
                {
                    "league": compiled.league_id,
                    "season": compiled.season.value,
                    "team": compiled.team_id,
                    "date": compiled.date.value if compiled.date is not None else None,
                },
                cache_ttl_seconds=900,
            )
        )

    async def get_fixture_rounds(self, request: FixtureRoundsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, FixtureRoundsRequest):
            raise InvalidFootballApiRequest("get_fixture_rounds requires a compiled rounds request.")
        return await self._request(
            _envelope(
                "/fixtures/rounds",
                {
                    "league": request.league_id,
                    "season": request.season.value,
                    "current": "true" if request.current is True else "false" if request.current is False else None,
                    "dates": "true" if request.include_dates else None,
                    "timezone": self.timezone_name,
                },
                cache_ttl_seconds=21600,
            )
        )

    async def get_team_seasons(self, request: TeamSeasonsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, TeamSeasonsRequest):
            raise InvalidFootballApiRequest("get_team_seasons requires a compiled team seasons request.")
        rows = await self._request(_envelope("/teams/seasons", {"team": request.team_id}, cache_ttl_seconds=21600))
        normalized: list[dict[str, Any]] = []
        for row in rows:
            season = row.get("season") if isinstance(row, dict) else None
            if not isinstance(season, int) and isinstance(row, dict):
                season = row.get("value")
            if isinstance(season, int):
                normalized.append({"season": season})
        return normalized

    async def get_team_countries(self) -> list[dict[str, Any]]:
        return await self._request(_envelope("/teams/countries", cache_ttl_seconds=86400))

    async def search_venues(self, request: VenueSearchRequest) -> list[dict[str, Any]]:
        if not isinstance(request, VenueSearchRequest):
            raise InvalidFootballApiRequest("search_venues requires a compiled venue request.")
        return await self._request(
            _envelope(
                "/venues",
                {
                    "name": request.name.value if request.name is not None else None,
                    "search": request.search.value if request.search is not None else None,
                    "city": request.city.value if request.city is not None else None,
                    "country": request.country.value if request.country is not None else None,
                },
                cache_ttl_seconds=21600,
            )
        )

    async def search_coaches(self, request: CoachSearchRequest) -> list[dict[str, Any]]:
        if not isinstance(request, CoachSearchRequest):
            raise InvalidFootballApiRequest("search_coaches requires a compiled coach request.")
        return await self._request(
            _envelope(
                "/coachs",
                {"id": request.coach_id, "team": request.team_id, "search": request.search.value if request.search is not None else None},
                cache_ttl_seconds=21600,
            )
        )

    async def get_trophies(self, request: MultiEntityRequest) -> list[dict[str, Any]]:
        if not isinstance(request, MultiEntityRequest):
            raise InvalidFootballApiRequest("get_trophies requires a compiled trophy request.")
        return await self._request(
            _envelope(
                "/trophies",
                {
                    "player": request.player_id,
                    "coach": request.coach_id,
                    "players": "-".join(str(item) for item in request.player_ids) if request.player_ids else None,
                    "coachs": "-".join(str(item) for item in request.coach_ids) if request.coach_ids else None,
                },
                cache_ttl_seconds=21600,
            )
        )

    async def get_sidelined(self, request: MultiEntityRequest) -> list[dict[str, Any]]:
        if not isinstance(request, MultiEntityRequest):
            raise InvalidFootballApiRequest("get_sidelined requires a compiled sidelined request.")
        return await self._request(
            _envelope(
                "/sidelined",
                {
                    "player": request.player_id,
                    "coach": request.coach_id,
                    "players": "-".join(str(item) for item in request.player_ids) if request.player_ids else None,
                    "coachs": "-".join(str(item) for item in request.coach_ids) if request.coach_ids else None,
                },
                cache_ttl_seconds=21600,
            )
        )

    async def get_predictions(self, request: PredictionRequest) -> list[dict[str, Any]]:
        if not isinstance(request, PredictionRequest):
            raise InvalidFootballApiRequest("get_predictions requires a compiled prediction request.")
        return await self._request(_envelope("/predictions", {"fixture": request.fixture_id}, cache_ttl_seconds=3600))

    async def get_odds(self, request: OddsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, OddsRequest) or request.live:
            raise InvalidFootballApiRequest("get_odds requires a compiled pre-match odds request.")
        return await self._request(
            _envelope(
                "/odds",
                {
                    "fixture": request.fixture_id,
                    "league": request.league_id,
                    "season": request.season.value if request.season is not None else None,
                    "date": request.date.value if request.date is not None else None,
                    "bookmaker": request.bookmaker_id,
                    "bet": request.bet_id,
                    "timezone": self.timezone_name,
                },
                cache_ttl_seconds=10800,
            )
        )

    async def get_live_odds(self, request: OddsRequest) -> list[dict[str, Any]]:
        if not isinstance(request, OddsRequest) or not request.live:
            raise InvalidFootballApiRequest("get_live_odds requires a compiled live odds request.")
        return await self._request(
            _envelope("/odds/live", {"fixture": request.fixture_id, "league": request.league_id, "bet": request.bet_id}, cache_ttl_seconds=30)
        )

    async def get_odds_bookmakers(self, request: OddsReferenceRequest) -> list[dict[str, Any]]:
        if not isinstance(request, OddsReferenceRequest):
            raise InvalidFootballApiRequest("get_odds_bookmakers requires a compiled odds reference request.")
        return await self._request(
            _envelope("/odds/bookmakers", {"id": request.item_id, "search": request.search.value if request.search is not None else None}, cache_ttl_seconds=86400)
        )

    async def get_odds_bets(self, request: OddsReferenceRequest) -> list[dict[str, Any]]:
        if not isinstance(request, OddsReferenceRequest):
            raise InvalidFootballApiRequest("get_odds_bets requires a compiled odds reference request.")
        return await self._request(
            _envelope("/odds/bets", {"id": request.item_id, "search": request.search.value if request.search is not None else None}, cache_ttl_seconds=86400)
        )

    async def get_live_odds_bets(self, request: OddsReferenceRequest) -> list[dict[str, Any]]:
        if not isinstance(request, OddsReferenceRequest):
            raise InvalidFootballApiRequest("get_live_odds_bets requires a compiled odds reference request.")
        return await self._request(
            _envelope("/odds/live/bets", {"id": request.item_id, "search": request.search.value if request.search is not None else None}, cache_ttl_seconds=86400)
        )
