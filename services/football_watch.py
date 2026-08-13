from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.football_formatter import fixture_score, fixture_status, fixture_teams
from services.football_live_match_service import match_event_key, normalize_shootout, stats_for_watch


WATCH_CHECKPOINT_MINUTES = (15, 30, 60, 75)
TERMINAL_STATUS_SHORTS = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "PST"}


@dataclass(frozen=True)
class FootballWatchSnapshot:
    score: str
    status: str
    status_short: str
    elapsed: int | None
    stats: dict[str, dict[str, float]] = field(default_factory=dict)
    event_keys: tuple[str, ...] = ()
    shootout_attempt_keys: tuple[str, ...] = ()
    shootout_score: str = ""


@dataclass(frozen=True)
class FootballWatchUpdate:
    update_type: str
    text: str
    event_key: str | None = None
    checkpoint: str | None = None
    needs_send: bool = True


def snapshot_from_fixture(
    fixture: dict[str, Any],
    *,
    statistics: list[dict[str, Any]] | None = None,
) -> FootballWatchSnapshot:
    fixture_obj = fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {}
    status_obj = fixture_obj.get("status") if isinstance(fixture_obj.get("status"), dict) else {}
    status = fixture_status(fixture_obj)
    return FootballWatchSnapshot(
        score=fixture_score(fixture),
        status=status,
        status_short=str(status_obj.get("short") or status.split()[0] if status else "").upper(),
        elapsed=status_obj.get("elapsed") if isinstance(status_obj.get("elapsed"), int) else None,
        stats=stats_for_watch(statistics or []),
        shootout_score=_shootout_score(fixture),
    )


def should_fetch_lineups(snapshot: FootballWatchSnapshot, *, lineups_fetched: bool) -> bool:
    if lineups_fetched:
        return False
    if snapshot.status_short in {"NS", "TBD"}:
        return True
    return snapshot.status_short == "1H" and isinstance(snapshot.elapsed, int) and snapshot.elapsed <= 10


def should_fetch_statistics(
    snapshot: FootballWatchSnapshot,
    *,
    emitted_checkpoints: set[str],
) -> bool:
    checkpoint = checkpoint_for_snapshot(snapshot)
    return checkpoint is not None and checkpoint not in emitted_checkpoints


def checkpoint_for_snapshot(snapshot: FootballWatchSnapshot) -> str | None:
    if snapshot.status_short in {"HT", "FT", "AET", "PEN"}:
        return snapshot.status_short
    if not isinstance(snapshot.elapsed, int):
        return None
    for minute in reversed(WATCH_CHECKPOINT_MINUTES):
        if snapshot.elapsed >= minute:
            return str(minute)
    return None


def is_terminal_status(status: str) -> bool:
    short = status.split()[0].upper() if status else ""
    return short in TERMINAL_STATUS_SHORTS


def build_watch_updates(
    *,
    previous: FootballWatchSnapshot | None,
    current: FootballWatchSnapshot,
    fixture: dict[str, Any],
    events: list[dict[str, Any]],
    seen_event_keys: set[str],
    emitted_checkpoints: set[str],
    statistics: list[dict[str, Any]] | None = None,
    lineups: list[dict[str, Any]] | None = None,
) -> tuple[list[FootballWatchUpdate], FootballWatchSnapshot]:
    updates: list[FootballWatchUpdate] = []
    ordered_events = [event for event in events[:80] if isinstance(event, dict)]
    ordered_keys = tuple(event_key(event, index=index) for index, event in enumerate(ordered_events))
    for index, event in enumerate(ordered_events):
        key = event_key(event, index=index)
        if key in seen_event_keys or not _event_is_meaningful(event):
            continue
        text = format_event_update(event, fixture)
        if text:
            updates.append(FootballWatchUpdate("event", text, event_key=key))

    entered_shootout = previous is not None and previous.status_short != "P" and current.status_short == "P"
    pre_shootout_keys = set(previous.event_keys if previous is not None else ordered_keys)
    shootout = normalize_shootout(
        fixture,
        ordered_events,
        pre_shootout_event_keys=pre_shootout_keys,
        live_entered_shootout=entered_shootout or (previous is not None and previous.status_short == "P" and current.status_short == "P"),
    )
    prior_attempt_keys = set(previous.shootout_attempt_keys if previous is not None else ())
    current_attempt_keys = tuple(item.attempt_key for item in shootout.attempts)
    for attempt in shootout.attempts:
        if attempt.attempt_key in prior_attempt_keys or attempt.attempt_key in seen_event_keys:
            continue
        outcome = "anota" if attempt.scored else "falla"
        label = attempt.player_name or "Jugador"
        team = f" ({attempt.team_name})" if attempt.team_name else ""
        score = _shootout_score(fixture)
        updates.append(FootballWatchUpdate("shootout", f"Penales: {label}{team} {outcome}. {score}".strip(), event_key=attempt.attempt_key))
    if previous is not None and current.shootout_score != previous.shootout_score and current.status_short in {"P", "PEN"} and not any(item.update_type == "shootout" for item in updates):
        updates.append(FootballWatchUpdate("shootout", f"Penales: {_fixture_label(fixture)} {current.shootout_score}".strip()))

    if lineups:
        lineups_text = _format_lineups_update(lineups)
        if lineups_text and "lineups" not in emitted_checkpoints:
            updates.append(FootballWatchUpdate("lineups", lineups_text, checkpoint="lineups"))

    if previous is not None and current.score != previous.score and not any(item.update_type == "event" for item in updates):
        updates.append(FootballWatchUpdate("score", f"{_fixture_label(fixture)}: {current.score} ({current.status})"))

    phase_added = False
    if previous is not None and _status_changed_meaningfully(previous.status_short, current.status_short):
        updates.append(FootballWatchUpdate("phase", f"{_phase_label(current.status_short)}: {_fixture_label(fixture)} {current.score}"))
        phase_added = True

    checkpoint = checkpoint_for_snapshot(current)
    if checkpoint and checkpoint not in emitted_checkpoints and not (phase_added and checkpoint in {"HT", "FT", "AET", "PEN"}):
        checkpoint_text = _format_checkpoint_update(checkpoint, fixture, current, previous)
        if checkpoint_text:
            updates.append(FootballWatchUpdate("checkpoint", checkpoint_text, checkpoint=checkpoint))

    return [item for item in updates if item.needs_send and item.text.strip()], FootballWatchSnapshot(
        score=current.score,
        status=current.status,
        status_short=current.status_short,
        elapsed=current.elapsed,
        stats=current.stats,
        event_keys=ordered_keys,
        shootout_attempt_keys=current_attempt_keys,
        shootout_score=current.shootout_score,
    )


def event_key(event: dict[str, Any], *, index: int = 0) -> str:
    return match_event_key(event, index)


def format_event_update(event: dict[str, Any], fixture: dict[str, Any]) -> str:
    time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
    team = event.get("team") if isinstance(event.get("team"), dict) else {}
    player = event.get("player") if isinstance(event.get("player"), dict) else {}
    assist = event.get("assist") if isinstance(event.get("assist"), dict) else {}
    elapsed = time_info.get("elapsed")
    extra = time_info.get("extra")
    minute = f"{elapsed}'" if isinstance(elapsed, int) else ""
    if isinstance(extra, int):
        minute = f"{elapsed}+{extra}'" if isinstance(elapsed, int) else f"+{extra}'"
    event_type = str(event.get("type") or "").casefold()
    detail = str(event.get("detail") or "")
    detail_key = detail.casefold()
    team_name = str(team.get("name") or "Equipo")
    player_name = str(player.get("name") or "Jugador")
    score = fixture_score(fixture)

    if event_type == "goal":
        if "missed" in detail_key:
            label = "Penal fallado" if "pen" in detail_key else "Gol anulado"
            return f"{label} {minute} {team_name} - {player_name}. {score}".strip()
        if "own" in detail_key:
            label = "Autogol"
        elif "pen" in detail_key:
            label = "Gol de penal"
        else:
            label = "Gol"
        assist_text = f" (asistencia: {assist.get('name')})" if assist.get("name") else ""
        impact = _goal_impact(event, fixture)
        impact_text = f" {impact}." if impact else ""
        return f"{label} {minute} {team_name} - {player_name}{assist_text}. {team_name} {score}.{impact_text}".strip()

    if event_type == "card":
        card = "Roja" if "red" in detail_key else "Amarilla"
        return f"{card} {minute} {team_name} - {player_name}.".strip()

    if event_type == "subst":
        assist_name = str(assist.get("name") or "").strip()
        if assist_name:
            return f"Cambio {minute} {team_name}: entra {assist_name}, sale {player_name}.".strip()
        return f"Cambio {minute} {team_name}: {player_name}.".strip()

    if "var" in event_type or "var" in detail_key:
        return f"VAR {minute} {team_name}: {detail or player_name}.".strip()

    return ""


def _event_is_meaningful(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").casefold()
    detail = str(event.get("detail") or "").casefold()
    return event_type in {"goal", "card", "subst"} or "var" in event_type or "var" in detail


def _status_changed_meaningfully(previous: str, current: str) -> bool:
    return previous != current and current in {"1H", "HT", "2H", "ET", "BT", "P", "FT", "AET", "PEN", "CANC", "ABD", "PST", "SUSP", "INT", "AWD", "WO"}


def _phase_label(status_short: str) -> str:
    return {
        "1H": "Arranca",
        "HT": "Medio tiempo",
        "2H": "Segundo tiempo",
        "ET": "Tiempo extra",
        "BT": "Descanso del extra",
        "P": "Penales",
        "FT": "Final",
        "AET": "Final en tiempos extra",
        "PEN": "Final por penales",
        "SUSP": "Suspendido",
        "INT": "Interrumpido",
        "CANC": "Cancelado",
        "ABD": "Suspendido",
        "AWD": "Adjudicado",
        "WO": "Walkover",
        "PST": "Pospuesto",
    }.get(status_short, status_short)


def _fixture_label(fixture: dict[str, Any]) -> str:
    home, away = fixture_teams(fixture)
    return f"{home} vs {away}"


def _shootout_score(fixture: dict[str, Any]) -> str:
    score = fixture.get("score") if isinstance(fixture.get("score"), dict) else {}
    penalty = score.get("penalty") if isinstance(score.get("penalty"), dict) else {}
    home = penalty.get("home")
    away = penalty.get("away")
    if isinstance(home, int) or isinstance(away, int):
        return f"Shootout: {home if isinstance(home, int) else '-'}-{away if isinstance(away, int) else '-'}"
    return ""


def _goal_impact(event: dict[str, Any], fixture: dict[str, Any]) -> str:
    team = event.get("team") if isinstance(event.get("team"), dict) else {}
    teams = fixture.get("teams") if isinstance(fixture.get("teams"), dict) else {}
    goals = fixture.get("goals") if isinstance(fixture.get("goals"), dict) else {}
    home_team = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away_team = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    home_goals = goals.get("home")
    away_goals = goals.get("away")
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        return ""
    team_id = team.get("id")
    is_home = team_id is not None and team_id == home_team.get("id")
    if team_id is None:
        is_home = str(team.get("name") or "") == str(home_team.get("name") or "")
    scored = home_goals if is_home else away_goals
    conceded = away_goals if is_home else home_goals
    previous_scored = max(0, scored - 1)
    previous_conceded = conceded
    if previous_scored == 0 and previous_conceded == 0:
        return "Abre el marcador"
    if previous_scored < previous_conceded and scored == conceded:
        return "Empata el partido"
    if previous_scored < previous_conceded and scored > conceded:
        return "Remonta el partido"
    if previous_scored > previous_conceded and scored > conceded:
        return "Aumenta la ventaja"
    elapsed = event.get("time", {}).get("elapsed") if isinstance(event.get("time"), dict) else None
    if isinstance(elapsed, int) and elapsed >= 85 and scored > conceded and scored - conceded == 1:
        return "Puede ser gol decisivo"
    if scored < conceded:
        return "Descuenta"
    return ""


def _format_lineups_update(lineups: list[dict[str, Any]]) -> str:
    teams: list[str] = []
    for lineup in lineups[:2]:
        team = lineup.get("team") if isinstance(lineup, dict) else {}
        name = str(team.get("name") or "").strip()
        formation = str(lineup.get("formation") or "").strip()
        if name and formation:
            teams.append(f"{name} {formation}")
    if len(teams) >= 2:
        return "Formaciones: " + ", ".join(teams) + "."
    return ""


def _format_checkpoint_update(
    checkpoint: str,
    fixture: dict[str, Any],
    current: FootballWatchSnapshot,
    previous: FootballWatchSnapshot | None,
) -> str:
    if checkpoint in {"HT", "FT", "AET", "PEN"}:
        prefix = _phase_label(checkpoint)
        return f"{prefix}: {_fixture_label(fixture)} {current.score}."
    momentum = _momentum_summary(current, previous)
    if momentum:
        return f"{checkpoint}': {_fixture_label(fixture)} {current.score}. {momentum}"
    return ""


def _momentum_summary(current: FootballWatchSnapshot, previous: FootballWatchSnapshot | None) -> str:
    if not current.stats:
        return ""
    parts: list[str] = []
    names = list(current.stats.keys())
    if len(names) >= 2:
        a_name, b_name = names[0], names[1]
        a_stats, b_stats = current.stats[a_name], current.stats[b_name]
        shot_delta = abs(a_stats.get("shots", 0.0) - b_stats.get("shots", 0.0))
        target_delta = abs(a_stats.get("shots_on_target", 0.0) - b_stats.get("shots_on_target", 0.0))
        corner_delta = abs(a_stats.get("corners", 0.0) - b_stats.get("corners", 0.0))
        possession_delta = abs(a_stats.get("possession", 0.0) - b_stats.get("possession", 0.0))
        leader = a_name
        if b_stats.get("shots", 0.0) > a_stats.get("shots", 0.0):
            leader = b_name
        if shot_delta >= 3 or target_delta >= 2:
            parts.append(f"{leader} esta llegando mas")
        if corner_delta >= 2:
            corner_leader = a_name if a_stats.get("corners", 0.0) >= b_stats.get("corners", 0.0) else b_name
            parts.append(f"{corner_leader} fuerza mas corners")
        if possession_delta >= 10:
            possession_leader = a_name if a_stats.get("possession", 0.0) >= b_stats.get("possession", 0.0) else b_name
            parts.append(f"{possession_leader} trae mas posesion")
    return "; ".join(parts[:2]) + ("." if parts else "")
