from __future__ import annotations

import json
from typing import Any

from services.football_formatter import fixture_datetime, fixture_score, fixture_status, fixture_teams


def build_fixture_context(
    *,
    label: str,
    fixtures: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    statistics: list[dict[str, Any]] | None = None,
    lineups: list[dict[str, Any]] | None = None,
    source_endpoints: list[str] | None = None,
) -> str:
    payload = {
        "label": label,
        "fixtures": [_compact_fixture(item) for item in fixtures[:5]],
        "events": [_compact_event(item) for item in (events or [])[:20]],
        "statistics": (statistics or [])[:8],
        "lineups": [_compact_lineup(item) for item in (lineups or [])[:4]],
        "source_endpoints": source_endpoints or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]


def build_team_context(
    *,
    team: dict[str, Any],
    standings_row: dict[str, Any] | None = None,
    fixtures: list[dict[str, Any]] | None = None,
    source_endpoints: list[str] | None = None,
) -> str:
    team_info = team.get("team") if isinstance(team, dict) else {}
    payload = {
        "team": team_info if isinstance(team_info, dict) else {},
        "standing": standings_row or {},
        "fixtures": [_compact_fixture(item) for item in (fixtures or [])[:5]],
        "source_endpoints": source_endpoints or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]


def build_standings_context(
    *,
    label: str,
    standings: list[dict[str, Any]],
    source_endpoints: list[str] | None = None,
) -> str:
    payload = {
        "label": label,
        "standings": [_compact_standing(item) for item in standings[:24]],
        "source_endpoints": source_endpoints or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]


def build_player_context(
    *,
    label: str,
    player_row: dict[str, Any] | None,
    stat_focus: str | None = None,
    fixtures: list[dict[str, Any]] | None = None,
    extra_label: str | None = None,
    extra_rows: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    source_endpoints: list[str] | None = None,
) -> str:
    player = player_row.get("player") if isinstance(player_row, dict) else {}
    stats = player_row.get("statistics") if isinstance(player_row, dict) else []
    payload = {
        "label": label,
        "player": player if isinstance(player, dict) else {},
        "statistics": stats[:3] if isinstance(stats, list) else [],
        "stat_focus": stat_focus,
        "fixtures": [_compact_fixture(item) for item in (fixtures or [])[:3]],
        "extra_label": extra_label,
        "extra_rows": (extra_rows or [])[:5],
        "notes": notes or [],
        "source_endpoints": source_endpoints or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]


def football_grounding_prompt(question: str, context: str) -> str:
    return (
        f"{question}\n\n"
        "[TRUSTED_FOOTBALL_DATA]\n"
        f"{context}\n\n"
        "Answer using only the bracketed football block, but do not mention the block, evidence labels, "
        "or internal provenance wording to the user. If a score, lineup, injury, transfer, odds, "
        "or live status is missing, say naturally that the specific detail does not appear right now "
        "instead of using a stock availability label. Do not invent missing football facts."
    )


def _compact_fixture(item: dict[str, Any]) -> dict[str, Any]:
    fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
    league = item.get("league") if isinstance(item.get("league"), dict) else {}
    home, away = fixture_teams(item)
    return {
        "fixture_id": fixture.get("id"),
        "league": league.get("name"),
        "round": league.get("round"),
        "home": home,
        "away": away,
        "score": fixture_score(item),
        "status": fixture_status(fixture),
        "date": fixture_datetime(item),
    }


def _compact_event(item: dict[str, Any]) -> dict[str, Any]:
    team = item.get("team") if isinstance(item.get("team"), dict) else {}
    player = item.get("player") if isinstance(item.get("player"), dict) else {}
    return {
        "time": item.get("time"),
        "team": team.get("name"),
        "player": player.get("name"),
        "type": item.get("type"),
        "detail": item.get("detail"),
    }


def _compact_lineup(item: dict[str, Any]) -> dict[str, Any]:
    team = item.get("team") if isinstance(item.get("team"), dict) else {}
    coach = item.get("coach") if isinstance(item.get("coach"), dict) else {}
    return {
        "team": team.get("name"),
        "formation": item.get("formation"),
        "coach": coach.get("name"),
    }


def _compact_standing(item: dict[str, Any]) -> dict[str, Any]:
    team = item.get("team") if isinstance(item.get("team"), dict) else {}
    all_stats = item.get("all") if isinstance(item.get("all"), dict) else {}
    return {
        "rank": item.get("rank"),
        "team": team.get("name"),
        "points": item.get("points"),
        "played": all_stats.get("played"),
        "goalsDiff": item.get("goalsDiff"),
        "form": item.get("form"),
    }
