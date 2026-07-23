from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final

from services.api_football import FootballApiError


LEAGUE_ALIASES: Final[dict[str, str]] = {
    "ligamx": "ligamx",
    "ligabbvamx": "ligamx",
    "liga": "ligamx",
    "premier": "premier",
    "premierleague": "premier",
    "epl": "premier",
    "laliga": "laliga",
    "laligasantander": "laliga",
    "champions": "champions",
    "championsleague": "champions",
    "ucl": "champions",
    "concacaf": "concacaf",
    "concacafchampions": "concacaf",
    "concacafchampionscup": "concacaf",
    "concacafchampionsleague": "concacaf",
    "worldcup": "worldcup",
    "fifaworldcup": "worldcup",
    "mundial": "worldcup",
    "copamundial": "worldcup",
    "copamundialfifa": "worldcup",
    "copadelmundo": "worldcup",
    "copamundo": "worldcup",
    "worldcupfinal": "worldcup",
    "expansionmx": "expansionmx",
    "ligaexpansionmx": "expansionmx",
    "ligadeexpansionmx": "expansionmx",
    "ligaexpansion": "expansionmx",
    "ligadeexpansion": "expansionmx",
}

LEAGUE_LABELS: Final[dict[str, tuple[str, str]]] = {
    "ligamx": ("Liga BBVA MX", "Liga BBVA MX"),
    "premier": ("Premier League", "Premier League"),
    "laliga": ("LaLiga", "LaLiga"),
    "champions": ("UEFA Champions League", "UEFA Champions League"),
    "concacaf": ("CONCACAF Champions Cup", "Copa de Campeones CONCACAF"),
    "worldcup": ("FIFA World Cup", "Copa Mundial FIFA"),
    "expansionmx": ("Liga de Expansion MX", "Liga de Expansion MX"),
}

TEAM_ALIASES: Final[dict[str, str]] = {
    "america": "Club America",
    "clubamerica": "Club America",
    "barca": "Barcelona",
    "barcelona": "Barcelona",
    "pumas": "Pumas UNAM",
    "pumasunam": "Pumas UNAM",
    "universidadnacional": "Pumas UNAM",
    "psg": "Paris Saint Germain",
    "paris": "Paris Saint Germain",
    "mexico": "Mexico",
    "mexico national team": "Mexico",
    "francia": "France",
    "france": "France",
    "jaibabrava": "Tampico Madero",
    "tampicomadero": "Tampico Madero",
    "irapuato": "Irapuato",
    "irapuatofc": "Irapuato",
}

DEFAULT_PLAYER_SCOPE_LEAGUES: Final[tuple[str, ...]] = ("worldcup", "premier", "laliga", "champions", "ligamx")

# Small bootstrap list for very common nicknames. The general path is API search
# plus optional LLM canonicalizer fallback plus API validation.
PLAYER_SEED_ALIASES: Final[dict[str, str]] = {
    "dibu": "Emiliano Martinez",
    "dibumtz": "Emiliano Martinez",
    "dibumartinez": "Emiliano Martinez",
    "emilianomartinez": "Emiliano Martinez",
}

PLAYER_STAT_FOCUS_TERMS: Final[dict[str, str]] = {
    "penal": "penalties",
    "penales": "penalties",
    "penalty": "penalties",
    "penalties": "penalties",
}

PLAYER_QUERY_STOP_WORDS: Final[set[str]] = {
    "actual",
    "actuales",
    "age",
    "ano",
    "anos",
    "año",
    "años",
    "cuantos",
    "cuántos",
    "edad",
    "este",
    "futbol",
    "fútbol",
    "football",
    "goals",
    "goal",
    "gol",
    "goles",
    "jugador",
    "lleva",
    "mundial",
    "tendra",
    "tendrá",
    "tiene",
    "world",
    "cup",
    "sabes",
    "si",
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "es",
    "esta",
    "está",
    "bueno",
    "buena",
    "en",
    "de",
    "del",
    "para",
    "como",
    "qué",
    "que",
    "tal",
    "stats",
    "stat",
    "statistics",
    "performance",
    "rendimiento",
    "datos",
    "data",
    "penal",
    "penales",
    "penalty",
    "penalties",
}


@dataclass(frozen=True)
class FootballResolution:
    selected: dict[str, Any] | None
    matches: tuple[dict[str, Any], ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True)
class PlayerQuery:
    raw: str
    candidates: tuple[str, ...]
    stat_focus: str | None = None


@dataclass(frozen=True)
class FootballPlayerLookup:
    query: PlayerQuery
    resolution: FootballResolution
    rows: tuple[dict[str, Any], ...]
    searches: tuple[dict[str, Any], ...]
    canonicalizer_used: bool = False


FootballPlayerCanonicalizer = Callable[[PlayerQuery], Awaitable[dict[str, Any] | None]]


def normalize_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def normalize_league_key(raw: str | None) -> str | None:
    if not raw:
        return None
    return LEAGUE_ALIASES.get(normalize_key(raw))


def league_label(league_key: str, lang: str = "en") -> str:
    en, es = LEAGUE_LABELS.get(league_key, (league_key, league_key))
    return es if str(lang).startswith("es") else en


def canonical_team_query(query: str) -> str:
    return TEAM_ALIASES.get(normalize_key(query), query.strip())


def canonical_player_query(query: str) -> str:
    return PLAYER_SEED_ALIASES.get(normalize_key(query), query.strip())


def parse_player_query(query: str) -> PlayerQuery:
    raw = str(query or "").strip()
    words = _ascii_words(raw)
    stat_focus = next((PLAYER_STAT_FOCUS_TERMS[word] for word in words if word in PLAYER_STAT_FOCUS_TERMS), None)
    filtered_words = [word for word in words if word not in PLAYER_QUERY_STOP_WORDS and not word.isdigit()]
    filtered = " ".join(filtered_words).strip()

    candidates: list[str] = []
    normalized_raw = normalize_key(raw)
    normalized_filtered = normalize_key(filtered)
    for alias, canonical in PLAYER_SEED_ALIASES.items():
        if alias and (alias in normalized_raw or alias in normalized_filtered):
            candidates.append(canonical)

    if filtered:
        candidates.append(PLAYER_SEED_ALIASES.get(normalize_key(filtered), filtered))
    canonical = canonical_player_query(raw)
    if canonical and canonical != raw:
        candidates.append(canonical)
    if len(filtered_words) >= 2:
        candidates.append(filtered_words[-1])

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates or [raw]:
        cleaned = " ".join(str(candidate).split())
        key = normalize_key(cleaned)
        if cleaned and key and key not in seen:
            deduped.append(cleaned)
            seen.add(key)
    return PlayerQuery(raw=raw, candidates=tuple(deduped), stat_focus=stat_focus)


async def resolve_player(
    client: Any,
    query: str,
    *,
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    explicit_context: bool = False,
    canonicalizer: FootballPlayerCanonicalizer | None = None,
    alias_cache: dict[str, dict[str, Any]] | None = None,
    cache_ttl_seconds: int = 86400,
    candidate_names: list[str] | tuple[str, ...] | None = None,
    stat_focus: str | None = None,
) -> FootballPlayerLookup:
    parsed = parse_player_query(query)
    if stat_focus and not parsed.stat_focus:
        parsed = PlayerQuery(raw=parsed.raw, candidates=parsed.candidates, stat_focus=stat_focus)
    cached = _cached_alias_candidate(parsed, alias_cache)
    candidates = _dedupe_candidates([cached] if cached else [], candidate_names or (), parsed.candidates)
    rows: list[dict[str, Any]] = []
    searches: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    await _search_player_candidates(
        client,
        candidates,
        rows=rows,
        searches=searches,
        seen_ids=seen_ids,
        league_id=league_id,
        season=season,
        team_id=team_id,
        explicit_context=explicit_context,
        stage="api_search",
    )

    resolution = pick_player(rows, candidates[0] if candidates else query)
    canonicalizer_used = False
    if canonicalizer is not None and _should_use_canonicalizer(parsed, resolution):
        suggestion = _normalize_canonicalizer_result(await canonicalizer(parsed))
        suggested_candidates = _dedupe_candidates(suggestion.get("candidate_names", ()) if suggestion else ())
        if suggested_candidates:
            canonicalizer_used = True
            validated_rows: list[dict[str, Any]] = []
            validated_searches: list[dict[str, Any]] = []
            validated_seen_ids: set[int] = set()
            await _search_player_candidates(
                client,
                suggested_candidates,
                rows=validated_rows,
                searches=validated_searches,
                seen_ids=validated_seen_ids,
                league_id=league_id,
                season=season,
                team_id=team_id,
                explicit_context=explicit_context,
                stage="api_validation",
            )
            validated_resolution = pick_player(validated_rows, suggested_candidates[0])
            searches.extend(validated_searches)
            if validated_rows:
                rows = validated_rows
                resolution = validated_resolution
            if validated_resolution.selected is not None:
                _store_validated_alias(
                    parsed,
                    validated_resolution.selected,
                    alias_cache,
                    confidence=float(suggestion.get("confidence", 0.0)),
                    ttl_seconds=cache_ttl_seconds,
                )

    return FootballPlayerLookup(
        query=parsed,
        resolution=resolution,
        rows=tuple(rows),
        searches=tuple(searches),
        canonicalizer_used=canonicalizer_used,
    )


def pick_team(teams: list[dict[str, Any]], query: str) -> FootballResolution:
    return _pick_entity(teams, query, key="team")


def pick_player(players: list[dict[str, Any]], query: str) -> FootballResolution:
    return _pick_entity(players, query, key="player")


def _ascii_words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.casefold())


async def _search_player_candidates(
    client: Any,
    candidates: tuple[str, ...],
    *,
    rows: list[dict[str, Any]],
    searches: list[dict[str, Any]],
    seen_ids: set[int],
    league_id: int | None,
    season: int | None,
    team_id: int | None,
    explicit_context: bool,
    stage: str,
) -> None:
    scopes = await _player_search_scopes(
        client,
        league_id=league_id,
        season=season,
        team_id=team_id,
        explicit_context=explicit_context,
    )
    for candidate in candidates:
        for scope in scopes:
            scoped_league_id, scoped_season, scoped_team_id = scope
            params = {
                "name": candidate,
                "league_id": scoped_league_id,
                "season": scoped_season,
                "team_id": scoped_team_id,
            }
            try:
                found = await client.search_players(**params)
            except FootballApiError as exc:
                searches.append({**params, "stage": stage, "response_count": 0, "error": str(exc)[:160]})
                continue
            searches.append({**params, "stage": stage, "response_count": len(found)})
            for row in found:
                player = row.get("player") if isinstance(row, dict) else {}
                player_id = player.get("id") if isinstance(player, dict) else None
                if isinstance(player_id, int):
                    if player_id in seen_ids:
                        continue
                    seen_ids.add(player_id)
                rows.append(row)
            if found:
                break
        if rows:
            break


async def _player_search_scopes(
    client: Any,
    *,
    league_id: int | None,
    season: int | None,
    team_id: int | None,
    explicit_context: bool,
) -> tuple[tuple[int | None, int | None, int | None], ...]:
    if team_id is not None:
        return ((league_id, season, team_id),)
    if league_id is not None:
        if season is None:
            season = await client.get_current_season(league_id)
        return ((league_id, season, None),)
    if explicit_context:
        return ()

    scopes: list[tuple[int | None, int | None, int | None]] = []
    seen: set[tuple[int | None, int | None, int | None]] = set()
    for league_key in DEFAULT_PLAYER_SCOPE_LEAGUES:
        try:
            scoped_league_id = await client.resolve_league_id(league_key)
            scoped_season = await client.get_current_season(scoped_league_id)
        except Exception:
            continue
        scope = (scoped_league_id, scoped_season, None)
        if scope not in seen:
            scopes.append(scope)
            seen.add(scope)
    return tuple(scopes)


def _should_use_canonicalizer(parsed: PlayerQuery, resolution: FootballResolution) -> bool:
    if resolution.selected is None or resolution.ambiguous:
        return True
    cleaned = parsed.candidates[0] if parsed.candidates else parsed.raw
    words = _ascii_words(cleaned)
    return bool(words) and (len(words) == 1 and len(words[0]) <= 4)


def _normalize_canonicalizer_result(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    names = raw.get("candidate_names")
    confidence = raw.get("confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    if confidence_value < 0.5 or not isinstance(names, list):
        return {}
    candidates = _dedupe_candidates([str(item) for item in names[:8]])
    if not candidates:
        return {}
    return {"candidate_names": candidates, "confidence": confidence_value}


def _dedupe_candidates(*groups: Any) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        items = group if isinstance(group, (list, tuple, set)) else [group]
        for item in items:
            cleaned = " ".join(str(item).split())
            key = normalize_key(cleaned)
            if cleaned and key and key not in seen:
                result.append(cleaned)
                seen.add(key)
    return tuple(result)


def _cached_alias_candidate(parsed: PlayerQuery, alias_cache: dict[str, dict[str, Any]] | None) -> str | None:
    if not alias_cache:
        return None
    now = time.monotonic()
    for key in (normalize_key(parsed.raw), *(normalize_key(candidate) for candidate in parsed.candidates)):
        cached = alias_cache.get(key)
        if not cached:
            continue
        if float(cached.get("expires_at", 0.0) or 0.0) < now:
            alias_cache.pop(key, None)
            continue
        canonical = str(cached.get("canonical_name", "") or "").strip()
        if canonical:
            return canonical
    return None


def _store_validated_alias(
    parsed: PlayerQuery,
    row: dict[str, Any],
    alias_cache: dict[str, dict[str, Any]] | None,
    *,
    confidence: float,
    ttl_seconds: int,
) -> None:
    if alias_cache is None:
        return
    player = row.get("player") if isinstance(row, dict) else {}
    if not isinstance(player, dict):
        return
    name = str(player.get("name", "") or "").strip()
    if not name:
        return
    now = time.monotonic()
    payload = {
        "canonical_name": name,
        "api_player_id": player.get("id"),
        "source": "llm_suggested_api_validated",
        "confidence": confidence,
        "created_at": now,
        "expires_at": now + max(60, ttl_seconds),
    }
    keys = [normalize_key(parsed.raw), *(normalize_key(candidate) for candidate in parsed.candidates)]
    for key in keys:
        if key:
            alias_cache[key] = dict(payload)


def _pick_entity(items: list[dict[str, Any]], query: str, *, key: str) -> FootballResolution:
    normalized = normalize_key(query)
    if not normalized:
        return FootballResolution(None)

    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for item in items:
        entity = item.get(key) if isinstance(item, dict) else None
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name", "") or "")
        candidate = normalize_key(name)
        if candidate == normalized:
            exact.append(item)
        elif normalized and normalized in candidate:
            partial.append(item)

    if len(exact) == 1 and not partial:
        return FootballResolution(exact[0], tuple(exact), ambiguous=False)
    if len(exact) == 1 and partial:
        return FootballResolution(None, tuple(exact + partial), ambiguous=True)
    if len(exact) > 1:
        return FootballResolution(None, tuple(exact), ambiguous=True)
    if len(partial) == 1:
        return FootballResolution(partial[0], tuple(partial), ambiguous=False)
    if len(partial) > 1:
        return FootballResolution(None, tuple(partial), ambiguous=True)
    return FootballResolution(items[0] if items else None, tuple(items[:5]), ambiguous=len(items) > 1)


def extract_table_rows(standings_response: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not standings_response:
        return []
    first = standings_response[0]
    league = first.get("league") if isinstance(first, dict) else None
    if not isinstance(league, dict):
        return []
    standings = league.get("standings")
    if not isinstance(standings, list) or not standings:
        return []
    if isinstance(standings[0], list):
        rows: list[dict[str, Any]] = []
        for group in standings:
            if isinstance(group, list):
                rows.extend(row for row in group if isinstance(row, dict))
        return rows
    return [row for row in standings if isinstance(row, dict)]


def find_team_row(rows: list[dict[str, Any]], team_id: int | None) -> dict[str, Any] | None:
    if not isinstance(team_id, int):
        return None
    for row in rows:
        team = row.get("team")
        if isinstance(team, dict) and team.get("id") == team_id:
            return row
    return None
