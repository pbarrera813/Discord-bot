from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata
from typing import Any

from services import football_resolver
from services.football_api_request_compiler import (
    CountrySlot,
    FootballCapabilityIntent,
    InvalidFootballApiRequest,
    LeagueSlot,
    PlayerSlot,
    TeamSlot,
    _make_country_slot,
    _make_league_slot,
    _make_player_slot,
    _make_team_slot,
)


@dataclass(frozen=True)
class FootballQuerySpec:
    operation_type: str
    route_action: str
    data_focus: str | None
    player: PlayerSlot | None
    team: TeamSlot | None
    opponent: TeamSlot | None
    teams: tuple[TeamSlot, ...]
    league: LeagueSlot | None
    leagues: tuple[LeagueSlot, ...]
    country: CountrySlot | None
    countries: tuple[CountrySlot, ...]
    fixture_focus: str | None
    stat_focus: str | None
    date_hint: str | None
    season_hint: str | None
    live: bool
    time_scope: str | None
    source_route: str
    capability_intent: FootballCapabilityIntent | None = None


@dataclass(frozen=True)
class FootballQueryOperation:
    operation_type: str
    route_action: str
    data_focus: str | None
    league_candidates: tuple[str, ...]
    country_candidates: tuple[str, ...]
    team_candidates: tuple[str, ...]
    player_candidates: tuple[str, ...]
    fixture_focus: str | None
    stat_focus: str | None
    date_hint: str | None
    season_hint: str | None
    live: bool
    time_scope: str | None
    spec: FootballQuerySpec | None = None
    league_slots: tuple[LeagueSlot, ...] = ()
    country_slots: tuple[CountrySlot, ...] = ()
    team_slots: tuple[TeamSlot, ...] = ()
    player_slots: tuple[PlayerSlot, ...] = ()
    capability_intent: FootballCapabilityIntent | None = None


CompiledFootballOperation = FootballQueryOperation


@dataclass(frozen=True)
class _StructuredPlayerExtraction:
    players: tuple[str, ...] = ()
    team_hint: str | None = None
    league_hint: str | None = None
    country_hint: str | None = None
    nationality_hint: str | None = None


OPERATION_TYPES = {
    "fixture_result",
    "fixture_live",
    "fixture_next",
    "fixture_last",
    "fixture_events",
    "fixture_statistics",
    "fixture_lineups",
    "standings",
    "team_profile",
    "player_profile",
    "player_recent_stats",
    "player_current_team",
    "player_previous_team",
    "player_career_history",
    "player_teams",
    "player_transfers",
    "player_injuries",
    "player_trophies",
    "player_sidelined",
    "player_match_stats",
    "team_squad",
    "team_season_statistics",
    "team_transfers",
    "team_injuries",
    "competition_rounds",
    "competition_current_round",
    "competition_round_fixtures",
    "competition_structure",
    "match_center",
    "fixture_status",
    "fixture_shootout",
    "fixture_shootout_attempts",
    "fixture_statistics_half",
    "fixture_statistics_half_comparison",
    "fixture_prediction",
    "fixture_odds_pre_match",
    "fixture_odds_live",
    "coach_profile",
    "team_current_coach",
    "coach_career",
    "coach_trophies",
    "coach_sidelined",
    "venue_lookup",
    "team_venue",
    "football_countries",
    "football_timezones",
    "league_seasons",
    "team_countries",
    "odds_bookmakers",
    "odds_bets",
    "odds_live_bets",
    "league_lookup",
    "top_scorers",
    "top_assists",
    "top_yellow_cards",
    "top_red_cards",
    "injuries",
    "transfers",
    "h2h",
    "live_watch_start",
    "live_watch_stop",
    "unknown",
}

_SENTENCE_HINTS = {
    "cuando",
    "cuanto",
    "cuantos",
    "cuanta",
    "cuantas",
    "como",
    "quien",
    "quienes",
    "donde",
    "equipo",
    "podrias",
    "puedes",
    "dame",
    "darme",
    "empieza",
    "temporada",
    "proximos",
    "ultimos",
    "partidos",
    "juegos",
    "tabla",
    "metio",
    "goles",
    "estadisticas",
    "recientes",
    "ultimo",
    "ahora",
    "juega",
    "lesiones",
    "lesionados",
    "transferencias",
    "historial",
    "informacion",
}

_CAPABILITY_MARKERS: dict[str, tuple[str, ...]] = {
    "season_start": ("cuando inicia", "cuando empieza", "cuando arranca", "start of season", "season start"),
    "next_fixtures": (
        "proximo",
        "proximos",
        "cuando juega",
        "next match",
        "next game",
        "next fixture",
        "next fixtures",
        "upcoming match",
        "upcoming game",
        "upcoming fixture",
        "upcoming fixtures",
        "siguiente partido",
        "siguiente juego",
        "siguiente fixture",
    ),
    "summary": ("resultado", "como quedo", "como quedaron", "jugaron", "played", "finish", "finished", "final score"),
    "standings": ("tabla", "table", "standings", "posiciones", "clasificacion"),
    "events": ("gol", "goles", "eventos", "events", "cards", "tarjetas", "cambios", "substitutions", "var"),
    "statistics": ("estadistica", "estadisticas", "stats", "statistics", "posesion", "tiros"),
    "lineups": ("alineacion", "alineaciones", "lineup", "lineups", "once inicial", "starting eleven"),
    "fixture_players": ("participaron", "participated", "fixture players", "player match stats", "estadisticas de jugadores"),
    "prediction": ("prediccion", "prediction", "pronostico", "probabilidad"),
    "odds": ("odds", "cuotas", "momios", "bookmaker", "apuestas"),
    "injuries": ("lesion", "lesionado", "lesionados", "injury", "injuries"),
    "sidelined": ("sidelined", "bajas historicas", "sanciones", "suspensiones"),
    "transfers": ("transfer", "transfers", "fichaje", "fichajes"),
    "coach_career": ("carrera de entrenador", "coach career", "historial de entrenador"),
    "coach_trophies": ("trofeos de entrenador", "titulos de entrenador", "coach trophies"),
    "shootout": ("penales", "shootout", "tanda"),
    "shootout_attempts": ("tirador", "cobrador", "attempt", "intento"),
    "current_live": ("ahora", "ahorita", "now", "live", "en vivo"),
    "last_fixtures": ("ultimo", "ultimos", "last match", "last game", "last fixture", "previous match", "previous fixture"),
}


def build_operation(route_action: str, request: str, plan: dict[str, Any] | None) -> FootballQueryOperation:
    return compile_football_operation(route_action, request, plan)


def compile_football_operation(
    route_action: str,
    request: str,
    plan: dict[str, Any] | None,
    prior_context: str | None = None,
) -> FootballQueryOperation:
    date_correction_only = bool(prior_context) and _request_is_date_correction_only(request)
    parsed_player = football_resolver.parse_player_query(request)
    structured_player = _StructuredPlayerExtraction() if str(route_action or "").upper() == "FOOTBALL_COMPARISON" else _structured_player_extraction(request)
    planned_players = _list(plan, "player_candidates")
    planned_focus = _text(plan, "data_focus")
    planned_teams = _list(plan, "team_candidates")
    prior_context_operation = _operation_type_from_prior_context(prior_context)
    text_players = []
    if not _planned_team_entity_focus(planned_focus, planned_teams):
        text_players = list(structured_player.players) or _player_candidates_from_text(parsed_player, route_action, planned_focus, request)
    prior_context_player = _player_candidate_from_prior_context(prior_context)
    if prior_context_player and (planned_players or text_players):
        prior_key = football_resolver.normalize_key(prior_context_player)
        planned_players = [candidate for candidate in planned_players if football_resolver.normalize_key(candidate) != prior_key]
        text_players = [candidate for candidate in text_players if football_resolver.normalize_key(candidate) != prior_key]
    # Prior context is only a fallback for true follow-ups. If the current turn
    # names a player, do not contaminate the same slot with an older player.
    prior_player = None if planned_players or text_players else prior_context_player
    prior_player_candidates = clean_entity_candidates("player", [prior_player] if prior_player else [], request, allow_contextual=True)
    prior_player_keys = _candidate_keys(prior_player_candidates)
    player_candidates = _dedupe(
        [
            *clean_entity_candidates("player", [*planned_players, *text_players], request),
            *prior_player_candidates,
        ]
    )
    has_player_focus = _has_player_focus(route_action, planned_focus, player_candidates, planned_teams)
    data_focus = _data_focus(plan, route_action, request, has_player_focus)
    if data_focus is None and prior_context_operation in {"league_fixture_results", "team_fixture_result", "fixture_result"} and _request_has_date_correction(request):
        data_focus = "summary"
    route = _route_action_for_focus(route_action, data_focus)
    operation_type = _operation_type(route, data_focus, request, bool(player_candidates))
    planned_leagues = _list(plan, "league_candidates")
    text_leagues = extract_alias_candidates("league", request)
    prior_league = _league_candidate_from_prior_context(prior_context)
    prior_league_candidates = clean_entity_candidates("league", [prior_league] if prior_league and not planned_leagues and not text_leagues else [], request, allow_contextual=True)
    prior_league_keys = _candidate_keys(prior_league_candidates)
    league_candidates = _dedupe(
        [
            *clean_entity_candidates("league", [*planned_leagues, *text_leagues], request),
            *prior_league_candidates,
        ]
    )
    if prior_league and not league_candidates and not player_candidates:
        league_candidates = clean_entity_candidates("league", [prior_league], request, allow_contextual=True)
        prior_league_keys = _candidate_keys(league_candidates)
    country_candidates = clean_entity_candidates("country", _list(plan, "country_candidates"), request)
    league_scoped_operations = {
        "standings",
        "top_scorers",
        "top_assists",
        "top_yellow_cards",
        "top_red_cards",
        "league_lookup",
        "competition_rounds",
        "competition_current_round",
        "competition_round_fixtures",
        "competition_structure",
    }
    derive_team_from_text = not operation_type.startswith("player_") and operation_type not in league_scoped_operations
    if planned_teams and operation_type in {
        "fixture_prediction",
        "fixture_odds_pre_match",
        "fixture_odds_live",
        "fixture_shootout",
        "fixture_shootout_attempts",
        "fixture_statistics_half",
        "fixture_statistics_half_comparison",
    }:
        team_text_candidates = []
    elif route in {"FOOTBALL_WATCH_TODAY", "FOOTBALL_LIVE_WATCH_START"} and not planned_teams:
        team_text_candidates = extract_alias_candidates("team", request) if route == "FOOTBALL_WATCH_TODAY" else []
    elif date_correction_only:
        team_text_candidates = []
    else:
        team_text_candidates = _team_candidates_from_text(request) if derive_team_from_text else []
    if operation_type.startswith("player_") and structured_player.team_hint:
        team_text_candidates.append(structured_player.team_hint)
    prior_teams = _team_candidates_from_prior_context(prior_context)
    prior_team_candidates = clean_entity_candidates("team", prior_teams if prior_teams and not planned_teams and not team_text_candidates and not player_candidates else [], request, allow_contextual=True)
    prior_team_keys = _candidate_keys(prior_team_candidates)
    team_candidates = _dedupe(
        [
            *clean_entity_candidates("team", [*_list(plan, "team_candidates"), *team_text_candidates], request),
            *prior_team_candidates,
        ]
    )
    if prior_teams and not team_candidates and not player_candidates:
        team_candidates = clean_entity_candidates("team", prior_teams, request, allow_contextual=True)
        prior_team_keys = _candidate_keys(team_candidates)
    if operation_type in league_scoped_operations:
        team_candidates = []
    if player_candidates:
        player_candidates = _prune_cross_entity_player_candidates(
            player_candidates,
            team_candidates=team_candidates,
            league_candidates=league_candidates,
            country_candidates=country_candidates,
        )
    if operation_type in {
        "fixture_statistics",
        "fixture_events",
        "fixture_lineups",
        "fixture_live",
        "fixture_result",
        "fixture_next",
        "fixture_last",
        "fixture_prediction",
        "fixture_odds_pre_match",
        "fixture_odds_live",
        "fixture_shootout",
        "fixture_shootout_attempts",
        "fixture_statistics_half",
        "fixture_statistics_half_comparison",
        "standings",
        "top_scorers",
        "top_assists",
        "top_yellow_cards",
        "top_red_cards",
        "league_lookup",
        "team_profile",
        "team_squad",
        "team_season_statistics",
        "team_injuries",
        "team_transfers",
        "team_current_coach",
        "team_venue",
        "venue_lookup",
        "injuries",
        "transfers",
        "h2h",
    }:
        player_candidates = []
    stat_focus = _text(plan, "stat_focus") or (parsed_player.stat_focus if operation_type.startswith("player_") or operation_type == "player_profile" else None)
    if operation_type == "fixture_statistics":
        stat_focus = stat_focus or _stat_focus_from_text(request)
    time_scope = _time_scope(plan, request, operation_type)
    player_slots = tuple(
        _player_slots(
            player_candidates,
            team_candidates=team_candidates,
            league_candidates=league_candidates,
            country_candidates=country_candidates,
            nationality_hint=structured_player.nationality_hint,
            request=request,
            context_keys=prior_player_keys,
        )
    )
    country_slots = tuple(_country_slots(country_candidates))
    league_slots = tuple(_league_slots(league_candidates, country_candidates=country_candidates, request=request, context_keys=prior_league_keys))
    team_slots = tuple(_team_slots(team_candidates, league_candidates=league_candidates, country_candidates=country_candidates, request=request, context_keys=prior_team_keys))
    capability_intent = _capability_intent_from_request(
        request,
        operation_type=operation_type,
        data_focus=data_focus,
        time_scope=time_scope,
        plan=plan,
    )
    spec = FootballQuerySpec(
        operation_type=operation_type,
        route_action=route,
        data_focus=data_focus,
        player=player_slots[0] if player_slots else None,
        team=team_slots[0] if team_slots else None,
        opponent=team_slots[1] if len(team_slots) > 1 else None,
        teams=team_slots,
        league=league_slots[0] if league_slots else None,
        leagues=league_slots,
        country=country_slots[0] if country_slots else None,
        countries=country_slots,
        fixture_focus=_text(plan, "fixture_focus"),
        stat_focus=stat_focus,
        date_hint=_text(plan, "date_hint"),
        season_hint=_text(plan, "season_hint"),
        live=bool(plan.get("live", False)) if isinstance(plan, dict) else False,
        time_scope=time_scope,
        source_route=route,
        capability_intent=capability_intent,
    )
    return FootballQueryOperation(
        operation_type=operation_type,
        route_action=route,
        data_focus=data_focus,
        league_candidates=tuple(league_candidates),
        country_candidates=tuple(country_candidates),
        team_candidates=tuple(team_candidates),
        player_candidates=tuple(player_candidates),
        fixture_focus=_text(plan, "fixture_focus"),
        stat_focus=stat_focus,
        date_hint=_text(plan, "date_hint"),
        season_hint=_text(plan, "season_hint"),
        live=bool(plan.get("live", False)) if isinstance(plan, dict) else False,
        time_scope=time_scope,
        spec=spec,
        league_slots=league_slots,
        country_slots=country_slots,
        team_slots=team_slots,
        player_slots=player_slots,
        capability_intent=capability_intent,
    )


def _candidate_keys(candidates: list[str] | tuple[str, ...]) -> set[str]:
    return {football_resolver.normalize_key(candidate) for candidate in candidates if football_resolver.normalize_key(candidate)}


def _slot_metadata(kind: str, candidate: str, request: str, context_keys: set[str]) -> dict[str, Any]:
    key = football_resolver.normalize_key(candidate)
    if key in context_keys and not _phrase_in_text(candidate, request):
        return {
            "source": "validated_context",
            "authority": "VALIDATED_FOOTBALL_CONTEXT",
            "literal": candidate,
            "source_component": "validated_football_context",
            "evidence": "prior_validated_context",
        }
    alias = _alias_evidence(kind, candidate, request)
    if alias:
        return {
            "source": "alias",
            "authority": "DERIVED_ALIAS",
            "literal": alias,
            "source_component": "alias_extractor",
            "evidence": alias,
            "equivalent_to": candidate,
        }
    if _phrase_in_text(candidate, request):
        return {
            "source": "deterministic_parser",
            "authority": "EXPLICIT_CURRENT_MESSAGE",
            "literal": candidate,
            "source_component": "current_message",
            "evidence": candidate,
        }
    return {
        "source": "deterministic_parser",
        "authority": "EXPLICIT_CURRENT_MESSAGE",
        "literal": candidate,
        "source_component": "current_message",
        "evidence": candidate,
    }


def _alias_evidence(kind: str, candidate: str, request: str) -> str | None:
    aliases = (
        football_resolver.TEAM_ALIASES
        if kind == "team"
        else football_resolver.LEAGUE_ALIASES
        if kind == "league"
        else football_resolver.PLAYER_SEED_ALIASES
        if kind == "player"
        else {}
    )
    candidate_key = football_resolver.normalize_key(candidate)
    for alias, canonical in aliases.items():
        if kind == "league" and alias == "liga":
            continue
        canonical_key = football_resolver.normalize_key(canonical)
        resolved_candidate_key = football_resolver.normalize_league_key(candidate) if kind == "league" else None
        if canonical_key != candidate_key and canonical_key != resolved_candidate_key:
            continue
        if kind == "league" and alias == "laliga":
            words_text = _normalize_words_text(request)
            matched = bool(re.search(r"\blaliga\b", words_text) or re.search(r"\bla\s+liga\b(?!\s+de\b)", words_text))
        else:
            matched = _phrase_in_text(alias, request)
        if matched:
            return alias
    return None


def _team_slots(
    candidates: list[str],
    *,
    league_candidates: list[str] | None = None,
    country_candidates: list[str] | None = None,
    request: str,
    context_keys: set[str],
) -> list[TeamSlot]:
    slots: list[TeamSlot] = []
    seen: set[str] = set()
    league_hint = league_candidates[0] if league_candidates else None
    country_hint = country_candidates[0] if country_candidates else None
    for candidate in candidates:
        try:
            key = football_resolver.normalize_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            metadata = _slot_metadata("team", candidate, request, context_keys)
            slots.append(_make_team_slot(candidate, league_hint=league_hint, country_hint=country_hint, **metadata))
        except InvalidFootballApiRequest:
            continue
    return slots


def _league_slots(candidates: list[str], *, country_candidates: list[str] | None = None, request: str, context_keys: set[str]) -> list[LeagueSlot]:
    slots: list[LeagueSlot] = []
    country_hint = country_candidates[0] if country_candidates else None
    for candidate in candidates:
        try:
            metadata = _slot_metadata("league", candidate, request, context_keys)
            slots.append(_make_league_slot(candidate, country_hint=country_hint, **metadata))
        except InvalidFootballApiRequest:
            continue
    return slots


def _country_slots(candidates: list[str]) -> list[CountrySlot]:
    slots: list[CountrySlot] = []
    for candidate in candidates:
        try:
            slots.append(_make_country_slot(candidate, source="deterministic_parser"))
        except InvalidFootballApiRequest:
            continue
    return slots


def _player_slots(
    candidates: list[str],
    *,
    team_candidates: list[str],
    league_candidates: list[str],
    country_candidates: list[str],
    nationality_hint: str | None = None,
    request: str,
    context_keys: set[str],
) -> list[PlayerSlot]:
    slots: list[PlayerSlot] = []
    team_hint = team_candidates[0] if team_candidates else None
    league_hint = league_candidates[0] if league_candidates else None
    country_hint = country_candidates[0] if country_candidates else None
    for candidate in candidates:
        try:
            metadata = _slot_metadata("player", candidate, request, context_keys)
            slots.append(
                _make_player_slot(
                    candidate,
                    team_hint=team_hint,
                    league_hint=league_hint,
                    country_hint=country_hint,
                    nationality_hint=nationality_hint,
                    **metadata,
                )
            )
        except InvalidFootballApiRequest:
            continue
    return slots


_NATIONALITY_HINTS = {
    "aleman": "Germany",
    "alemana": "Germany",
    "argentina": "Argentina",
    "argentino": "Argentina",
    "brasilena": "Brazil",
    "brasileno": "Brazil",
    "brazilian": "Brazil",
    "colombiana": "Colombia",
    "colombiano": "Colombia",
    "english": "England",
    "espanol": "Spain",
    "espanola": "Spain",
    "frances": "France",
    "francesa": "France",
    "german": "Germany",
    "ingles": "England",
    "italian": "Italy",
    "italiana": "Italy",
    "italiano": "Italy",
    "mexicana": "Mexico",
    "mexicano": "Mexico",
    "portugues": "Portugal",
    "portuguesa": "Portugal",
    "spanish": "Spain",
    "uruguaya": "Uruguay",
    "uruguayo": "Uruguay",
}


def _structured_player_extraction(text: str) -> _StructuredPlayerExtraction:
    normalized = _normalize_words_text(text)
    if not _looks_like_player_entity_request(text):
        return _StructuredPlayerExtraction()

    nationality_hint = None
    for marker, country in _NATIONALITY_HINTS.items():
        if re.search(rf"\b{re.escape(marker)}\b", normalized):
            nationality_hint = country
            break

    team_hint = None
    team_patterns = (
        r"(?i)\b(?:que\s+juega|juega|plays)\s+(?:en|para|for|with)\s+(?:el\s+|la\s+|los\s+|las\s+)?([^,?.]+)",
        r"(?i)\b(?:en|with)\s+(?:el\s+|la\s+|los\s+|las\s+)?([A-ZÁÉÍÓÚÑ][^,?.]+)",
    )
    for pattern in team_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = _clean_structured_entity_phrase(match.group(1))
        if candidate:
            team_hint = candidate
            break

    player_candidates: list[str] = []
    player_patterns = (
        r"(?i)\bd[oó]nde\s+juega\s+([^,?.]+)",
        r"(?i)\b(?:estadisticas|estadísticas|stats|statistics|transferencias|transfers|lesiones|injuries|historial|carrera|equipo|juega)\s+(?:mas\s+recientes\s+)?(?:de|del|for|of)?\s+([^,?.]+)",
        r"(?i)\b(?:de|del|of|for)\s+([^,?.]+)",
    )
    for pattern in player_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = _clean_player_phrase(match.group(1), team_hint=team_hint, nationality_hint=nationality_hint)
        if candidate:
            player_candidates.append(candidate)
            break

    if not player_candidates:
        candidate = _clean_player_phrase(text, team_hint=team_hint, nationality_hint=nationality_hint)
        if candidate:
            player_candidates.append(candidate)

    return _StructuredPlayerExtraction(
        players=tuple(_dedupe(player_candidates)),
        team_hint=team_hint,
        nationality_hint=nationality_hint,
    )


def _clean_structured_entity_phrase(value: str) -> str | None:
    cleaned = re.sub(r"(?i)\b(?:ahora|actualmente|today|now|por\s+favor|please)\b", " ", str(value or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:80] or None


def _clean_player_phrase(value: str, *, team_hint: str | None, nationality_hint: str | None) -> str | None:
    cleaned = str(value or "")
    cleaned = re.sub(r"(?i)\b(?:jugador|player|que\s+juega|juega|plays|en|para|for|with|el|la|los|las|de|del|of|actualmente|ahora)\b.*$", " ", cleaned)
    if team_hint:
        cleaned = re.sub(re.escape(team_hint), " ", cleaned, flags=re.IGNORECASE)
    if nationality_hint:
        for marker, country in _NATIONALITY_HINTS.items():
            if country == nationality_hint:
                cleaned = re.sub(rf"(?i)\b{re.escape(marker)}\b", " ", cleaned)
    cleaned = re.sub(r"(?i)\b(?:donde|dónde|mas|más|recientes?|estadisticas|estadísticas|stats|statistics|transferencias|transfers|lesiones|injuries|historial|carrera|equipo)\b", " ", cleaned)
    cleaned = " ".join(cleaned.split(" ,")).strip(" ,")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if looks_like_raw_sentence(cleaned, value):
        return None
    return _title_candidate(cleaned)


def clean_entity_candidates(
    kind: str,
    candidates: list[str] | tuple[str, ...],
    original_request: str,
    *,
    allow_contextual: bool = False,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = " ".join(str(candidate or "").split())[:120]
        key = football_resolver.normalize_key(cleaned)
        if not cleaned or not key or key in seen:
            continue
        if looks_like_raw_sentence(cleaned, original_request):
            logging.info(
                "AI football raw sentence candidate rejected kind=%s candidate_hash=%s",
                kind,
                abs(hash(key)) % 100000,
            )
            continue
        if not allow_contextual and not _candidate_is_grounded(kind, cleaned, original_request):
            logging.info(
                "AI football ungrounded candidate rejected kind=%s candidate_hash=%s request_hash=%s",
                kind,
                abs(hash(key)) % 100000,
                abs(hash(football_resolver.normalize_key(original_request))) % 100000,
            )
            continue
        result.append(cleaned)
        seen.add(key)
    return result


def _prune_cross_entity_player_candidates(
    candidates: list[str],
    *,
    team_candidates: list[str],
    league_candidates: list[str],
    country_candidates: list[str],
) -> list[str]:
    entity_words = [
        _words(entity)
        for entity in [*team_candidates, *league_candidates, *country_candidates]
        if entity
    ]
    result: list[str] = []
    seen: set[str] = set()
    candidate_words = [(candidate, _words(candidate)) for candidate in candidates]
    for candidate, words in candidate_words:
        pruned_words = list(words)
        pruned = False
        for hint_words in entity_words:
            if hint_words and len(pruned_words) > len(hint_words) and pruned_words[-len(hint_words) :] == hint_words:
                pruned_words = pruned_words[: -len(hint_words)]
                pruned = True
                logging.info("AI football player candidate pruned cross_entity_suffix candidate_hash=%s", abs(hash(football_resolver.normalize_key(candidate))) % 100000)
        redundant = any(
            other != candidate
            and len(other_words) >= 2
            and len(other_words) < len(pruned_words)
            and pruned_words[: len(other_words)] == other_words
            for other, other_words in candidate_words
        )
        if redundant:
            logging.info("AI football player candidate rejected cross_entity_or_redundant candidate_hash=%s", abs(hash(football_resolver.normalize_key(candidate))) % 100000)
            continue
        cleaned = " ".join(part.capitalize() for part in pruned_words) if pruned else candidate
        key = football_resolver.normalize_key(cleaned)
        if cleaned and key and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


def looks_like_raw_sentence(value: str, original_request: str | None = None) -> bool:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return False
    normalized = football_resolver.normalize_key(cleaned)
    original_normalized = football_resolver.normalize_key(original_request or "")
    words = _words(cleaned)
    if original_normalized and normalized == original_normalized and len(words) > 2:
        return True
    if len(words) >= 7:
        return True
    if "?" in cleaned or "¿" in cleaned:
        return True
    hint_count = sum(1 for word in words if word in _SENTENCE_HINTS)
    return hint_count >= 2


def extract_alias_candidates(kind: str, text: str) -> list[str]:
    if not football_resolver.normalize_key(text):
        return []
    aliases = (
        football_resolver.TEAM_ALIASES
        if kind == "team"
        else football_resolver.LEAGUE_ALIASES
        if kind == "league"
        else football_resolver.PLAYER_SEED_ALIASES
    )
    candidates: list[str] = []
    words_text = _normalize_words_text(text)
    for alias, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if kind == "league" and alias == "liga":
            matched = False
        elif kind == "league" and alias == "laliga":
            matched = bool(
                re.search(r"\blaliga\b", words_text)
                or re.search(r"\bla\s+liga\b(?!\s+de\b)", words_text)
            )
        else:
            matched = _phrase_in_text(alias, text)
        if matched:
            candidates.append(canonical)
    return clean_entity_candidates(kind, candidates, text)


def _candidate_is_grounded(kind: str, candidate: str, text: str) -> bool:
    candidate_key = football_resolver.normalize_key(candidate)
    if kind == "league" and candidate_key == "laliga" and re.search(r"\bla\s+liga\s+de\b", _normalize_words_text(text)):
        return False
    if _phrase_in_text(candidate, text):
        return True
    if kind == "league" and football_resolver.normalize_key(candidate) == "laliga":
        normalized = _normalize_words_text(text)
        if re.search(r"\bla\s+liga\b(?!\s+de\b)", normalized):
            return True
    aliases = (
        football_resolver.TEAM_ALIASES
        if kind == "team"
        else football_resolver.LEAGUE_ALIASES
        if kind == "league"
        else football_resolver.PLAYER_SEED_ALIASES
        if kind == "player"
        else {}
    )
    for alias, canonical in aliases.items():
        canonical_key = football_resolver.normalize_key(canonical)
        resolved_candidate_key = football_resolver.normalize_league_key(candidate) if kind == "league" else None
        if canonical_key != candidate_key and canonical_key != resolved_candidate_key:
            continue
        if _phrase_in_text(alias, text):
            return True
    return False


def _phrase_in_text(phrase: str, text: str) -> bool:
    phrase_words = _words(phrase)
    text_words = _words(text)
    if not phrase_words or not text_words or len(phrase_words) > len(text_words):
        exact_possible = False
    else:
        exact_possible = True
    width = len(phrase_words)
    if exact_possible and any(text_words[index : index + width] == phrase_words for index in range(0, len(text_words) - width + 1)):
        return True
    phrase_key = football_resolver.normalize_key(phrase)
    if not phrase_key:
        return False
    max_width = min(6, len(text_words))
    for start in range(len(text_words)):
        for end in range(start + 1, min(len(text_words), start + max_width) + 1):
            if football_resolver.normalize_key(" ".join(text_words[start:end])) == phrase_key:
                return True
    return False


def _data_focus(plan: dict[str, Any] | None, route_action: str, request: str, has_player_candidate: bool = False) -> str | None:
    route = str(route_action or "").upper()
    planned = _text(plan, "data_focus")
    planned_stat = _text(plan, "stat_focus")
    lowered = _normalize_words_text(request)
    if _is_competition_start_request(lowered):
        return "season_start"
    explicit_focus = _explicit_data_focus_from_text(lowered, has_player_candidate=has_player_candidate)
    if explicit_focus is not None:
        return explicit_focus
    if planned:
        planned_key = planned.casefold()
        if planned_key in {"next_fixtures", "fixture_next", "next_match"} and _is_date_scoped_result_request(_normalize_words_text(request)):
            return "summary"
        if planned_key in {"next_fixtures", "fixture_next", "next_match"} and _is_competition_start_request(lowered):
            return "season_start"
        if planned_key == "season_start" and not _is_competition_start_request(lowered):
            return None
        if planned_key == "statistics" and planned_stat and _stat_focus_is_event(planned_stat) and not has_player_candidate:
            return "events"
        if planned_key == "statistics" and has_player_candidate:
            return "player_recent_stats"
        return planned_key
    if planned_stat and not has_player_candidate and _stat_focus_is_event(planned_stat):
        return "events"
    if has_player_candidate:
        if _has_any(lowered, ("transfer", "transfers", "fichaje", "fichajes")):
            return "player_transfers"
        if _has_any(lowered, ("lesion", "lesionado", "lesionados", "injury", "injuries")):
            return "player_injuries"
        if _has_any(lowered, ("ultimo equipo", "equipo anterior", "previous team", "last team")):
            return "player_previous_team"
        if _has_any(lowered, ("donde juega", "donde esta", "actualmente", "ahora", "current team", "where does")):
            return "player_current_team"
        if _has_any(lowered, ("historial", "carrera", "career")):
            return "player_career_history"
        if _has_any(lowered, ("trofeos", "titulos", "trophies")):
            return "player_trophies"
        if _has_any(lowered, ("sidelined", "bajas historicas", "sanciones", "suspensiones")):
            return "player_sidelined"
        if _has_any(lowered, ("estadistica", "stats", "statistics", "recientes", "recent")):
            return "player_recent_stats"
    if route == "FOOTBALL_PLAYER_QUERY":
        return "player"
    if _stat_focus_from_text(request):
        return "statistics"
    if route == "FOOTBALL_TABLE" or _has_any(lowered, ("tabla", "table", "standings", "posiciones", "clasificacion")):
        return "standings"
    if route == "FOOTBALL_TEAM_QUERY":
        return "team"
    if _has_any(lowered, ("asistencias", "top assists", "assists")):
        return "top_assists"
    if _has_any(lowered, ("amarillas", "yellow cards", "yellowcards")):
        return "top_yellow_cards"
    if _has_any(lowered, ("rojas", "red cards", "redcards")):
        return "top_red_cards"
    if _has_any(lowered, ("goleador", "goleadores", "top scorer", "scorers", "tabla de goleo")):
        return "scorers"
    if _has_any(lowered, ("lesion", "lesionado", "lesionados", "injury", "injuries")):
        return "injuries"
    if _has_any(lowered, ("transfer", "transfers", "fichaje", "fichajes")):
        return "transfers"
    if _has_any(lowered, ("prediccion", "prediction", "pronostico", "probabilidad")):
        return "prediction"
    if _has_any(lowered, ("odds", "cuotas", "momios", "bookmaker", "apuestas")):
        return "live_odds" if _has_any(lowered, ("live", "en vivo")) else "odds"
    if _has_any(lowered, ("penales", "shootout", "tanda")):
        return "shootout"
    if _has_any(lowered, ("primer tiempo", "first half", "segundo tiempo", "second half", "mitad")) and _has_any(lowered, ("estadistica", "stats", "statistics", "comparacion", "comparison")):
        return "half_statistics"
    if _has_any(lowered, ("rondas", "rounds", "jornada actual", "current round", "fase", "stage")):
        return "competition_rounds"
    if _has_any(lowered, ("tecnico", "entrenador", "coach", "dt")):
        return "current_coach"
    if _has_any(lowered, ("estadio", "venue", "stadium", "cancha")):
        return "venue"
    if _has_any(lowered, ("estadisticas de temporada", "team stats", "season statistics", "estadisticas del equipo")):
        return "team_season_statistics"
    if route == "FOOTBALL_COMPARISON" or _has_any(lowered, ("historial", "head to head", "h2h")):
        return "h2h"
    if _is_competition_start_request(lowered):
        return "season_start"
    if _is_date_scoped_result_request(lowered):
        return "summary"
    if _capability_marker_present(lowered, "next_fixtures"):
        return "next_fixtures"
    if _has_any(lowered, ("ultimo", "ultimos", "last match", "last game", "previous match")):
        return "last_fixtures"
    if _has_any(lowered, ("alineacion", "lineup", "lineups")):
        return "lineups"
    if _has_any(lowered, ("participaron", "participated", "jugadores que jugaron", "fixture players", "player match stats", "estadisticas de jugadores")):
        return "fixture_players"
    if _has_any(lowered, ("estadistica", "stats", "statistics")):
        return "statistics"
    if _has_any(lowered, ("gol", "goles", "events", "eventos", "quien metio")):
        return "events"
    if _is_result_request(lowered):
        return "summary"
    return None


def _route_action_for_focus(route_action: str, data_focus: str | None) -> str:
    route = str(route_action or "").upper()
    if route in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP"}:
        return route
    if data_focus == "standings":
        return "FOOTBALL_TABLE"
    if data_focus == "player":
        return "FOOTBALL_PLAYER_QUERY"
    if data_focus and str(data_focus).startswith("player_"):
        return "FOOTBALL_PLAYER_QUERY"
    if data_focus == "team":
        return "FOOTBALL_TEAM_QUERY"
    if data_focus in {"events", "lineups", "statistics", "fixture_players", "prediction", "predictions", "odds", "live_odds", "shootout", "half_statistics"}:
        return "FOOTBALL_MATCH_CENTER"
    if data_focus == "summary":
        return "FOOTBALL_SUMMARY"
    if data_focus == "summary" and route == "FOOTBALL_LOOKUP":
        return "FOOTBALL_SUMMARY"
    return route


def _operation_type(route_action: str, data_focus: str | None, request: str, has_player_candidate: bool = False) -> str:
    route = str(route_action or "").upper()
    focus = str(data_focus or "").casefold()
    if route == "FOOTBALL_LIVE_WATCH_START":
        return "live_watch_start"
    if route == "FOOTBALL_LIVE_WATCH_STOP":
        return "live_watch_stop"
    if focus == "standings" or route == "FOOTBALL_TABLE":
        return "standings"
    if focus in {"player_recent_stats", "player_stats"}:
        return "player_recent_stats"
    if focus == "player_current_team":
        return "player_current_team"
    if focus == "player_previous_team":
        return "player_previous_team"
    if focus == "player_career_history":
        return "player_career_history"
    if focus == "player_teams":
        return "player_teams"
    if focus == "player_transfers":
        return "player_transfers"
    if focus == "player_injuries":
        return "player_injuries"
    if focus == "player_trophies":
        return "player_trophies"
    if focus == "player_sidelined":
        return "player_sidelined"
    if focus == "player_match_stats":
        return "player_match_stats"
    if focus in {"team_season_statistics", "team_stats", "team_statistics"}:
        return "team_season_statistics"
    if focus in {"coach", "coach_profile"}:
        return "coach_profile"
    if focus in {"current_coach", "team_current_coach"}:
        return "team_current_coach"
    if focus == "coach_career":
        return "coach_career"
    if focus == "coach_trophies":
        return "coach_trophies"
    if focus == "coach_sidelined":
        return "coach_sidelined"
    if focus in {"venue", "stadium", "team_venue"}:
        return "team_venue"
    if focus == "player" or route == "FOOTBALL_PLAYER_QUERY":
        if _has_any(_normalize_words_text(request), ("reciente", "recientes", "estadistica", "stats", "statistics")):
            return "player_recent_stats"
        return "player_profile"
    if focus == "team" or route == "FOOTBALL_TEAM_QUERY":
        return "team_profile"
    if focus == "scorers":
        return "top_scorers"
    if focus in {"top_assists", "assists"}:
        return "top_assists"
    if focus in {"top_yellow_cards", "yellowcards", "yellow_cards"}:
        return "top_yellow_cards"
    if focus in {"top_red_cards", "redcards", "red_cards"}:
        return "top_red_cards"
    if focus == "injuries":
        return "player_injuries" if has_player_candidate else "team_injuries"
    if focus == "transfers":
        return "player_transfers" if has_player_candidate else "team_transfers"
    if focus in {"h2h", "comparison"} or route == "FOOTBALL_COMPARISON":
        return "h2h"
    if focus == "season_start":
        return "competition_structure"
    if focus == "next_fixtures":
        return "fixture_next"
    if focus == "last_fixtures":
        return "fixture_last"
    if focus == "events":
        return "fixture_events"
    if focus == "statistics":
        return "fixture_statistics"
    if focus == "lineups":
        return "fixture_lineups"
    if focus in {"fixture_players", "players", "participation"}:
        return "player_match_stats"
    if focus in {"prediction", "predictions"}:
        return "fixture_prediction"
    if focus in {"odds", "pre_match_odds"}:
        return "fixture_odds_pre_match"
    if focus in {"live_odds"}:
        return "fixture_odds_live"
    if focus in {"shootout", "shootout_attempts"}:
        return "fixture_shootout"
    if focus == "half_statistics":
        return "fixture_statistics_half_comparison"
    if focus in {"team_season_statistics", "team_stats", "team_statistics"}:
        return "team_season_statistics"
    if focus in {"competition_rounds", "rounds"}:
        return "competition_rounds"
    if focus in {"current_round", "competition_current_round"}:
        return "competition_current_round"
    if focus in {"round_fixtures", "competition_round_fixtures"}:
        return "competition_round_fixtures"
    if focus in {"competition_structure", "stage", "fase"}:
        return "competition_structure"
    if focus in {"coach", "coach_profile"}:
        return "coach_profile"
    if focus in {"current_coach", "team_current_coach"}:
        return "team_current_coach"
    if focus == "coach_career":
        return "coach_career"
    if focus == "coach_trophies":
        return "coach_trophies"
    if focus == "coach_sidelined":
        return "coach_sidelined"
    if focus in {"venue", "stadium", "team_venue"}:
        return "team_venue"
    if focus in {"countries", "football_countries"}:
        return "football_countries"
    if focus in {"timezones", "football_timezones"}:
        return "football_timezones"
    if focus == "league_seasons":
        return "league_seasons"
    if focus == "team_countries":
        return "team_countries"
    if focus in {"bookmakers", "odds_bookmakers"}:
        return "odds_bookmakers"
    if focus in {"bets", "odds_bets"}:
        return "odds_bets"
    if focus in {"live_bets", "odds_live_bets"}:
        return "odds_live_bets"
    if focus == "summary" or route in {"FOOTBALL_SUMMARY", "FOOTBALL_EXPLAIN_RESULT"} or _is_result_request(_normalize_words_text(request)):
        return "fixture_result"
    if route in {"FOOTBALL_MATCH_CENTER", "FOOTBALL_PREVIEW"}:
        return "fixture_live" if _is_live_request(request) else "fixture_events"
    if route == "FOOTBALL_WATCH_TODAY":
        return "fixture_live"
    if route == "FOOTBALL_FIXTURE_QUERY":
        return "fixture_live" if _is_live_request(request) else "fixture_next"
    if _is_live_request(request):
        return "fixture_live"
    return "unknown"


def _team_candidates_from_text(text: str) -> list[str]:
    candidates = extract_alias_candidates("team", text)
    for side in re.split(r"(?i)\b(?:vs|contra|versus)\b", text):
        side_cleaned = re.sub(
            r"(?i)\b(?:historial|h2h|head\s+to\s+head|partido|juego|goles|quien|qui[eé]n|metio|meti[oó]|de|del|la|el|los|las|ya|termin[oó]|termino|como|quedaron|quedo|resultado)\b",
            " ",
            side,
        )
        side_cleaned = re.sub(
            r"(?i)\b(?:cuantos?|cuantas?|cual|que|a|tabla|gol|van|va|lleva|llevan|tuvo|tuvieron|recibio|recibieron|fue|en|su|ultimo|ultima|pasado|pasada|ayer|hoy|ahora|ahorita|actual|tiros?|puerta|posesion|balon|amarillas?|rojas?|corners?|faltas?|pases?|acertados?|porcentaje)\b",
            " ",
            side_cleaned,
        )
        side_cleaned = re.sub(r"(?i)\b(?:lesiones?|lesionados?|injur(?:y|ies)|transferencias?|transfers?|fichajes?|recientes?)\b", " ", side_cleaned)
        if not looks_like_raw_sentence(side_cleaned, text):
            candidates.append(side_cleaned)
    return _dedupe(candidates)


def _player_candidates_from_text(parsed: football_resolver.PlayerQuery, route_action: str, data_focus: str | None, request: str) -> list[str]:
    if str(route_action or "").upper() == "FOOTBALL_COMPARISON":
        return []
    focus = str(data_focus or "").casefold()
    if (
        str(route_action or "").upper() != "FOOTBALL_PLAYER_QUERY"
        and focus not in {"player", "player_recent_stats", "player_current_team", "player_previous_team", "player_career_history", "player_transfers", "player_injuries"}
        and not _looks_like_player_entity_request(request)
    ):
        return []
    candidates = [_title_candidate(item) for item in parsed.candidates if not _looks_like_known_team_or_league(item)]
    return _dedupe(candidates)


def _planned_team_entity_focus(data_focus: str | None, team_candidates: list[str] | tuple[str, ...]) -> bool:
    if not team_candidates:
        return False
    focus = str(data_focus or "").casefold()
    return focus in {"team", "injuries", "team_injuries", "transfers", "team_transfers", "squad", "team_squad"}


def _has_player_focus(
    route_action: str,
    data_focus: str | None,
    player_candidates: list[str],
    team_candidates: list[str] | tuple[str, ...],
) -> bool:
    if not player_candidates:
        return False
    route = str(route_action or "").upper()
    focus = str(data_focus or "").casefold()
    if route == "FOOTBALL_PLAYER_QUERY" or focus == "player" or focus.startswith("player_"):
        return True
    return not bool(team_candidates)


def _looks_like_player_entity_request(text: str) -> bool:
    lowered = _normalize_words_text(text)
    if not _has_any(
        lowered,
        (
            "estadistica",
            "stats",
            "statistics",
            "reciente",
            "recientes",
            "donde juega",
            "equipo",
            "transfer",
            "fichaje",
            "lesion",
            "historial",
            "carrera",
        ),
    ):
        return False
    parsed = football_resolver.parse_player_query(text)
    return any(not looks_like_raw_sentence(candidate, text) and not _looks_like_known_team_or_league(candidate) for candidate in parsed.candidates)


def _looks_like_known_team_or_league(candidate: str) -> bool:
    key = football_resolver.normalize_key(candidate)
    return key in football_resolver.TEAM_ALIASES or key in football_resolver.LEAGUE_ALIASES


def _title_candidate(candidate: str) -> str:
    cleaned = " ".join(str(candidate or "").split())
    if not cleaned:
        return ""
    if cleaned.casefold() == cleaned:
        return " ".join(part.capitalize() for part in cleaned.split())
    return cleaned


def _player_candidate_from_prior_context(prior_context: str | None) -> str | None:
    if not prior_context:
        return None
    match = re.search(r'"player_name"\s*:\s*"([^"]+)"', prior_context)
    if match:
        return match.group(1)
    match = re.search(r"player(?:_found)?=([^;\n]+)", prior_context)
    if match:
        return match.group(1).strip()
    return None


def _league_candidate_from_prior_context(prior_context: str | None) -> str | None:
    if not prior_context or not prior_context.lstrip().startswith("{"):
        return None
    match = re.search(r'"league_name"\s*:\s*"([^"]+)"', prior_context)
    return match.group(1) if match else None


def _team_candidates_from_prior_context(prior_context: str | None) -> list[str]:
    if not prior_context or not prior_context.lstrip().startswith("{"):
        return []
    result: list[str] = []
    for key in ("team_name", "opponent_name"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', prior_context)
        if match:
            result.append(match.group(1))
    return _dedupe(result)


def _operation_type_from_prior_context(prior_context: str | None) -> str | None:
    if not prior_context or not prior_context.lstrip().startswith("{"):
        return None
    match = re.search(r'"operation_type"\s*:\s*"([^"]+)"', prior_context)
    return match.group(1) if match else None


def _request_has_date_correction(request: str) -> bool:
    lowered = _normalize_words_text(request)
    return _has_any(
        lowered,
        (
            "ayer",
            "yesterday",
            "hoy",
            "today",
            "mañana",
            "manana",
            "tomorrow",
            "me equivoque",
            "i was wrong",
            "era",
            "it was",
        ),
    )


def _request_is_date_correction_only(request: str) -> bool:
    lowered = _normalize_words_text(request)
    if not _request_has_date_correction(lowered):
        return False
    if _has_any(lowered, ("equipo", "team", "jugador", "player", "liga", "league", "competicion", "competition")):
        return False
    words = set(_words(lowered))
    allowed = {
        "ah",
        "a",
        "no",
        "hoy",
        "ayer",
        "manana",
        "mañana",
        "today",
        "yesterday",
        "tomorrow",
        "fue",
        "era",
        "was",
        "it",
        "actually",
        "wrong",
        "equivoque",
        "equivoqué",
        "quise",
        "decir",
        "me",
    }
    return bool(words) and words.issubset({football_resolver.normalize_key(item) for item in allowed})


def _is_date_scoped_result_request(normalized_text: str) -> bool:
    has_date = _has_any(normalized_text, ("hoy", "today", "ayer", "yesterday")) or bool(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", normalized_text))
    if not has_date:
        return False
    return _has_any(
        normalized_text,
        (
            "jugaron",
            "jugo",
            "se jugo",
            "played",
            "did play",
            "resultado",
            "result",
            "como quedo",
            "como quedaron",
            "ya termino",
            "final score",
        ),
    )


def _is_result_request(normalized_text: str) -> bool:
    return _has_any(
        normalized_text,
        ("ya termino", "ya termin", "como quedaron", "como quedo", "resultado", "result", "final score", "score final", "ended", "finish", "finished"),
    )


def _is_competition_start_request(normalized_text: str) -> bool:
    return _has_any(
        normalized_text,
        (
            "cuando inicia",
            "cuando empieza",
            "cuando arranca",
            "inicio de temporada",
            "empieza la temporada",
            "inicia la temporada",
            "start of season",
            "season start",
            "when does",
            "when starts",
            "when is the start",
        ),
    ) and _has_any(normalized_text, ("liga", "league", "cup", "championship", "competition", "competicion", "temporada", "season"))


def _explicit_data_focus_from_text(normalized_text: str, *, has_player_candidate: bool) -> str | None:
    if has_player_candidate and _has_any(normalized_text, ("ultimo equipo", "equipo anterior", "previous team", "last team", "donde juega", "where does")):
        return None
    if _has_any(normalized_text, ("tabla", "table", "standings", "posiciones", "clasificacion")):
        return "standings"
    if _has_any(normalized_text, ("estadisticas de temporada", "team stats", "season statistics", "estadisticas del equipo")):
        return "team_season_statistics"
    if not has_player_candidate and _stat_focus_from_text(normalized_text) and _has_any(
        normalized_text,
        ("recibio", "recibieron", "tuvo", "tuvieron", "ayer", "hoy", "ultimo", "ultima", "partido", "juego", "match", "game"),
    ):
        return "statistics"
    if _has_any(normalized_text, ("asistencias", "top assists", "assists")):
        return "top_assists"
    if _has_any(normalized_text, ("top yellow cards", "yellowcards", "tabla de amarillas")):
        return "top_yellow_cards"
    if _has_any(normalized_text, ("top red cards", "redcards", "tabla de rojas")):
        return "top_red_cards"
    if _has_any(normalized_text, ("goleador", "goleadores", "top scorer", "scorers", "tabla de goleo")):
        return "scorers"
    if _is_date_scoped_result_request(normalized_text) or _is_result_request(normalized_text):
        return "summary"
    if _has_any(normalized_text, ("prediccion", "prediction", "pronostico", "probabilidad")):
        return "prediction"
    if _has_any(normalized_text, ("odds", "cuotas", "momios", "bookmaker", "apuestas")):
        return "live_odds" if _has_any(normalized_text, ("live", "en vivo")) else "odds"
    if _has_any(normalized_text, ("alineacion", "lineup", "lineups", "once inicial", "starting eleven")):
        return "lineups"
    if _has_any(normalized_text, ("participaron", "participated", "jugadores que jugaron", "fixture players", "player match stats", "estadisticas de jugadores")):
        return "fixture_players"
    if not has_player_candidate and (_stat_focus_from_text(normalized_text) or _has_any(normalized_text, ("estadistica", "estadisticas", "stats", "statistics"))):
        return "statistics"
    if not has_player_candidate and _has_any(normalized_text, ("gol", "goles", "events", "eventos", "quien metio", "tarjetas", "cambios", "substitutions")):
        return "events"
    if _capability_marker_present(normalized_text, "next_fixtures"):
        return "next_fixtures"
    if _has_any(normalized_text, ("ultimo", "ultimos", "last match", "last game", "previous match")):
        return "last_fixtures"
    return None


def _is_live_request(text: str) -> bool:
    lowered = _normalize_words_text(text)
    return _has_any(lowered, ("ahora", "ahorita", "now", "live", "en vivo", "minuto a minuto", "pretemporada", "pre temporada", "preseason", "amistoso", "friendly"))


def _time_scope(plan: dict[str, Any] | None, request: str, operation_type: str) -> str | None:
    planned = _text(plan, "time_scope")
    if planned:
        key = planned.casefold()
        aliases = {
            "live": "live",
            "today": "today",
            "hoy": "today",
            "yesterday": "yesterday",
            "ayer": "yesterday",
            "tomorrow": "tomorrow",
            "mañana": "tomorrow",
            "manana": "tomorrow",
            "last_finished_match": "last_finished_match",
            "previous_match": "previous_match",
            "specific_date": "specific_date",
            "recent_finished": "recent_finished",
            "next_match": "next_match",
        }
        if key in aliases:
            return aliases[key]
    lowered = _normalize_words_text(request)
    if _has_any(lowered, ("ayer", "yesterday")):
        return "yesterday"
    if _has_any(lowered, ("hoy", "today")):
        return "today"
    if _has_any(lowered, ("mañana", "manana", "tomorrow")):
        return "tomorrow"
    if _has_any(lowered, ("ultimo partido", "ultimo juego", "su ultimo", "last match", "last game", "juego pasado", "partido pasado", "previous match")):
        return "last_finished_match"
    if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", lowered):
        return "specific_date"
    if _is_live_request(request):
        return "live"
    if operation_type == "fixture_statistics" and _has_any(lowered, ("tuvo", "tuvieron", "fue", "recibio", "recibieron")):
        return "recent_finished"
    if operation_type == "fixture_next":
        return "next_match"
    return "today" if operation_type in {"fixture_live", "fixture_statistics", "fixture_events", "fixture_lineups"} else None


def _capability_intent_from_request(
    request: str,
    *,
    operation_type: str,
    data_focus: str | None,
    time_scope: str | None,
    plan: dict[str, Any] | None,
) -> FootballCapabilityIntent:
    temporal = time_scope
    if operation_type == "fixture_next":
        temporal = "future"
    elif operation_type in {"fixture_result", "team_fixture_result", "league_fixture_results"}:
        temporal = temporal or "past_or_date"
    elif operation_type == "fixture_live":
        temporal = "live"
    evidence = _capability_evidence(request, data_focus, operation_type)
    planner_operation = _text(plan, "operation") or _text(plan, "intent")
    planner_data_focus = _text(plan, "data_focus")
    return FootballCapabilityIntent(
        operation_family=operation_type,
        data_focus=data_focus,
        temporal_semantics=temporal,
        requested_subscope=_requested_subscope_for_focus(data_focus, request),
        evidence=evidence,
        planner_operation=planner_operation,
        planner_data_focus=planner_data_focus,
        planner_accepted=bool(planner_data_focus and str(planner_data_focus).casefold() == str(data_focus or "").casefold()),
    )


def _capability_evidence(request: str, data_focus: str | None, operation_type: str) -> str | None:
    lowered = _normalize_words_text(request)
    focus = str(data_focus or "").casefold()
    for key, markers in _CAPABILITY_MARKERS.items():
        if focus == key or operation_type.startswith(key):
            for marker in markers:
                if marker in lowered:
                    return marker
    return None


def _capability_marker_present(normalized_text: str, focus: str) -> bool:
    return _has_any(normalized_text, _CAPABILITY_MARKERS.get(focus, ()))


def _requested_subscope_for_focus(data_focus: str | None, request: str) -> str | None:
    lowered = _normalize_words_text(request)
    focus = str(data_focus or "").casefold()
    if focus == "shootout" and _has_any(lowered, ("tirador", "cobrador", "attempt", "intento")):
        return "shootout_attempts"
    if focus == "shootout":
        return "shootout_aggregate"
    if focus in {"odds", "live_odds"}:
        return focus
    return None


def _stat_focus_from_text(text: str) -> str | None:
    lowered = _normalize_words_text(text)
    patterns = (
        (("tiros a gol", "tiro a gol", "tiros a puerta", "tiro a puerta", "remates a puerta"), "shots_on_goal"),
        (("tiros fuera", "remates fuera"), "shots_off_goal"),
        (("tiros bloqueados", "remates bloqueados"), "blocked_shots"),
        (("tiros", "remates", "total shots"), "total_shots"),
        (("posesion del balon", "posesion", "possession"), "ball_possession"),
        (("corners", "corner", "tiros de esquina"), "corner_kicks"),
        (("faltas", "fouls"), "fouls"),
        (("offsides", "fuera de lugar", "fueras de lugar"), "offsides"),
        (("atajadas", "salvadas", "goalkeeper saves"), "goalkeeper_saves"),
        (("pases acertados", "pases completados"), "passes_accurate"),
        (("porcentaje de pases", "precision de pase"), "passes_percent"),
        (("pases", "passes"), "total_passes"),
        (("amarillas", "tarjetas amarillas", "yellow cards"), "yellow_cards"),
        (("rojas", "tarjetas rojas", "red cards"), "red_cards"),
    )
    for markers, key in patterns:
        if _has_any(lowered, markers):
            return key
    return None


def _stat_focus_is_event(value: str | None) -> bool:
    lowered = _normalize_words_text(str(value or ""))
    key = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return key in {
        "gol",
        "goles",
        "goal",
        "goals",
        "tarjeta",
        "tarjetas",
        "card",
        "cards",
        "amarilla",
        "amarillas",
        "roja",
        "rojas",
        "substitution",
        "substitutions",
        "cambio",
        "cambios",
    }


def _has_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _normalize_words_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value or "").split())
        key = football_resolver.normalize_key(cleaned)
        if cleaned and key and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


def season_hint_to_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(20\d{2})\b", value)
    if match:
        return int(match.group(1))
    return None


def season_hint_to_year(value: str | None, current_season: int | None) -> int | None:
    explicit = season_hint_to_int(value)
    if explicit is not None:
        return explicit
    if not isinstance(current_season, int):
        return None
    normalized = football_resolver.normalize_key(value)
    if any(marker in normalized for marker in ("torneopasado", "temporadapasada", "lastseason", "previousseason")):
        return current_season - 1
    if any(marker in normalized for marker in ("torneoactual", "temporadaactual", "currentseason")):
        return current_season
    return None


def _list(plan: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(plan, dict):
        return []
    values = plan.get(key)
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values[:8]:
        if isinstance(item, str):
            cleaned = " ".join(item.split())
            if cleaned:
                result.append(cleaned)
    return result


def _text(plan: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(plan, dict):
        return None
    value = plan.get(key)
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:160]
    if not cleaned or looks_like_raw_sentence(cleaned, None):
        return None
    return cleaned


def _words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.casefold())
