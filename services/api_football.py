from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import aiohttp


class ApiFootballClient:
    _LEAGUE_DEFAULT_IDS: dict[str, int] = {
        "ligamx": 262,
        "premier": 39,
        "laliga": 140,
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
        "concacaf": [
            {"country": "World", "name": "CONCACAF Champions Cup"},
            {"country": "World", "name": "CONCACAF Champions League"},
            {"search": "CONCACAF Champions"},
            {"search": "Champions Cup"},
        ],
    }
    _LEAGUE_NAME_HINTS: dict[str, tuple[str, ...]] = {
        "ligamx": ("liga", "mx"),
        "premier": ("premier",),
        "laliga": ("liga",),
        "concacaf": ("concacaf", "champions"),
    }

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=30)
        self._session: aiohttp.ClientSession | None = None
        self._league_id_cache: dict[str, tuple[float, int]] = {}
        self._season_cache: dict[int, tuple[float, int]] = {}
        self._live_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}

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
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("API-Football key is not configured.")

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        headers = {"x-apisports-key": self.api_key}
        async with session.get(url, params=params or {}, headers=headers) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"API-Football error ({resp.status})")
            errors = self._extract_errors(payload)
            if errors:
                raise RuntimeError(errors)
            return self._extract_response(payload)

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
            raise RuntimeError("Unexpected API-Football response format.")
        response = payload.get("response")
        if response is None:
            return []
        if not isinstance(response, list):
            raise RuntimeError("Unexpected API-Football response format.")
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
            raise RuntimeError(f"Unsupported league key: {league_key}")
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
        league_id: int,
        cache_ttl_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._live_cache.get(league_id)
        if cached and now - cached[0] <= cache_ttl_seconds:
            return [dict(item) for item in cached[1]]

        rows = await self._request(
            "/fixtures",
            params={"league": league_id, "live": "all"},
        )
        self._live_cache[league_id] = (now, rows)
        return rows

    async def get_fixtures_on_date(
        self,
        *,
        league_id: int,
        season: int,
        date_iso: str,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "/fixtures",
            params={"league": league_id, "season": season, "date": date_iso},
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
        return await self._request("/fixtures", params=params)

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
        return await self._request("/fixtures", params=params)

    async def get_standings(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request(
            "/standings",
            params={"league": league_id, "season": season},
        )

    async def search_teams(
        self,
        *,
        name: str,
        league_id: int,
        season: int,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "/teams",
            params={
                "name": name,
                "league": league_id,
                "season": season,
            },
        )

    async def get_top_scorers(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        return await self._request(
            "/players/topscorers",
            params={"league": league_id, "season": season},
        )
