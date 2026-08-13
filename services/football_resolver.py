from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final

from services.api_football import FootballApiError
from services.football_api_request_compiler import (
    CountrySlot,
    InvalidFootballApiRequest,
    LeagueSlot,
    PlayerCandidate,
    PlayerSlot,
    TeamSlot,
    _make_player_slot,
    _make_team_slot,
    build_league_search_request,
    build_player_profile_request,
    build_player_search_request,
    build_player_stats_request,
    build_team_search_request,
)


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
    "rayados": "Monterrey",
    "rayadosdemonterrey": "Monterrey",
    "monterrey": "Monterrey",
    "tigres": "Tigres UANL",
    "tigresuanl": "Tigres UANL",
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
PLAYER_QUERY_STOP_WORDS.update(
    {
        "ahora",
        "carrera",
        "cual",
        "cuál",
        "dame",
        "darme",
        "dime",
        "donde",
        "dónde",
        "equipo",
        "estadistica",
        "estadisticas",
        "fue",
        "historial",
        "juega",
        "jugar",
        "lesion",
        "lesiones",
        "lesionado",
        "lesionados",
        "mas",
        "más",
        "podrias",
        "podrías",
        "puedes",
        "reciente",
        "recientes",
        "transferencia",
        "transferencias",
        "ultimo",
        "ultima",
        "su",
        "sus",
        "último",
        "última",
    }
)


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


@dataclass(frozen=True)
class FootballLeagueLookup:
    candidate: str
    league_key: str | None
    league_id: int | None
    season: int | None
    row: dict[str, Any] | None = None
    ambiguous: bool = False
    matches: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class FootballTeamLookup:
    candidate: str
    resolution: FootballResolution
    rows: tuple[dict[str, Any], ...]
    searches: tuple[dict[str, Any], ...]


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


def slot_allows_static_alias(slot: Any) -> bool:
    authority = str(getattr(slot, "authority", "") or "")
    if authority in {"DERIVED_ALIAS", "CANONICAL_EQUIVALENT", "VALIDATED_FOOTBALL_CONTEXT"}:
        return bool(getattr(slot, "evidence", None) or getattr(slot, "equivalent_to", None))
    return str(getattr(slot, "source", "") or "") in {"slash_arg", "alias", "canonicalizer"}


async def resolve_league_candidate(client: Any, query: LeagueSlot, *, country: CountrySlot | None = None, season: int | None = None) -> FootballLeagueLookup:
    if not isinstance(query, LeagueSlot):
        raise InvalidFootballApiRequest("resolve_league_candidate requires a FootballQuerySpec league slot.")
    cleaned = query.name
    if not cleaned:
        return FootballLeagueLookup(candidate="", league_key=None, league_id=None, season=season)

    league_key = normalize_league_key(cleaned) if slot_allows_static_alias(query) else None
    if league_key is not None:
        league_id = await client.resolve_league_id(league_key)
        resolved_season = season or await client.get_current_season(league_id)
        return FootballLeagueLookup(candidate=cleaned, league_key=league_key, league_id=league_id, season=resolved_season)

    rows: list[dict[str, Any]] = []
    search_method = getattr(client, "search_leagues", None)
    if search_method is not None:
        if country is not None:
            try:
                rows = await search_method(
                    build_league_search_request(
                        query,
                        country=country,
                        current=True,
                    )
                )
            except (FootballApiError, InvalidFootballApiRequest):
                rows = []
        try:
            rows = rows or await search_method(
                build_league_search_request(
                    search=query,
                    current=True,
                )
            )
        except FootballApiError:
            rows = []
        except InvalidFootballApiRequest:
            rows = []
    picked = pick_league(rows, cleaned)
    league = picked.selected.get("league") if isinstance(picked.selected, dict) else {}
    league_id = league.get("id") if isinstance(league, dict) else None
    resolved_season = season
    if isinstance(league_id, int) and resolved_season is None:
        resolved_season = await client.get_current_season(league_id)
    return FootballLeagueLookup(
        candidate=cleaned,
        league_key=None,
        league_id=league_id if isinstance(league_id, int) else None,
        season=resolved_season,
        row=picked.selected,
        ambiguous=picked.ambiguous,
        matches=picked.matches,
    )


async def resolve_team_candidate(
    client: Any,
    query: TeamSlot,
    *,
    league_id: int | None = None,
    season: int | None = None,
    allow_global: bool = True,
    use_search_fallback: bool = True,
) -> FootballTeamLookup:
    if not isinstance(query, TeamSlot):
        raise InvalidFootballApiRequest("resolve_team_candidate requires a FootballQuerySpec team slot.")
    alias_allowed = slot_allows_static_alias(query)
    candidate = canonical_team_query(query.name) if alias_allowed else query.name.strip()
    if not candidate:
        return FootballTeamLookup(candidate="", resolution=FootballResolution(None), rows=(), searches=())

    rows: list[dict[str, Any]] = []
    searches: list[dict[str, Any]] = []
    search_slots = [query]
    if alias_allowed and normalize_key(candidate) != normalize_key(query.name):
        try:
            search_slots.append(
                _make_team_slot(
                    candidate,
                    source="alias",
                    authority="DERIVED_ALIAS",
                    literal=query.literal or query.name,
                    evidence=query.evidence or query.name,
                    equivalent_to=candidate,
                    league_hint=query.league_hint,
                    country_hint=query.country_hint,
                )
            )
        except InvalidFootballApiRequest:
            pass
    scopes: list[tuple[int | None, int | None]] = [(league_id, season)]
    if allow_global and (league_id is not None or season is not None):
        scopes.append((None, None))
    seen_scope: set[tuple[int | None, int | None]] = set()
    for scoped_league_id, scoped_season in scopes:
        scope = (scoped_league_id, scoped_season)
        if scope in seen_scope:
            continue
        seen_scope.add(scope)
        for use_search in ((False, True) if use_search_fallback else (False,)):
            for search_slot in search_slots:
                search_name = search_slot.name
                try:
                    search_request = build_team_search_request(
                        search_slot,
                        league_id=scoped_league_id,
                        season=scoped_season,
                        search=use_search,
                    )
                except InvalidFootballApiRequest as exc:
                    searches.append({"name": search_name, "mode": "search" if use_search else "name", "league_id": scoped_league_id, "season": scoped_season, "response_count": 0, "error": str(exc)[:160]})
                    continue
                try:
                    found = await client.search_teams(search_request)
                except FootballApiError as exc:
                    searches.append({"name": search_name, "mode": "search" if use_search else "name", "league_id": scoped_league_id, "season": scoped_season, "response_count": 0, "error": str(exc)[:160]})
                    continue
                searches.append({"name": search_slot.name, "mode": "search" if use_search else "name", "league_id": scoped_league_id, "season": scoped_season, "response_count": len(found)})
                rows.extend(found)
    deduped = _dedupe_entity_rows(rows, "team")
    picked = pick_team(deduped, candidate)
    return FootballTeamLookup(candidate=candidate, resolution=picked, rows=tuple(deduped), searches=tuple(searches))


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

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = " ".join(str(candidate).split())
        key = normalize_key(cleaned)
        if cleaned and key and key not in seen:
            deduped.append(cleaned)
            seen.add(key)
    return PlayerQuery(raw=raw, candidates=tuple(deduped), stat_focus=stat_focus)


async def resolve_player(
    client: Any,
    query: PlayerSlot,
    *,
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    explicit_context: bool = False,
    canonicalizer: FootballPlayerCanonicalizer | None = None,
    alias_cache: dict[str, dict[str, Any]] | None = None,
    cache_ttl_seconds: int = 86400,
    candidate_names: list[PlayerSlot] | tuple[PlayerSlot, ...] | None = None,
    stat_focus: str | None = None,
    team_hint: str | None = None,
    league_hint: str | None = None,
    country_hint: str | None = None,
    nationality_hint: str | None = None,
) -> FootballPlayerLookup:
    if not isinstance(query, PlayerSlot):
        raise InvalidFootballApiRequest("resolve_player requires a FootballQuerySpec player slot.")
    parsed = PlayerQuery(raw=query.full_name, candidates=(query.full_name,), stat_focus=stat_focus)
    if stat_focus and not parsed.stat_focus:
        parsed = PlayerQuery(raw=parsed.raw, candidates=parsed.candidates, stat_focus=stat_focus)
    cached = _cached_alias_candidate(parsed, alias_cache)
    compiled_candidates: list[PlayerCandidate] = [query]
    for candidate in candidate_names or ():
        if isinstance(candidate, PlayerSlot) and candidate not in compiled_candidates:
            compiled_candidates.append(candidate)
    if cached:
        try:
            cached_slot = _make_player_slot(cached, source="validated_context")
            if cached_slot not in compiled_candidates:
                compiled_candidates.insert(0, cached_slot)
        except InvalidFootballApiRequest:
            pass
    if not compiled_candidates:
        return FootballPlayerLookup(
            query=parsed,
            resolution=FootballResolution(None),
            rows=(),
            searches=({"stage": "compile", "response_count": 0, "error": "player_candidate_invalid"},),
            canonicalizer_used=False,
        )
    rows: list[dict[str, Any]] = []
    searches: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    profile_method = getattr(client, "search_player_profiles", None)
    if profile_method is not None:
        for candidate in compiled_candidates:
            try:
                found_profiles = await profile_method(build_player_profile_request(candidate))
            except (FootballApiError, InvalidFootballApiRequest) as exc:
                searches.append({"name": candidate.full_name, "stage": "profile_search", "response_count": 0, "error": str(exc)[:160]})
                continue
            searches.append({"name": candidate.full_name, "stage": "profile_search", "response_count": len(found_profiles)})
            for row in found_profiles:
                player = row.get("player") if isinstance(row, dict) else {}
                player_id = player.get("id") if isinstance(player, dict) else None
                if isinstance(player_id, int):
                    if player_id in seen_ids:
                        continue
                    seen_ids.add(player_id)
                rows.append(row)
            if found_profiles:
                break

    rows[:] = _merge_player_identity_rows(rows)

    if not rows:
        await _search_player_candidates(
            client,
            tuple(compiled_candidates),
            rows=rows,
            searches=searches,
            seen_ids=seen_ids,
            league_id=league_id,
            season=season,
            team_id=team_id,
            explicit_context=explicit_context,
            stage="api_search",
        )
    rows[:] = _merge_player_identity_rows(rows)

    resolution = pick_player_identity(
        rows,
        compiled_candidates[0],
        team_hint=team_hint,
        league_hint=league_hint,
        country_hint=country_hint,
        nationality_hint=nationality_hint,
    )
    canonicalizer_used = False
    if canonicalizer is not None and _should_use_canonicalizer(parsed, resolution):
        suggestion = _normalize_canonicalizer_result(await canonicalizer(parsed))
        suggested_candidates = _dedupe_candidates(suggestion.get("candidate_names", ()) if suggestion else ())
        if suggested_candidates:
            canonicalizer_used = True
            validated_rows: list[dict[str, Any]] = []
            validated_searches: list[dict[str, Any]] = []
            validated_seen_ids: set[int] = set()
            suggested_compiled: list[PlayerCandidate] = []
            for suggested in suggested_candidates:
                try:
                    suggested_compiled.append(_make_player_slot(suggested, source="canonicalizer"))
                except InvalidFootballApiRequest:
                    continue
            profile_method = getattr(client, "search_player_profiles", None)
            if profile_method is not None:
                for suggested in suggested_compiled:
                    try:
                        found_profiles = await profile_method(build_player_profile_request(suggested))
                    except (FootballApiError, InvalidFootballApiRequest) as exc:
                        validated_searches.append({"name": suggested.full_name, "stage": "profile_validation", "response_count": 0, "error": str(exc)[:160]})
                        continue
                    validated_searches.append({"name": suggested.full_name, "stage": "profile_validation", "response_count": len(found_profiles)})
                    validated_rows.extend(found_profiles)
                    if found_profiles:
                        break
            validated_rows[:] = _merge_player_identity_rows(validated_rows)
            if not validated_rows:
                await _search_player_candidates(
                    client,
                    tuple(suggested_compiled),
                    rows=validated_rows,
                    searches=validated_searches,
                    seen_ids=validated_seen_ids,
                    league_id=league_id,
                    season=season,
                    team_id=team_id,
                    explicit_context=explicit_context,
                    stage="api_validation",
                )
            validated_rows[:] = _merge_player_identity_rows(validated_rows)
            validated_resolution = (
                pick_player_identity(
                    validated_rows,
                    suggested_compiled[0],
                    team_hint=team_hint,
                    league_hint=league_hint,
                    country_hint=country_hint,
                    nationality_hint=nationality_hint,
                )
                if suggested_compiled
                else FootballResolution(None)
            )
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


def _merge_player_identity_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for row in rows:
        player = row.get("player") if isinstance(row, dict) else {}
        player_id = player.get("id") if isinstance(player, dict) else None
        if not isinstance(player_id, int):
            if isinstance(row, dict):
                unkeyed.append(row)
            continue
        current = merged.get(player_id)
        if current is None:
            merged[player_id] = dict(row)
            continue
        current_player = current.get("player") if isinstance(current, dict) else {}
        current_name = str(current_player.get("name") if isinstance(current_player, dict) else "")
        new_name = str(player.get("name") if isinstance(player, dict) else "")
        if _identity_name_is_more_specific(new_name, current_name):
            merged_player = dict(current_player) if isinstance(current_player, dict) else {}
            merged_player.update(player)
            current["player"] = merged_player
        current_stats = current.get("statistics") if isinstance(current, dict) else []
        new_stats = row.get("statistics") if isinstance(row, dict) else []
        if (not isinstance(current_stats, list) or not current_stats) and isinstance(new_stats, list) and new_stats:
            current["statistics"] = new_stats
    return [*merged.values(), *unkeyed]


def _identity_name_is_more_specific(candidate: str, existing: str) -> bool:
    candidate_words = _ascii_words(candidate)
    existing_words = _ascii_words(existing)
    if len(candidate_words) > len(existing_words):
        return True
    if any(len(word) > 1 for word in candidate_words) and any(len(word) == 1 for word in existing_words):
        return True
    return len(candidate) > len(existing) and normalize_key(candidate) != normalize_key(existing)


def _dedupe_entity_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for row in rows:
        entity = row.get(key) if isinstance(row, dict) else {}
        if not isinstance(entity, dict):
            continue
        entity_id = entity.get("id")
        if isinstance(entity_id, int):
            if entity_id in seen_ids:
                continue
            seen_ids.add(entity_id)
        else:
            name_key = normalize_key(entity.get("name"))
            if not name_key or name_key in seen_names:
                continue
            seen_names.add(name_key)
        deduped.append(row)
    return deduped


def pick_team(teams: list[dict[str, Any]], query: str) -> FootballResolution:
    return _pick_entity(teams, query, key="team")


def pick_player(players: list[dict[str, Any]], query: str) -> FootballResolution:
    return _pick_entity(players, query, key="player")


def pick_player_identity(
    players: list[dict[str, Any]],
    requested: PlayerCandidate,
    *,
    team_hint: str | None = None,
    league_hint: str | None = None,
    country_hint: str | None = None,
    nationality_hint: str | None = None,
) -> FootballResolution:
    if not players:
        return FootballResolution(None)
    requested_full = normalize_key(requested.full_name)
    requested_first = normalize_key(requested.first_name)
    requested_last = normalize_key(requested.last_name)
    has_explicit_first = bool(requested_first)
    scored: list[tuple[int, dict[str, Any]]] = []
    plausible: list[dict[str, Any]] = []
    for row in players:
        player = row.get("player") if isinstance(row, dict) else None
        if not isinstance(player, dict):
            continue
        name = str(player.get("name") or "").strip()
        name_key = normalize_key(name)
        name_words = _ascii_words(name)
        profile_first_key = normalize_key(player.get("firstname"))
        profile_last_key = normalize_key(player.get("lastname"))
        display_first_key = normalize_key(name_words[0] if len(name_words) >= 2 else None)
        display_last_key = normalize_key(name_words[-1] if name_words else None)
        first_key = profile_first_key or display_first_key
        last_key = profile_last_key or display_last_key
        initial_match = bool(
            has_explicit_first
            and not profile_first_key
            and len(display_first_key) == 1
            and requested_first.startswith(display_first_key)
        )
        if has_explicit_first and first_key and first_key != requested_first and not initial_match:
            continue
        score = 0
        profile_full_key = normalize_key(f"{player.get('firstname') or ''} {player.get('lastname') or ''}".strip())
        if requested_full and (name_key == requested_full or profile_full_key == requested_full):
            score += 70
        elif requested_full and (requested_full in name_key or requested_full in profile_full_key):
            score += 45
        if requested_first and first_key == requested_first:
            score += 20
        elif initial_match:
            score += 20
        if requested_last and last_key == requested_last:
            score += 55 if not has_explicit_first else 25
        elif requested_last and not has_explicit_first and first_key == requested_last:
            score += 55
        elif requested_last and requested_last in name_key:
            score += 15
        nationality = normalize_key(player.get("nationality"))
        if nationality_hint and normalize_key(nationality_hint) == nationality:
            score += 10
        profile_team = player.get("team") if isinstance(player.get("team"), dict) else row.get("team") if isinstance(row, dict) and isinstance(row.get("team"), dict) else {}
        profile_league = player.get("league") if isinstance(player.get("league"), dict) else row.get("league") if isinstance(row, dict) and isinstance(row.get("league"), dict) else {}
        if team_hint and isinstance(profile_team, dict) and normalize_key(profile_team.get("name")) == normalize_key(team_hint):
            score += 15
        if league_hint and isinstance(profile_league, dict) and normalize_key(profile_league.get("name")) == normalize_key(league_hint):
            score += 10
        if country_hint and normalize_key(player.get("country")) == normalize_key(country_hint):
            score += 10
        if initial_match and requested_last and last_key == requested_last and any((team_hint, league_hint, country_hint, nationality_hint)):
            score += 10
        if score > 0:
            scored.append((score, row))
            plausible.append(row)
    if not scored:
        return FootballResolution(None, tuple(players[:5]), ambiguous=False)
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_row = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    threshold = 55 if not has_explicit_first and len(scored) == 1 else 70
    if top_score < threshold:
        return FootballResolution(None, tuple(row for _, row in scored[:5]), ambiguous=len(scored) > 1)
    required_margin = 15 if any((team_hint, league_hint, country_hint, nationality_hint)) else 20
    if len(scored) > 1 and top_score - second_score < required_margin:
        return FootballResolution(None, tuple(row for _, row in scored[:5]), ambiguous=True)
    return FootballResolution(top_row, tuple(plausible[:5]), ambiguous=False)


def pick_league(leagues: list[dict[str, Any]], query: str) -> FootballResolution:
    return _pick_entity(leagues, query, key="league")


def _ascii_words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.casefold())


async def _search_player_candidates(
    client: Any,
    candidates: tuple[PlayerCandidate, ...],
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
    search_method = getattr(client, "search_players", None)
    if search_method is None:
        return
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
            try:
                request = build_player_search_request(
                    candidate,
                    league_id=scoped_league_id,
                    season=scoped_season,
                    team_id=scoped_team_id,
                )
                found = await search_method(request)
            except (FootballApiError, InvalidFootballApiRequest) as exc:
                searches.append(
                    {
                        "name": candidate.full_name,
                        "league_id": scoped_league_id,
                        "season": scoped_season,
                        "team_id": scoped_team_id,
                        "stage": stage,
                        "response_count": 0,
                        "error": str(exc)[:160],
                    }
                )
                continue
            searches.append(
                {
                    "name": candidate.full_name,
                    "league_id": scoped_league_id,
                    "season": scoped_season,
                    "team_id": scoped_team_id,
                    "stage": stage,
                    "response_count": len(found),
                }
            )
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
        if season is None:
            if league_id is not None:
                season = await client.get_current_season(league_id)
            else:
                return ()
        return ((league_id, season, team_id),)
    if league_id is not None:
        if season is None:
            season = await client.get_current_season(league_id)
        return ((league_id, season, None),)
    return ()


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


def _prune_redundant_player_candidates(candidates: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    normalized_words = [(candidate, _ascii_words(candidate)) for candidate in candidates]
    for candidate, words in normalized_words:
        if not words:
            continue
        redundant = False
        for other, other_words in normalized_words:
            if other == candidate or len(other_words) >= len(words) or len(other_words) < 2:
                continue
            if words[: len(other_words)] == other_words:
                redundant = True
                break
        if not redundant:
            result.append(candidate)
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

    if len(exact) == 1:
        return FootballResolution(exact[0], tuple(exact), ambiguous=False)
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
