from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp


class FootballApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=30)
        self._session: aiohttp.ClientSession | None = None
        self._league_id_cache: dict[str, tuple[float, int]] = {}
        self._season_cache: dict[int, tuple[float, int]] = {}
        self._live_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._response_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, list[dict[str, Any]]]] = {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        cache_ttl_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise FootballApiError("API-Football key is not configured.")

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
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

    async def _resolve_league_id_from_api(self, league_key: str) -> int | None:
        profiles = self._LEAGUE_SEARCH_PROFILES.get(league_key, [])
        for params in profiles:
            rows = await self._request("/leagues", params=params)
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

        rows = await self._request(
            "/leagues",
            params={"id": league_id, "current": "true"},
        )
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

        rows = await self._request(
            "/fixtures",
            params={"league": league_id, "team": team_id, "live": "all"},
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
        return await self._request(
            "/fixtures",
            params={"league": league_id, "season": season, "team": team_id, "date": date_iso},
            cache_ttl_seconds=120,
        )

    async def get_next_fixtures(
        self,
        *,
        league_id: int,
        season: int,
        next_count: int,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "league": league_id,
            "season": season,
            "next": next_count,
        }
        if team_id is not None:
            params["team"] = team_id
        return await self._request("/fixtures", params=params, cache_ttl_seconds=180)

    async def get_last_fixtures(
        self,
        *,
        league_id: int,
        season: int,
        last_count: int,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "league": league_id,
            "season": season,
            "last": last_count,
        }
        if team_id is not None:
            params["team"] = team_id
        return await self._request("/fixtures", params=params, cache_ttl_seconds=3600)

    async def get_standings(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request(
            "/standings",
            params={"league": league_id, "season": season},
            cache_ttl_seconds=600,
        )

    async def search_teams(
        self,
        *,
        name: str,
        league_id: int | None = None,
        season: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "/teams",
            params={
                "name": name,
                "league": league_id,
                "season": season,
            },
            cache_ttl_seconds=21600,
        )

    async def get_top_scorers(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request(
            "/players/topscorers",
            params={"league": league_id, "season": season},
            cache_ttl_seconds=900,
        )

    async def get_fixture_by_id(self, *, fixture_id: int) -> list[dict[str, Any]]:
        return await self._request("/fixtures", params={"id": fixture_id}, cache_ttl_seconds=120)

    async def get_fixture_events(self, *, fixture_id: int) -> list[dict[str, Any]]:
        return await self._request("/fixtures/events", params={"fixture": fixture_id}, cache_ttl_seconds=60)

    async def get_fixture_lineups(self, *, fixture_id: int) -> list[dict[str, Any]]:
        return await self._request("/fixtures/lineups", params={"fixture": fixture_id}, cache_ttl_seconds=300)

    async def get_fixture_statistics(self, *, fixture_id: int) -> list[dict[str, Any]]:
        return await self._request("/fixtures/statistics", params={"fixture": fixture_id}, cache_ttl_seconds=60)

    async def get_fixture_players(self, *, fixture_id: int) -> list[dict[str, Any]]:
        return await self._request("/fixtures/players", params={"fixture": fixture_id}, cache_ttl_seconds=300)

    async def search_players(self, *, name: str, league_id: int | None = None, season: int | None = None, team_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": name, "league": league_id, "season": season, "team": team_id}
        return await self._request("/players", params=params, cache_ttl_seconds=21600)

    async def get_player_stats(self, *, player_id: int, season: int, league_id: int | None = None, team_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"id": player_id, "season": season, "league": league_id, "team": team_id}
        return await self._request("/players", params=params, cache_ttl_seconds=3600)

    async def get_top_assists(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request("/players/topassists", params={"league": league_id, "season": season}, cache_ttl_seconds=900)

    async def get_top_yellow_cards(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request("/players/topyellowcards", params={"league": league_id, "season": season}, cache_ttl_seconds=900)

    async def get_top_red_cards(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request("/players/topredcards", params={"league": league_id, "season": season}, cache_ttl_seconds=900)

    async def get_injuries(self, *, league_id: int | None = None, season: int | None = None, team_id: int | None = None, player_id: int | None = None, fixture_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"league": league_id, "season": season, "team": team_id, "player": player_id, "fixture": fixture_id}
        return await self._request("/injuries", params=params, cache_ttl_seconds=1800)

    async def get_transfers(self, *, team_id: int | None = None, player_id: int | None = None) -> list[dict[str, Any]]:
        return await self._request("/transfers", params={"team": team_id, "player": player_id}, cache_ttl_seconds=3600)

    async def get_head_to_head(self, *, team_a_id: int, team_b_id: int, last: int = 10) -> list[dict[str, Any]]:
        return await self._request("/fixtures/headtohead", params={"h2h": f"{team_a_id}-{team_b_id}", "last": last}, cache_ttl_seconds=3600)

    async def get_team_statistics(self, *, league_id: int, season: int, team_id: int) -> list[dict[str, Any]]:
        return await self._request("/teams/statistics", params={"league": league_id, "season": season, "team": team_id}, cache_ttl_seconds=900)
