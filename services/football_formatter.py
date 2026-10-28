from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def fixture_status(fixture: dict[str, Any]) -> str:
    status = fixture.get("status")
    if not isinstance(status, dict):
        return "N/A"
    short = str(status.get("short", "") or "").strip()
    long = str(status.get("long", "") or "").strip()
    elapsed = status.get("elapsed")
    if isinstance(elapsed, int):
        return f"{short or long} {elapsed}'".strip()
    return short or long or "N/A"


def fixture_round(item: dict[str, Any]) -> str:
    league = item.get("league")
    if isinstance(league, dict):
        round_name = league.get("round")
        if isinstance(round_name, str) and round_name.strip():
            return round_name.strip()
    return ""


def fixture_teams(item: dict[str, Any]) -> tuple[str, str]:
    teams = item.get("teams")
    if not isinstance(teams, dict):
        return "Unknown", "Unknown"
    home = teams.get("home")
    away = teams.get("away")
    home_name = home.get("name") if isinstance(home, dict) else "Unknown"
    away_name = away.get("name") if isinstance(away, dict) else "Unknown"
    return str(home_name or "Unknown"), str(away_name or "Unknown")


def fixture_team_logos(item: dict[str, Any]) -> tuple[str | None, str | None]:
    teams = item.get("teams")
    if not isinstance(teams, dict):
        return None, None
    home = teams.get("home")
    away = teams.get("away")
    home_logo = home.get("logo") if isinstance(home, dict) else None
    away_logo = away.get("logo") if isinstance(away, dict) else None
    return _clean_url(home_logo), _clean_url(away_logo)


def fixture_score(item: dict[str, Any]) -> str:
    goals = item.get("goals")
    if not isinstance(goals, dict):
        return "vs"
    home = goals.get("home")
    away = goals.get("away")
    if home is None and away is None:
        return "vs"
    return f"{home if home is not None else '-'} - {away if away is not None else '-'}"


def fixture_datetime(item: dict[str, Any]) -> str:
    fixture = item.get("fixture")
    if not isinstance(fixture, dict):
        return "N/A"
    iso = fixture.get("date")
    if isinstance(iso, str) and iso.strip():
        cleaned = iso.strip()
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return cleaned.replace("T", " ").replace("+00:00", " UTC")
        label = _timezone_label(parsed)
        return f"{parsed.strftime('%Y-%m-%d %H:%M')} {label}".strip()
    return "N/A"


def _timezone_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset == timedelta(hours=-6):
        return "CST"
    if offset == timedelta(hours=-5):
        return "CDT"
    if offset == timedelta(0):
        return "UTC"
    name = value.tzname()
    return name or ""


def format_fixture_line(item: dict[str, Any]) -> tuple[str, str]:
    fixture = item.get("fixture")
    if not isinstance(fixture, dict):
        fixture = {}
    home, away = fixture_teams(item)
    score = fixture_score(item)
    status = fixture_status(fixture)
    round_name = fixture_round(item)
    title = f"{home} vs {away}"
    details = f"**{score}**\n{status}"
    if round_name:
        details = f"{details}\n{round_name}"
    return title, details


def _clean_url(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
