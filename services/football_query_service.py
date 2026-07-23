from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata
from typing import Any

from services import football_resolver


@dataclass(frozen=True)
class FootballQueryOperation:
    route_action: str
    data_focus: str | None
    league_candidates: tuple[str, ...]
    team_candidates: tuple[str, ...]
    player_candidates: tuple[str, ...]
    fixture_focus: str | None
    stat_focus: str | None
    date_hint: str | None
    season_hint: str | None
    live: bool


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
    "lesiones",
    "lesionados",
    "transferencias",
    "historial",
    "informacion",
}


def build_operation(route_action: str, request: str, plan: dict[str, Any] | None) -> FootballQueryOperation:
    data_focus = _text(plan, "data_focus")
    return FootballQueryOperation(
        route_action=str(route_action or "").upper(),
        data_focus=data_focus.casefold() if data_focus else None,
        league_candidates=tuple(clean_entity_candidates("league", _list(plan, "league_candidates"), request)),
        team_candidates=tuple(clean_entity_candidates("team", _list(plan, "team_candidates"), request)),
        player_candidates=tuple(clean_entity_candidates("player", _list(plan, "player_candidates"), request)),
        fixture_focus=_text(plan, "fixture_focus"),
        stat_focus=_text(plan, "stat_focus"),
        date_hint=_text(plan, "date_hint"),
        season_hint=_text(plan, "season_hint"),
        live=bool(plan.get("live", False)) if isinstance(plan, dict) else False,
    )


def clean_entity_candidates(kind: str, candidates: list[str] | tuple[str, ...], original_request: str) -> list[str]:
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
    normalized = football_resolver.normalize_key(text)
    if not normalized:
        return []
    aliases = (
        football_resolver.TEAM_ALIASES
        if kind == "team"
        else football_resolver.LEAGUE_ALIASES
        if kind == "league"
        else football_resolver.PLAYER_SEED_ALIASES
    )
    candidates: list[str] = []
    for alias, canonical in aliases.items():
        if alias and alias in normalized:
            candidates.append(canonical)
    return clean_entity_candidates(kind, candidates, text)


def season_hint_to_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(20\d{2})\b", value)
    if match:
        return int(match.group(1))
    return None


def _list(plan: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(plan, dict):
        return []
    values = plan.get(key)
    if not isinstance(values, list):
        return []
    return [" ".join(str(item or "").split()) for item in values[:8]]


def _text(plan: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(plan, dict):
        return None
    cleaned = " ".join(str(plan.get(key) or "").split())[:160]
    if not cleaned or looks_like_raw_sentence(cleaned, None):
        return None
    return cleaned


def _words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.casefold())
