from __future__ import annotations

from typing import Any

from services.football_formatter import fixture_datetime, fixture_score, fixture_status, fixture_teams



def _compact_fixture(item: dict[str, Any]) -> dict[str, Any]:
    fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
    league = item.get("league") if isinstance(item.get("league"), dict) else {}
    teams = item.get("teams") if isinstance(item.get("teams"), dict) else {}
    home, away = fixture_teams(item)
    return {
        "fixture_id": fixture.get("id"),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "league": league.get("name"),
        "round": league.get("round"),
        "home": home,
        "away": away,
        "home_id": teams["home"].get("id") if isinstance(teams.get("home"), dict) else None,
        "away_id": teams["away"].get("id") if isinstance(teams.get("away"), dict) else None,
        "score": fixture_score(item),
        "status": fixture_status(fixture),
        "date": fixture_datetime(item),
    }



def _compact_standing(item: dict[str, Any]) -> dict[str, Any]:
    team = item.get("team") if isinstance(item.get("team"), dict) else {}
    all_stats = item.get("all") if isinstance(item.get("all"), dict) else {}
    return {
        "rank": item.get("rank"),
        "team": team.get("name"),
        "points": item.get("points"),
        "team_id": team.get("id"),
        "played": all_stats.get("played"),
        "goalsDiff": item.get("goalsDiff"),
        "form": item.get("form"),
        **{key: item[key] for key in ("group", "description", "update") if key in item},
        **{key: all_stats[key] for key in ("win", "draw", "lose", "goals") if key in all_stats},
    }
