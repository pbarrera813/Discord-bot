from __future__ import annotations

import asyncio
from functools import lru_cache
from io import BytesIO
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from utils.i18n import tr


_PROVIDER_TERMS_ES = {
    "missing fixture": "Baja", "questionable": "En duda", "loan": "Préstamo",
    "transfer": "Traspaso", "free agent": "Agente libre", "return from loan": "Fin de préstamo",
    "home": "Local", "away": "Visitante", "draw": "Empate", "over": "Más de", "under": "Menos de",
    "yes": "Sí", "no": "No", "match winner": "Ganador del partido", "total goals": "Total de goles",
    "goals over/under": "Total de goles", "asian handicap": "Hándicap asiático",
    "winner": "Ganador", "2nd place": "Segundo lugar", "injury": "Lesión",
    "grass": "Césped natural", "artificial turf": "Césped artificial",
    "goalkeeper": "Portero", "defender": "Defensa", "midfielder": "Mediocampista", "attacker": "Delantero",
    "g": "POR", "d": "DEF", "m": "MED", "f": "DEL",
    "goal": "Gol", "card": "Tarjeta", "subst": "Cambio", "substitution": "Cambio",
    "normal goal": "Gol", "own goal": "Autogol", "penalty": "Penal", "missed penalty": "Penal fallado",
    "yellow card": "Tarjeta amarilla", "second yellow card": "Segunda tarjeta amarilla", "red card": "Tarjeta roja",
    "goal cancelled": "Gol anulado", "goal confirmed": "Gol confirmado", "penalty confirmed": "Penal confirmado",
    "penalty cancelled": "Penal anulado", "red card cancelled": "Tarjeta roja anulada",
    "red card confirmed": "Tarjeta roja confirmada", "card upgrade": "Cambio de tarjeta",
    "foul": "Falta", "handball": "Mano", "time wasting": "Pérdida de tiempo", "argument": "Protesta",
    "simulation": "Simulación", "persistent infringement": "Faltas reiteradas", "violent conduct": "Conducta violenta",
    "professional foul last man": "Falta al último hombre", "professional foul handball": "Mano deliberada",
    "unallowed field entering": "Entrada no autorizada al campo", "not on pitch": "Fuera del campo",
    "regular season": "Temporada regular", "group stage": "Fase de grupos", "league stage": "Fase de liga",
    "round of 16": "Octavos de final", "quarter-finals": "Cuartos de final", "semi-finals": "Semifinales",
    "final": "Final", "3rd place final": "Tercer puesto", "preliminary round": "Ronda preliminar",
    "qualifying round": "Ronda clasificatoria", "relegation round": "Fase de descenso",
    "promotion round": "Fase de ascenso", "championship round": "Fase de campeonato",
}


def _provider_term(value: Any, lang: str) -> Any:
    if lang != "es":
        return value
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if not isinstance(value, str):
        return value
    translated = _PROVIDER_TERMS_ES.get(value.strip().casefold())
    if translated is not None:
        return translated
    prefix, _, number = value.rpartition(" ")
    if prefix.casefold() == "substitution" and number.isdigit():
        return f"Cambio {number}"
    return value


def _round_label(value: str, lang: str) -> str:
    prefix, separator, suffix = value.partition(" - ")
    return f"{_provider_term(prefix, lang)}{separator}{suffix}"


async def load_logos(urls: list[str | None], cache: dict[str, bytes]) -> dict[str, bytes]:
    # Only the provider's public media host is eligible; never send API credentials.
    urls = list(dict.fromkeys(url for url in urls if isinstance(url, str) and
                            url.startswith("https://media.api-sports.io/football/")))
    logos = {url: cache[url] for url in urls if url in cache}
    missing = [url for url in urls if url not in logos]
    if not missing:
        return logos
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=6), connector=aiohttp.TCPConnector(limit=6)
    ) as session:
        async def fetch(url: str) -> None:
            try:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > 512 * 1024:
                            return
                    logos[url] = bytes(data)
                    if len(cache) >= 128:
                        cache.pop(next(iter(cache)))
                    cache[url] = logos[url]
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

        await asyncio.gather(*(fetch(url) for url in missing))
    return logos


@lru_cache(maxsize=8)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (("DejaVuSans-Bold.ttf", "arialbd.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf")
                 if bold else ("DejaVuSans.ttf", "arial.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError as exc:
        raise OSError("No scalable font available for the Football table card") from exc


def table_pages(table: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in table:
        groups.setdefault(str(row.get("group") or ""), []).append(row)
    return [(group, rows[start:start + 24]) for group, rows in groups.items()
            for start in range(0, len(rows), 24)]


def table_text(rows: list[dict[str, Any]], lang: str) -> str:
    lines = [tr(lang, "POS | TEAM | P | GD | PTS", "POS | EQUIPO | PJ | DG | PTS")]
    for row in rows:
        team = row.get("team") or {}
        played = (row.get("all") or {}).get("played", "-")
        lines.append(f"{row.get('rank', '-')} | {team.get('name') or 'Unknown'} | "
                     f"{played} | {row.get('goalsDiff', '-')} | {row.get('points', '-')}")
    return "\n".join(lines)


def standings_pages(table: list[dict[str, Any]], *, league: dict[str, Any], season: int,
                    section: str, lang: str) -> list[dict[str, Any]]:
    title = league.get("name") or tr(lang, "Standings", "Tabla")
    if section == "compact":
        return [{"title": title, "subtitle": f"{season} / {_round_label(group, lang)}",
                 "table": rows, "league": league, "season": season, "group": group,
                 "rows": [(tr(lang, "Standings", "Tabla"), table_text(rows, lang))],
                 "media": [league.get("logo"), *[(row.get("team") or {}).get("logo") for row in rows]]}
                for group, rows in table_pages(table)]
    label = {"detailed": tr(lang, "Detailed standings", "Tabla detallada"),
             "home": tr(lang, "Home record", "Registro de local"),
             "away": tr(lang, "Away record", "Registro de visitante")}[section]
    entries = []
    for row in table:
        team = row.get("team") or {}
        record = row.get("all" if section == "detailed" else section) or {}
        fields = [(tr(lang, en, es), record.get(key)) for key, en, es in
                  [("played", "Played", "PJ"), ("win", "Wins", "Victorias"),
                   ("draw", "Draws", "Empates"), ("lose", "Losses", "Derrotas")]]
        fields += [(tr(lang, "Goals for", "Goles a favor"), _value(record, "goals", "for")),
                   (tr(lang, "Goals against", "Goles en contra"), _value(record, "goals", "against"))]
        if section == "detailed":
            fields += [(tr(lang, "Goal difference", "Diferencia de goles"), row.get("goalsDiff")),
                       (tr(lang, "Form", "Forma"), row.get("form"))]
        if row.get("description"):
            fields.append((tr(lang, "Provider zone", "Zona del proveedor"), row["description"]))
        group = _round_label(str(row.get("group") or ""), lang)
        entries.append({"name": team.get("name") or "N/A", "rank": row.get("rank"),
                        "detail": group, "logo": team.get("logo"), "fields": fields,
                        "value": row.get("points") if section == "detailed" else None})
    # Home/away records keep the provider's overall order; they are not a new ranking.
    subtitle = f"{season} / {label}"
    if section == "detailed":
        subtitle += tr(lang, " / PTS\nForm: W win / D draw / L loss",
                       " / PTS\nForma: W victoria / D empate / L derrota")
    if section in {"home", "away"}:
        subtitle += tr(lang, " / Overall table order", " / Orden de la tabla general")
    return _entry_pages(entries, title=title, subtitle=subtitle, badge=league.get("logo"),
                        metric="PTS" if section == "detailed" else "", page_size=3)


_RANKING_METRICS = {
    "top_scorers": ("goals", "total", "Goals", "Goles"),
    "top_assists": ("goals", "assists", "Assists", "Asistencias"),
    "top_yellow_cards": ("cards", "yellow", "Yellow cards", "Amarillas"),
    "top_red_cards": ("cards", "red", "Red cards", "Rojas"),
}


def leaderboard_pages(rows: list[dict[str, Any]], *, league: dict[str, Any], season: int,
                      focus: str, lang: str) -> list[dict[str, Any]]:
    section, metric, en, es = _RANKING_METRICS[focus]
    label = tr(lang, en, es)
    entries = []
    for rank, row in enumerate(rows, 1):
        player = row.get("player") or {}
        blocks = row.get("statistics") or [{}]
        # Preserve provider order/rank and every returned team/competition block.
        for block in blocks:
            team, competition = block.get("team") or {}, block.get("league") or {}
            detail = " / ".join(str(value) for value in [team.get("name"), competition.get("name"),
                                                         competition.get("season")] if value is not None)
            fields = [(tr(lang, "Appearances", "Partidos"), _value(block, "games", "appearences"))]
            if focus == "top_scorers":
                fields.append((tr(lang, "Assists", "Asistencias"), _value(block, "goals", "assists")))
            elif focus == "top_assists":
                fields.append((tr(lang, "Goals", "Goles"), _value(block, "goals", "total")))
            entries.append({"rank": rank, "name": player.get("name") or "N/A", "detail": detail,
                            "logo": team.get("logo"), "value": _value(block, section, metric),
                            "fields": fields})
    return _entry_pages(entries, title=league.get("name") or tr(lang, "Rankings", "Clasificación"),
                        subtitle=f"{season} / {label}", badge=league.get("logo"), metric=label)


def _entry_pages(entries: list[dict[str, Any]], *, title: str, subtitle: str,
                 badge: str | None, metric: str, page_size: int = 5) -> list[dict[str, Any]]:
    result = []
    for start in range(0, len(entries), page_size):
        batch = entries[start:start + page_size]
        rows = []
        for entry in batch:
            details = [entry.get("detail") or ""]
            if metric:
                details.append(f"{metric}: {'N/A' if entry['value'] is None else entry['value']}")
            details.extend(f"{label}: {'N/A' if value is None else value}" for label, value in entry["fields"])
            rows.append((f"{entry.get('rank') or '-'}. {entry['name']}", "\n".join(details)))
        result.append({"title": title, "subtitle": subtitle, "entries": batch, "metric": metric,
                       "rows": rows, "media": [badge, *[entry.get("logo") for entry in batch]]})
    return result


def h2h_summary_pages(fixtures: list[dict[str, Any]], teams: tuple[dict, dict], *, lang: str) -> list[dict[str, Any]]:
    first, second = teams
    if not fixtures:
        return [{"title": f"{first['display_name']} vs {second['display_name']}", "text": tr(lang,
            "No meetings are recorded in the selected sample.", "No hay enfrentamientos registrados en la muestra seleccionada.")}]
    wins = [0, 0]
    goals = [0, 0]
    shootouts = [0, 0]
    draws = counted = penalties = missing_penalties = 0
    for item in fixtures:
        home, away = _value(item, "teams", "home", "id"), _value(item, "teams", "away", "id")
        status = _value(item, "fixture", "status", "short")
        if {home, away} != {first["id"], second["id"]} or status not in {"FT", "AET", "PEN"}:
            continue
        if status == "PEN":
            penalty = [_value(item, "score", "penalty", side) for side in ("home", "away")]
            if all(type(value) is int and value >= 0 for value in penalty) and penalty[0] != penalty[1]:
                if home != first["id"]:
                    penalty.reverse()
                penalties += 1
                shootouts[0 if penalty[0] > penalty[1] else 1] += 1
            else:
                missing_penalties += 1
        scores = [_value(item, "goals", side) for side in ("home", "away")]
        if not all(type(value) is int and value >= 0 for value in scores):
            continue
        if home != first["id"]:
            scores.reverse()
        counted += 1
        goals = [total + value for total, value in zip(goals, scores)]
        if scores[0] == scores[1]:
            draws += 1
        else:
            wins[0 if scores[0] > scores[1] else 1] += 1
    names = [team["display_name"] for team in teams]
    rows = [(tr(lang, "Returned meetings", "Partidos recibidos"), len(fixtures)),
            (tr(lang, "Finished meetings with scores", "Partidos finalizados con marcador"), counted),
            (tr(lang, "Excluded from goal comparison", "Excluidos de comparación de goles"), len(fixtures) - counted),
            (tr(lang, "Draws before shootouts", "Empates antes de penales"), draws)]
    for index, name in enumerate(names):
        rows.extend([(tr(lang, f"Wins: {name}", f"Victorias: {name}"), wins[index]),
                     (tr(lang, f"Goals: {name}", f"Goles: {name}"), goals[index])])
    rows.append((tr(lang, "Shootouts with results", "Tandas con resultado"), penalties))
    rows.append((tr(lang, "Shootouts without results", "Tandas sin resultado"), missing_penalties))
    for index, name in enumerate(names):
        rows.append((tr(lang, f"Shootout wins: {name}", f"Victorias en penales: {name}"), shootouts[index]))
    media = [next((_value(item, "teams", side, "logo") for item in fixtures for side in ("home", "away")
                   if _value(item, "teams", side, "id") == team["id"]), None) for team in teams]
    return _pages(" vs ".join(names), tr(lang,
        "Returned sample / All competitions / Goals include extra time; shootouts are separate",
        "Muestra recibida / Todas las competiciones / Goles incluyen prórroga; penales por separado"), rows, media)


def record_pages(rows: list[dict[str, Any]], *, team: dict, season: int | None, focus: str, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_datetime

    entries = []
    if focus == "transfers":
        movements = [(row.get("player") or {}, item) for row in rows for item in row.get("transfers") or []]
        movements.sort(key=lambda pair: str(pair[1].get("date") or ""), reverse=True)
        for player, item in movements:
            fields = [(tr(lang, "From", "Desde"), _value(item, "teams", "out", "name")),
                      (tr(lang, "To", "Hacia"), _value(item, "teams", "in", "name")),
                      (tr(lang, "Type / fee", "Tipo / importe"), _provider_term(item.get("type"), lang))]
            entries.append({"name": player.get("name") or "N/A", "detail": item.get("date") or "N/A",
                            "logo": player.get("photo"), "fields": fields})
        label = tr(lang, "Transfers / Recorded history", "Transferencias / Historial registrado")
    else:
        for row in rows:
            player = row.get("player") or {}
            fields = [(tr(lang, "Reason", "Motivo"), _provider_term(player.get("reason"), lang)),
                      (tr(lang, "Availability", "Disponibilidad"), _provider_term(player.get("type"), lang)),
                      (tr(lang, "Competition", "Competición"), _value(row, "league", "name"))]
            entries.append({"name": player.get("name") or "N/A", "detail": fixture_datetime(row) if _value(row, "fixture", "date") else "",
                            "logo": player.get("photo"), "fields": fields})
        label = tr(lang, f"Injuries / Season {season}", f"Lesiones / Temporada {season}")
    for index, entry in enumerate(entries, 1):
        entry["rank"] = index
    return _entry_pages(entries, title=team.get("name") or "N/A", subtitle=label,
                        badge=team.get("logo"), metric="", page_size=3)


def profile_section_pages(result: Any, focus: str, *, lang: str) -> list[dict[str, Any]]:
    team_section = focus in {"team_squad", "team_seasons", "team_coach", "team_venue"}
    row = result.team_context_row if team_section else result.player_context_row
    subject = (row or {}).get("team" if team_section else "player") or {}
    title = subject.get("name") or "N/A"
    badge = subject.get("logo") or subject.get("photo")
    label = {
        "team_squad": tr(lang, "Current squad", "Plantilla actual"),
        "team_seasons": tr(lang, "Registered seasons", "Temporadas registradas"),
        "team_coach": tr(lang, "Registered coaches", "Entrenadores registrados"),
        "team_venue": tr(lang, "Venue", "Estadio"),
        "player_teams": tr(lang, "Career teams / Registered seasons", "Trayectoria / Temporadas registradas"),
        "player_transfers": tr(lang, "Transfer history", "Historial de transferencias"),
        "player_trophies": tr(lang, "Recorded trophies", "Palmarés registrado"),
        "player_sidelined": tr(lang, "Recorded absence history", "Historial de ausencias"),
    }[focus]
    entries, pages = [], []
    for item in result.generic_rows:
        if focus == "team_squad":
            for player in item.get("players") or []:
                entries.append({"name": player.get("name") or "N/A", "logo": player.get("photo"), "detail": "",
                    "fields": [(tr(lang, "Position", "Posición"), _provider_term(player.get("position"), lang)),
                               (tr(lang, "Shirt number", "Dorsal"), player.get("number")),
                               (tr(lang, "Age", "Edad"), player.get("age"))]})
        elif focus == "team_coach":
            coach = item.get("coach") if isinstance(item.get("coach"), dict) else item
            fields = [(tr(lang, "Nationality", "Nacionalidad"), coach.get("nationality")),
                      (tr(lang, "Age", "Edad"), coach.get("age")),
                      (tr(lang, "Born", "Nacimiento"), _value(coach, "birth", "date")),
                      (tr(lang, "Profile team", "Equipo del perfil"), _value(item, "team", "name"))]
            fields.extend((tr(lang, f"Career: {(entry.get('team') or {}).get('name') or 'N/A'}",
                                   f"Trayectoria: {(entry.get('team') or {}).get('name') or 'N/A'}"),
                           f"{entry.get('start') or 'N/A'} / {entry.get('end') or 'N/A'}") for entry in item.get("career") or [])
            pages.extend(_pages(coach.get("name") or "N/A", f"{label} / {title}", fields, [coach.get("photo"), badge]))
        elif focus == "team_venue":
            fields = [(tr(lang, en, es), _provider_term(item.get(key), lang)) for key, en, es in [
                ("city", "City", "Ciudad"), ("address", "Address", "Dirección"),
                ("capacity", "Capacity", "Capacidad"), ("surface", "Surface", "Superficie")]]
            pages.extend(_pages(item.get("name") or "N/A", f"{label} / {title}", fields, [item.get("image"), badge]))
        elif focus == "player_teams":
            team = item.get("team") or {}
            entries.append({"name": team.get("name") or "N/A", "logo": team.get("logo"), "detail": "",
                            "fields": [(tr(lang, "Seasons", "Temporadas"),
                                        ", ".join(str(year) for year in item.get("seasons") or []) or None)]})
        elif focus == "player_trophies":
            entries.append({"name": item.get("league") or "N/A", "logo": None, "detail": item.get("country") or "",
                            "fields": [(tr(lang, "Season", "Temporada"), item.get("season")),
                                       (tr(lang, "Place", "Puesto"), _provider_term(item.get("place"), lang))]})
        elif focus == "player_sidelined":
            entries.append({"name": _provider_term(item.get("type") or "N/A", lang), "logo": None, "detail": "",
                            "fields": [(tr(lang, "Start", "Inicio"), item.get("start")),
                                       (tr(lang, "End", "Fin"), item.get("end"))]})
    if focus == "team_seasons" and result.generic_rows:
        fields = [(tr(lang, "Season", "Temporada"), item.get("season")) for item in result.generic_rows]
        pages = _pages(title, label, fields, [badge])
    if focus == "player_transfers":
        pages = record_pages(result.generic_rows, team={"name": title, "logo": badge},
                             season=None, focus="transfers", lang=lang)
    if entries:
        for index, entry in enumerate(entries, 1):
            entry["rank"] = index
        pages = _entry_pages(entries, title=title, subtitle=label, badge=badge, metric="", page_size=3)
    if focus == "team_venue" and not pages:
        return [{"title": f"{title} / {label}", "text": tr(lang,
            "No venue details are supplied in this team profile.", "Este perfil de equipo no incluye datos de un estadio.")}]
    return pages or [{"title": f"{title} / {label}", "text": tr(lang,
        f"No records are published for {title} under {label}.", f"No hay registros publicados para {title} en {label}.")}]


def odds_pages(rows: list[dict[str, Any]], *, fixture: dict, lang: str, live: bool) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_datetime

    title = " vs ".join(fixture_teams(fixture))
    pages = []
    for row in rows:
        status = row.get("status") or {}
        blocked = any(status.get(key) is True for key in ("stopped", "blocked", "finished"))
        bookmakers = row.get("bookmakers") or []
        if live:
            bookmakers = [{"name": tr(lang, "Live feed (bookmaker not supplied)", "En vivo (sin casa identificada)"),
                           "bets": row.get("odds") or []}]
        for bookmaker in bookmakers:
            identity = bookmaker.get("name") or f"#{bookmaker.get('id', 'N/A')}"
            for bet in bookmaker.get("bets") or []:
                market = _provider_term(bet.get("name") or f"#{bet.get('id', 'N/A')}", lang)
                values = []
                for value in bet.get("values") or []:
                    label = str(_provider_term(value.get("value") or value.get("name") or "N/A", lang))
                    if value.get("handicap") is not None:
                        label += f" / {tr(lang, 'Handicap', 'Hándicap')}: {value['handicap']}"
                    suspended = blocked or bet.get("suspended") is True or value.get("suspended") is True
                    odd = tr(lang, "Suspended / unavailable", "Suspendida / no disponible") if suspended else value.get("odd")
                    values.append((label, odd))
                for start in range(0, len(values), 6):
                    pages.extend(_pages(title, f"{tr(lang, 'Live odds', 'Cuotas en vivo') if live else tr(lang, 'Pre-match odds', 'Cuotas previas')} / {fixture_datetime(fixture)} / {identity} / {market}",
                                        values[start:start + 6], [_value(fixture, "league", "logo")]))
        if blocked and not any(bookmaker.get("bets") for bookmaker in bookmakers):
            pages.append({"title": title, "text": tr(lang, "Live odds are suspended or unavailable.",
                                                      "Las cuotas en vivo están suspendidas o no disponibles.")})
    return pages


def prediction_pages(rows: list[dict[str, Any]], *, fixture: dict, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_datetime

    pages = []
    for row in rows:
        prediction = row.get("predictions") or {}
        fields = [(tr(lang, en, es), _provider_term(_value(prediction, *path), lang)) for path, en, es in [
            (("winner", "name"), "Forecast winner", "Ganador previsto"),
            (("winner", "comment"), "Provider comment", "Comentario del proveedor"),
            (("win_or_draw",), "Win or draw", "Victoria o empate"),
            (("under_over",), "Goal line", "Línea de goles"),
            (("goals", "home"), "Home goals forecast", "Goles previstos de local"),
            (("goals", "away"), "Away goals forecast", "Goles previstos de visitante"),
            (("percent", "home"), "Home win", "Victoria local"),
            (("percent", "draw"), "Draw", "Empate"),
            (("percent", "away"), "Away win", "Victoria visitante"),
            (("advice",), "Provider forecast detail", "Detalle del pronóstico del proveedor"),
        ] if _value(prediction, *path) is not None]
        if fields:
            pages.extend(_pages(" vs ".join(fixture_teams(fixture)), f"{tr(lang, 'Provider forecast', 'Pronóstico')} / {fixture_datetime(fixture)}",
                                fields, [_value(fixture, "league", "logo")]))
    return pages


def preview_pages(match: Any, result: Any, *, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_datetime

    fixture = match.fixture
    names = fixture_teams(fixture)
    fields = [(tr(lang, "Kickoff", "Inicio"), fixture_datetime(fixture)),
              (tr(lang, "Round", "Jornada"), _round_label(_value(fixture, "league", "round") or "", lang))]
    for side, name in zip(("home", "away"), names):
        team_id = _value(fixture, "teams", side, "id")
        for row in result.standings_table:
            if _value(row, "team", "id") == team_id:
                position = "N/A" if row.get("rank") is None else row["rank"]
                points = "N/A" if row.get("points") is None else row["points"]
                fields.extend([(tr(lang, f"Table: {name}", f"Tabla: {name}"),
                                f"{_round_label(row.get('group') or '', lang)} / {tr(lang, 'Position', 'Posición')}: {position} / PTS: {points}"),
                               (tr(lang, f"Form: {name} (W win / D draw / L loss)", f"Forma: {name} (W victoria / D empate / L derrota)"), row.get("form"))])
    fields.append((tr(lang, "Available recent meetings", "Enfrentamientos recientes disponibles"),
                   None if "h2h_unavailable" in result.notes else len(result.fixtures)))
    if result.notes:
        fields.append((tr(lang, "Availability", "Disponibilidad"), tr(lang,
            "Some preview data is unavailable; fixture details remain valid.",
            "Parte de la previa no está disponible; los datos del partido siguen siendo válidos.")))
    pages = _pages(" vs ".join(names), f"{tr(lang, 'Preview', 'Previa')} / {_value(fixture, 'league', 'name') or ''} / {_value(fixture, 'league', 'season') or 'N/A'}",
                   fields, [_value(fixture, "teams", side, "logo") for side in ("home", "away")])
    if result.fixtures:
        pages.extend(fixture_list_pages(result.fixtures, title=tr(lang, "Recent H2H", "Enfrentamientos recientes"), lang=lang))
    return pages


def season_comparison_pages(fixture: dict, snapshots: list[Any], *, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams

    league = fixture.get("league") or {}
    sides = []
    for side, snapshot in zip(("home", "away"), snapshots):
        pages = team_statistics_pages({"team": _value(fixture, "teams", side)}, snapshot.team_season_statistics or {},
                                       league=league.get("name") or "", season=league.get("season"), lang=lang)
        sides.append(pages)
    result = []
    for left, right in zip(*sides):
        pairs = [(label, "N/A" if value is None else str(value), "N/A" if other is None else str(other))
                 for (label, value), (_, other) in zip(left["rows"], right["rows"])]
        for start in range(0, len(pairs), 6):
            result.append({"title": " vs ".join(fixture_teams(fixture)), "subtitle": left["subtitle"],
                           "match": fixture, "sides": fixture_teams(fixture), "comparison": pairs[start:start + 6],
                           "media": [_value(fixture, "teams", side, "logo") for side in ("home", "away")]})
    return result


def render_table(
    rows: list[dict[str, Any]], *, league: dict[str, Any], season: int,
    group: str, lang: str, logos: dict[str, bytes], page: int, pages: int,
) -> BytesIO:
    width, row_height, top = 800, 54, 230
    image = Image.new("RGB", (width, top + row_height * len(rows) + (70 if pages > 1 else 24)), "#141c29")
    draw = ImageDraw.Draw(image)
    foreground, muted = "#f2f5fa", "#b8c5d6"

    def text(x: int, y: int, value: object, *, size: int = 25, bold: bool = False,
             color: str = foreground, anchor: str = "lm", max_width: int = 0) -> None:
        value = str(value)
        font = _font(size, bold)
        if max_width:
            while value and draw.textlength(value, font=font) > max_width:
                value = value[:-2].rstrip() + "\u2026" if len(value) > 1 else ""
        draw.text((x, y), value, font=font, fill=color, anchor=anchor)

    draw.rectangle((28, 0, width - 28, 4), fill="#568bdf")
    _paste_logo(image, logos, league.get("logo"), 32, 32, 82)
    text(136, 57, league.get("name") or tr(lang, "Football", "Fútbol"), size=32, bold=True, max_width=624)
    text(136, 99, tr(lang, "Standings", "Tabla general"), size=24, color=muted)
    subtitle = tr(lang, f"Season {season}", f"Temporada {season}")
    if group:
        subtitle += f"  /  {_round_label(group, lang)}"
    text(34, 155, subtitle, size=22, color=muted, max_width=730)
    text(42, 207, "POS", size=18, color=muted)
    text(158, 207, tr(lang, "TEAM", "EQUIPO"), size=18, color=muted)
    for x, label in [(590, tr(lang, "P", "PJ")), (674, tr(lang, "GD", "DG")), (760, "PTS")]:
        text(x, 207, label, size=18, color=muted, anchor="rm")
    for index, row in enumerate(rows):
        y = top + index * row_height
        draw.rounded_rectangle((24, y, 776, y + row_height - 3), radius=6,
                               fill="#1b2637" if index % 2 == 0 else "#172131")
        center = y + 26
        text(65, center, row.get("rank", "-"), color=muted, anchor="rm")
        team = row.get("team") or {}
        _paste_logo(image, logos, team.get("logo"), 96, y + 7, 38)
        text(158, center, team.get("name") or "Unknown", size=24, max_width=355)
        text(590, center, (row.get("all") or {}).get("played", "-"), anchor="rm")
        gd = row.get("goalsDiff")
        text(674, center, f"{gd:+d}" if type(gd) is int else "-", anchor="rm")
        text(760, center, row.get("points", "-"), bold=True, anchor="rm")
    if pages > 1:
        text(760, image.height - 32, f"{page}/{pages}", size=18, color=muted, anchor="rm")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def _paste_logo(image: Image.Image, logos: dict[str, bytes], url: str | None,
                x: int, y: int, size: int) -> bool:
    data = logos.get(url or "")
    if not data:
        return False
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > 1_000_000:
                return False
            badge = source.convert("RGBA")
            badge.thumbnail((size, size), Image.Resampling.LANCZOS)
            image.paste(badge, (x + (size - badge.width) // 2,
                               y + (size - badge.height) // 2), badge)
        return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _value(data: dict[str, Any], *path: str) -> Any:
    value: Any = data
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _pages(title: str, subtitle: str, rows: list[tuple[str, Any]], media: list[str | None],
           form: str | None = None) -> list[dict[str, Any]]:
    return [{"title": title, "subtitle": subtitle, "rows": rows[start:start + 12],
             "media": media, "form": form} for start in range(0, len(rows), 12)]


# The first five metrics remain visible even when their values are missing.
_PLAYER_METRICS = [
    (('games', 'appearences'), 'Appearances', 'Partidos'),
    (('games', 'minutes'), 'Minutes', 'Minutos'),
    (('games', 'rating'), 'Rating', 'Valoracion'),
    (('goals', 'total'), 'Goals', 'Goles'),
    (('goals', 'assists'), 'Assists', 'Asistencias'),
    (('shots', 'total'), 'Shots', 'Tiros'),
    (('shots', 'on'), 'Shots on target', 'Tiros a puerta'),
    (('passes', 'total'), 'Passes', 'Pases'),
    (('passes', 'key'), 'Key passes', 'Pases clave'),
    (('passes', 'accuracy'), 'Pass accuracy', 'Precision de pase'),
    (('tackles', 'total'), 'Tackles', 'Entradas'),
    (('tackles', 'blocks'), 'Blocks', 'Bloqueos'),
    (('tackles', 'interceptions'), 'Interceptions', 'Intercepciones'),
    (('duels', 'total'), 'Duels', 'Duelos'),
    (('duels', 'won'), 'Duels won', 'Duelos ganados'),
    (('dribbles', 'attempts'), 'Dribbles attempted', 'Regates intentados'),
    (('dribbles', 'success'), 'Successful dribbles', 'Regates completados'),
    (('dribbles', 'past'), 'Dribbled past', 'Veces regateado'),
    (('fouls', 'drawn'), 'Fouls drawn', 'Faltas recibidas'),
    (('fouls', 'committed'), 'Fouls committed', 'Faltas cometidas'),
    (('cards', 'yellow'), 'Yellow cards', 'Tarjetas amarillas'),
    (('cards', 'yellowred'), 'Second yellow', 'Segunda amarilla'),
    (('cards', 'red'), 'Red cards', 'Tarjetas rojas'),
    (('goals', 'saves'), 'Saves', 'Paradas'),
    (('goals', 'conceded'), 'Goals conceded', 'Goles recibidos'),
    (('penalty', 'won'), 'Penalties won', 'Penales recibidos'),
    (('penalty', 'commited'), 'Penalties conceded', 'Penales cometidos'),
    (('penalty', 'scored'), 'Penalties scored', 'Penales anotados'),
    (('penalty', 'missed'), 'Penalties missed', 'Penales fallados'),
    (('penalty', 'saved'), 'Penalties saved', 'Penales detenidos'),
]


def player_pages(row: dict[str, Any], *, season: int | None, lang: str,
                 notice: str | None = None, latest: bool = False) -> dict[str, list[dict[str, Any]]]:
    player = row.get("player") or {}
    name = player.get("name") or tr(lang, "Player", "Jugador")
    photo = player.get("photo")
    profile = [(tr(lang, en, es), _provider_term(player.get(key), lang) if key == "position" else player.get(key)) for key, en, es in [
        ("position", "Position", "Posicion"), ("age", "Age", "Edad"),
        ("nationality", "Nationality", "Nacionalidad"), ("number", "Number", "Numero"),
        ("height", "Height", "Altura"), ("weight", "Weight", "Peso"),
    ]]
    full_name = " ".join(str(player.get(key) or "").strip() for key in ("firstname", "lastname")).strip()
    if full_name and full_name != name:
        profile.insert(0, (tr(lang, "Full name", "Nombre completo"), full_name))
    subtitle = tr(lang, "Player profile", "Perfil del jugador")
    season_label = tr(lang, "Latest registered season", "Ultima temporada registrada") if latest else tr(lang, "Season", "Temporada")
    if season is not None:
        subtitle += f" / {season_label} {season}"
    if not row.get("statistics") and notice:
        profile.append((tr(lang, "Season statistics", "Estadisticas de temporada"), notice))
    result = {"profile": _pages(name, subtitle, profile, [photo])}
    statistics = []
    for record in row.get("statistics") or []:
        league, team = record.get("league") or {}, record.get("team") or {}
        year = league.get("season", season)
        scope = f"{team.get('name') or 'N/A'} / {league.get('name') or 'N/A'} / {season_label} {year if year is not None else 'N/A'}"
        position = _value(record, "games", "position")
        if position:
            scope += f" / {_provider_term(position, lang)}"
        metrics = []
        for index, (path, en, es) in enumerate(_PLAYER_METRICS):
            value = _value(record, *path)
            if index < 5 or value is not None:
                metrics.append((tr(lang, en, es), value))
        statistics.extend(_pages(name, scope, metrics, [photo, team.get("logo"), league.get("logo")]))
    result["statistics"] = statistics or [{"title": name, "text": notice or tr(
        lang, "No season statistics are recorded for this player in the selected scope.",
        "No hay estadisticas de temporada registradas para este jugador en el alcance seleccionado.")}]
    return result


def team_pages(team_row: dict[str, Any], standing: dict[str, Any] | None,
               fixtures: list[dict[str, Any]], *, league: str, season: int, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_datetime, fixture_teams, fixture_score
    from services.football_live_match_service import build_lifecycle

    team, standing = team_row.get("team") or {}, standing or {}
    rows = [(tr(lang, en, es), _value(standing, *path)) for path, en, es in [
        (("rank",), "Position", "Posicion"), (("points",), "Points", "Puntos"),
        (("all", "played"), "Played", "Jugados"), (("all", "win"), "Wins", "Victorias"),
        (("all", "draw"), "Draws", "Empates"), (("all", "lose"), "Losses", "Derrotas"),
        (("all", "goals", "for"), "Goals for", "Goles a favor"),
        (("all", "goals", "against"), "Goals against", "Goles en contra"),
    ]]
    if fixtures:
        fixture = fixtures[0]
        lifecycle = build_lifecycle(fixture)
        label = tr(lang, "Last match", "Ultimo partido") if lifecycle.is_terminal else (
            tr(lang, "Next match", "Proximo partido") if lifecycle.is_scheduled else tr(lang, "Match", "Partido"))
        home, away = fixture_teams(fixture)
        rows.append((label, f"{home} {fixture_score(fixture)} {away}\n{fixture_datetime(fixture)}"))
    if team.get("founded") is not None:
        rows.append((tr(lang, "Founded", "Fundado"), team["founded"]))
    if _value(team_row, "venue", "name"):
        rows.append((tr(lang, "Venue", "Estadio"), _value(team_row, "venue", "name")))
    return _pages(team.get("name") or tr(lang, "Team", "Equipo"), f"{league} / {season}", rows,
                  [team.get("logo")], standing.get("form"))


def team_statistics_pages(team_row: dict[str, Any], stats: dict[str, Any], *,
                          league: str, season: int, lang: str) -> list[dict[str, Any]]:
    team = team_row.get("team") or {}
    result = []
    for scope, en, es in [("total", "Overall", "General"), ("home", "Home", "Local"), ("away", "Away", "Visitante")]:
        rows = [(tr(lang, label_en, label_es), _value(stats, *path, scope)) for path, label_en, label_es in [
            (("fixtures", "played"), "Played", "Jugados"), (("fixtures", "wins"), "Wins", "Victorias"),
            (("fixtures", "draws"), "Draws", "Empates"), (("fixtures", "loses"), "Losses", "Derrotas"),
            (("goals", "for", "total"), "Goals for", "Goles a favor"),
            (("goals", "against", "total"), "Goals against", "Goles en contra"),
            (("goals", "for", "average"), "Goals per game", "Goles por partido"),
            (("goals", "against", "average"), "Conceded per game", "Recibidos por partido"),
            (("clean_sheet",), "Clean sheets", "Porterias a cero"),
            (("failed_to_score",), "Failed to score", "Partidos sin marcar"),
        ]]
        result.extend(_pages(team.get("name") or tr(lang, "Team", "Equipo"), f"{league} / {season} / {tr(lang, en, es)}",
                             rows, [team.get("logo")], stats.get("form")))
    return result


def page_text(page: dict[str, Any]) -> str:
    if page.get("text"):
        return f"{page['title']}\n{page['text']}"
    lines = [page["title"], page.get("subtitle", "")]
    if page.get("comparison"):
        home, away = page["sides"]
        lines.append(f"{home} | {away}")
        lines.extend(f"{label}: {left} | {right}" for label, left, right in page["comparison"])
    if page.get("form"):
        lines.append(f"Form: {page['form']}")
    lines.extend(f"{label}: {'N/A' if value is None else value}" for label, value in page.get("rows", []))
    return "\n".join(lines)


def _wrap(text: str, font: Any, width: int) -> list[str]:
    lines = []
    for paragraph in str(text).split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if font.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while font.getlength(word) > width:
                end = 1
                while end < len(word) and font.getlength(word[:end + 1]) <= width:
                    end += 1
                lines.append(word[:end])
                word = word[end:]
            current = word
        lines.append(current)
    return lines


def render_profile(page: dict[str, Any], *, lang: str, logos: dict[str, bytes],
                   number: int, total: int) -> BytesIO:
    if "table" in page:
        return render_table(page["table"], league=page["league"], season=page["season"],
                            group=page["group"], lang=lang, logos=logos, page=number, pages=total)
    if "entries" in page:
        return render_entries(page, logos=logos, number=number, total=total)
    if "fixtures" in page:
        return render_fixture_list(page, lang=lang, logos=logos, number=number, total=total)
    if page.get("match"):
        return render_match(page, lang=lang, logos=logos, number=number, total=total)
    title_font, label_font, value_font = _font(34, True), _font(23), _font(29, True)
    media = page.get("media", [])
    title_x = 184 if media and media[0] in logos else 34
    title_lines = _wrap(page["title"], title_font, 624 - title_x)
    subtitle_lines = _wrap(page.get("subtitle", ""), _font(23), 730)
    top = max(186, 48 + len(title_lines) * 42) + len(subtitle_lines) * 30 + 28
    form_rows = (len(page.get("form") or "") + 17) // 18
    if form_rows:
        top += 48 + form_rows * 40
    rows = page.get("rows", [])
    tiles = []
    height = top
    for index in range(0, len(rows), 2):
        pair = []
        for label, value in rows[index:index + 2]:
            labels = _wrap(label, label_font, 324)
            values = _wrap("N/A" if value is None else str(value), value_font, 324)
            pair.append((labels, values))
        row_height = max(30 + len(labels) * 29 + len(values) * 36 for labels, values in pair)
        tiles.append((height, row_height, pair))
        height += row_height + 12
    image = Image.new("RGB", (800, height + 38), "#141c29")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 0, 772, 4), fill="#568bdf")
    if media:
        _paste_logo(image, logos, media[0], 32, 32, 128)
    for index, url in enumerate(media[1:3]):
        _paste_logo(image, logos, url, 650 + index * 58, 108, 48)
    for index, line in enumerate(title_lines):
        draw.text((title_x, 48 + index * 42), line, font=title_font, fill="#f2f5fa", anchor="lt")
    subtitle_y = max(186, 48 + len(title_lines) * 42)
    for index, line in enumerate(subtitle_lines):
        draw.text((34, subtitle_y + index * 30), line, font=_font(23), fill="#b8c5d6", anchor="lt")
    if page.get("form"):
        form_y = top - (44 + form_rows * 40)
        for index, letter in enumerate(page["form"]):
            x, y = 34 + (index % 18) * 40, form_y + (index // 18) * 40
            draw.rounded_rectangle((x, y, x + 32, y + 32), radius=4,
                                   fill={"W": "#235b46", "D": "#414e64", "L": "#783f4b"}.get(letter, "#414e64"))
            draw.text((x + 16, y + 16), letter, font=_font(21, True), fill="#ffffff", anchor="mm")
        legend = tr(lang, "W: Win / D: Draw / L: Loss", "W: Victoria / D: Empate / L: Derrota")
        draw.text((34, form_y + form_rows * 40), legend, font=_font(18), fill="#b8c5d6", anchor="lt")
    for y, row_height, pair in tiles:
        for index, (labels, values) in enumerate(pair):
            x = 24 + index * 382
            draw.rounded_rectangle((x, y, x + 370, y + row_height), radius=8, fill="#1b2637")
            for line_index, line in enumerate(labels):
                draw.text((x + 22, y + 16 + line_index * 29), line, font=label_font, fill="#b8c5d6", anchor="lt")
            for line_index, line in enumerate(values):
                draw.text((x + 22, y + 20 + len(labels) * 29 + line_index * 36), line,
                          font=value_font, fill="#f2f5fa", anchor="lt")
    if total > 1:
        draw.text((768, image.height - 20), f"{number}/{total}", font=_font(18), fill="#b8c5d6", anchor="rm")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def render_entries(page: dict[str, Any], *, logos: dict[str, bytes], number: int, total: int) -> BytesIO:
    title = _wrap(page["title"], _font(32, True), 620)
    subtitle = _wrap(page["subtitle"], _font(23), 730)
    top = max(135, 36 + len(title) * 40) + len(subtitle) * 30 + 24
    blocks, height = [], top
    for entry in page["entries"]:
        names = _wrap(f"{entry.get('rank') or '-'}. {entry['name']}", _font(27, True), 555)
        details = _wrap(entry.get("detail") or "", _font(24), 690)
        fields = [f"{label}: {'N/A' if value is None else value}" for label, value in entry["fields"]]
        # Keep dense standings readable by wrapping records instead of widening the table.
        lines = [line for start in range(0, len(fields), 2)
                 for line in _wrap("  /  ".join(fields[start:start + 2]), _font(24), 690)]
        block_height = 32 + max(48, len(names) * 34) + (len(details) + len(lines)) * 31
        blocks.append((height, block_height, entry, names, details, lines))
        height += block_height + 12
    image = Image.new("RGB", (800, height + 36), "#141c29")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 0, 772, 4), fill="#568bdf")
    _paste_logo(image, logos, page["media"][0], 32, 30, 70)
    for index, line in enumerate(title):
        draw.text((122, 36 + index * 40), line, font=_font(32, True), fill="#f2f5fa", anchor="lt")
    for index, line in enumerate(subtitle):
        draw.text((34, top - 24 - len(subtitle) * 30 + index * 30), line,
                  font=_font(23), fill="#b8c5d6", anchor="lt")
    for y, size, entry, names, details, lines in blocks:
        draw.rounded_rectangle((24, y, 776, y + size), radius=8, fill="#1b2637")
        _paste_logo(image, logos, entry.get("logo"), 36, y + 16, 44)
        for index, line in enumerate(names):
            draw.text((92, y + 16 + index * 34), line, font=_font(27, True), fill="#f2f5fa", anchor="lt")
        if page["metric"]:
            value = entry.get("value")
            draw.text((746, y + 16), "N/A" if value is None else str(value), font=_font(29, True),
                      fill="#f2f5fa", anchor="rt")
        line_y = y + 16 + max(48, len(names) * 34)
        for line in details + lines:
            draw.text((40, line_y), line, font=_font(24), fill="#b8c5d6", anchor="lt")
            line_y += 31
    if total > 1:
        draw.text((770, height + 8), f"{number}/{total}", font=_font(18), fill="#b8c5d6", anchor="rt")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def fixture_list_pages(fixtures: list[dict[str, Any]], *, title: str, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_score, fixture_datetime, fixture_status

    result = []
    for start in range(0, len(fixtures), 5):
        batch = fixtures[start:start + 5]
        rows, media = [], []
        for fixture in batch:
            home, away = fixture_teams(fixture)
            league = fixture.get("league") or {}
            details = " / ".join(value for value in [league.get("name"), _round_label(league.get("round") or "", lang),
                                 fixture_datetime(fixture), fixture_status(fixture.get("fixture") or {})] if value)
            rows.append((f"{home} {fixture_score(fixture)} {away}", details))
            media.extend([league.get("logo"), *[_value(fixture, "teams", side, "logo") for side in ("home", "away")]])
        result.append({"title": title, "subtitle": tr(lang, f"{len(fixtures)} matches", f"{len(fixtures)} partidos"),
                       "fixtures": batch, "rows": rows, "media": media})
    return result or [{"title": title, "text": tr(lang, "No fixtures are listed in the selected scope.",
                                                 "No hay partidos registrados en el alcance seleccionado."), "fixtures": []}]


def render_fixture_list(page: dict[str, Any], *, lang: str, logos: dict[str, bytes], number: int, total: int) -> BytesIO:
    from services.football_formatter import fixture_teams, fixture_datetime, fixture_status

    heading = _wrap(page["title"], _font(34, True), 730)
    top = 38 + len(heading) * 42 + 48
    blocks, height = [], top
    for fixture in page["fixtures"]:
        league = fixture.get("league") or {}
        label = " / ".join(value for value in [league.get("name"), _round_label(league.get("round") or "", lang)] if value)
        label_lines = _wrap(label, _font(21), 670)
        names = [_wrap(name, _font(27, True), 570) for name in fixture_teams(fixture)]
        time_lines = _wrap(f"{fixture_datetime(fixture)} / {fixture_status(fixture.get('fixture') or {})}", _font(21), 710)
        block_height = 32 + len(label_lines) * 27 + sum(max(42, len(lines) * 33 + 8) for lines in names) + len(time_lines) * 27
        blocks.append((height, block_height, fixture, label_lines, names, time_lines))
        height += block_height + 12
    image = Image.new("RGB", (800, height + 35), "#141c29")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 0, 772, 4), fill="#568bdf")
    for index, line in enumerate(heading):
        draw.text((34, 30 + index * 42), line, font=_font(34, True), fill="#f2f5fa", anchor="lt")
    draw.text((34, top - 36), page["subtitle"], font=_font(23), fill="#b8c5d6", anchor="lt")
    for y, block_height, fixture, label_lines, names, time_lines in blocks:
        draw.rounded_rectangle((24, y, 776, y + block_height), radius=8, fill="#1c2838")
        _paste_logo(image, logos, _value(fixture, "league", "logo"), 36, y + 10, 24)
        line_y = y + 12
        for line in label_lines:
            draw.text((70, line_y), line, font=_font(21), fill="#b8c5d6", anchor="lt")
            line_y += 27
        line_y += 8
        for side, lines in zip(("home", "away"), names):
            _paste_logo(image, logos, _value(fixture, "teams", side, "logo"), 36, line_y, 34)
            for index, line in enumerate(lines):
                draw.text((86, line_y + index * 33), line, font=_font(27, True), fill="#f2f5fa", anchor="lt")
            score = _value(fixture, "goals", side)
            draw.text((744, line_y), "-" if score is None else str(score), font=_font(29, True), fill="#f2f5fa", anchor="rt")
            line_y += max(42, len(lines) * 33 + 8)
        for line in time_lines:
            draw.text((36, line_y), line, font=_font(21), fill="#b8c5d6", anchor="lt")
            line_y += 27
    if total > 1:
        draw.text((770, height + 7), f"{number}/{total}", font=_font(18), fill="#b8c5d6", anchor="rt")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def match_pages(match: Any, section: str, *, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_datetime, fixture_score

    fixture = match.fixture
    home, away = fixture_teams(fixture)
    title = f"{home} {fixture_score(fixture)} {away}"
    league = fixture.get("league") or {}
    subtitle = f"{league.get('name') or tr(lang, 'Football', 'Fútbol')} / {_round_label(league.get('round') or '', lang)} / {fixture_datetime(fixture)} / {match.status}"
    media = [_value(fixture, "teams", side, "logo") for side in ("home", "away")]
    media.append(league.get("logo"))

    def pages(rows, label):
        result = [page for start in range(0, len(rows), 4)
                  for page in _pages(title, f"{subtitle} / {label}", rows[start:start + 4], media)]
        for page in result:
            page["match"] = fixture
        return result

    if section in {"overview", "periods"}:
        rows = []
        for key, en, es in [("halftime", "Half-time", "Descanso"), ("fulltime", "After 90 minutes", "Tras 90 minutos"),
                            ("extratime", "After extra time", "Tras prorroga"), ("penalty", "Penalty shootout", "Tanda de penales")]:
            score = (fixture.get("score") or {}).get(key) or {}
            if score.get("home") is not None or score.get("away") is not None:
                rows.append((tr(lang, en, es), f"{score.get('home') if score.get('home') is not None else 'N/A'} - "
                             f"{score.get('away') if score.get('away') is not None else 'N/A'}"))
        if section == "overview":
            rows.extend([(tr(lang, "Venue", "Estadio"), _value(fixture, "fixture", "venue", "name")),
                         (tr(lang, "Referee", "Arbitro"), _value(fixture, "fixture", "referee"))])
        return pages(rows, tr(lang, "Match", "Partido")) if rows else []

    if section == "statistics":
        return match_statistics_pages(match, match.statistics, tr(lang, "Full match", "Partido completo"), lang=lang)
    if section == "events":
        rows = []
        for event in match.normalized_events:
            minute = str(event.elapsed) if event.elapsed is not None else "?"
            if event.extra is not None:
                minute += f"+{event.extra}"
            label = f"{minute}' / {event.team_name} / {_provider_term(event.event_type, lang)}"
            details = " / ".join(value for value in [event.player_name, _provider_term(event.detail, lang),
                tr(lang, f"Assist / other player: {event.assist_name}", f"Asistencia / otro jugador: {event.assist_name}") if event.assist_name else "",
                _provider_term(str(event.raw.get("comments") or ""), lang)] if value)
            rows.append((label, details or "N/A"))
        result = []
        for start in range(0, len(rows), 4):
            result.extend(pages(rows[start:start + 4], tr(lang, "Events", "Eventos")))
        return result
    if section == "lineups":
        result = []
        for lineup in match.lineups:
            team = lineup.get("team") or {}
            label = f"{team.get('name') or tr(lang, 'Team', 'Equipo')} / {lineup.get('formation') or 'N/A'}"
            starters = [item.get("player") or {} for item in lineup.get("startXI") or []]
            coach = _value(lineup, "coach", "name")
            label += f" / {tr(lang, 'Coach', 'Entrenador')}: {coach or 'N/A'}"
            lineup_pages = []
            for key, en, es in [("startXI", "Starting XI", "Once inicial"), ("substitutes", "Substitutes", "Suplentes")]:
                rows = []
                for item in lineup.get(key) or []:
                    player = item.get("player") or {}
                    rows.append((f"{player.get('number') if player.get('number') is not None else '-'} / {_provider_term(player.get('pos') or 'N/A', lang)}",
                                 player.get('name') or 'N/A'))
                if not rows:
                    rows = [(tr(lang, en, es), tr(lang, "Not supplied", "No proporcionados"))]
                for page in _pages(title, f"{subtitle} / {label} / {tr(lang, en, es)}", rows, media):
                    page["match"], page["roster"] = fixture, True
                    lineup_pages.append(page)
            positions = []
            for player in starters:
                grid = str(player.get("grid") or "").split(":")
                if len(grid) != 2 or not all(value.isdigit() and 1 <= int(value) <= 11 for value in grid):
                    break
                positions.append((int(grid[0]), int(grid[1]), player))
            if starters and len(positions) == len(starters) and len({(x,y) for x,y,_ in positions}) == len(positions):
                result.append({"title": title, "subtitle": f"{subtitle} / {label}", "match": fixture,
                               "media": media, "pitch": positions,
                               "rows": [(tr(lang, "Starting XI", "Once inicial"), "\n".join(
                                   f"{player.get('number', '-')} / {player.get('name') or 'N/A'} / {x}:{y}" for x,y,player in positions))]})
            result.extend(lineup_pages)
        return result
    if section == "players":
        result = []
        primary = {"games_minutes": ("Minutes", "Minutos"), "games_rating": ("Rating", "Valoracion"),
                   "goals_total": ("Goals", "Goles"), "goals_assists": ("Assists", "Asistencias"),
                   "goals_saves": ("Saves", "Paradas"), "goals_conceded": ("Conceded", "Recibidos")}
        labels = {"_".join(path): (en, es) for path, en, es in _PLAYER_METRICS}
        labels.update(primary)
        labels.update({"games_number": ("Number", "Número"), "games_position": ("Position", "Posición"),
                       "games_captain": ("Captain", "Capitán"), "games_substitute": ("Substitute", "Suplente"),
                       "offsides": ("Offsides", "Fueras de lugar"), "penalty_committed": ("Penalties conceded", "Penales cometidos")})
        for performance in match.player_performances:
            keys = list(dict.fromkeys([*[key for key in primary if key in performance.metrics], *performance.metrics]))
            rows = [(tr(lang, *labels[key]) if key in labels else key.replace("_", " ").title(),
                     _provider_term(performance.metrics[key], lang) if key in {"games_position", "games_captain", "games_substitute"}
                     else performance.metrics[key]) for key in keys]
            if not rows:
                rows = [(tr(lang, "Performance", "Actuacion"), tr(lang, "Not supplied", "No proporcionada"))]
            result.extend(_pages(performance.player_name, f"{performance.team_name} / {title} / {subtitle}", rows,
                                 [_value(performance.raw, "player", "photo"),
                                  next((_value(fixture, "teams", side, "logo") for side in ("home", "away")
                                        if _value(fixture, "teams", side, "id") == performance.team_id), None),
                                  league.get("logo")]))
        return result
    return []


def match_statistics_pages(match: Any, statistics: list[dict[str, Any]], period: str, *, lang: str) -> list[dict[str, Any]]:
    from services.football_formatter import fixture_teams, fixture_score, fixture_datetime
    from services.football_live_match_service import canonical_api_stat_key

    labels_es = {
        "shots_on_goal": "Tiros a puerta", "shots_off_goal": "Tiros fuera",
        "total_shots": "Tiros totales", "blocked_shots": "Tiros bloqueados",
        "shots_insidebox": "Tiros dentro del área", "shots_outsidebox": "Tiros fuera del área",
        "fouls": "Faltas", "corner_kicks": "Tiros de esquina", "offsides": "Fueras de lugar",
        "ball_possession": "Posesión del balón", "yellow_cards": "Tarjetas amarillas",
        "red_cards": "Tarjetas rojas", "goalkeeper_saves": "Atajadas",
        "total_passes": "Pases totales", "passes_accurate": "Pases completados",
        "passes_percent": "Precisión de pase", "expected_goals": "Goles esperados (xG)",
        "goals_prevented": "Goles evitados", "free_kicks": "Tiros libres",
        "goal_kicks": "Saques de meta", "throw_ins": "Saques de banda",
    }
    home, away = fixture_teams(match.fixture)
    sides = []
    for side, name in [("home", home), ("away", away)]:
        team_id = _value(match.fixture, "teams", side, "id")
        side_rows = [row for row in statistics if (row.get("team") or {}).get("id") == team_id] if team_id is not None else []
        if not side_rows:
            side_rows = [row for row in statistics if (row.get("team") or {}).get("id") is None and
                         (row.get("team") or {}).get("name") == name]
        sides.append({str(item.get("type")): item.get("value") for row in side_rows
                      for item in row.get("statistics") or [] if item.get("type")})
    labels = list(dict.fromkeys([*sides[0], *sides[1]]))
    comparisons = [(tr(lang, label, labels_es.get(canonical_api_stat_key(label), label)),
                    *["N/A" if side.get(label) is None else str(side[label]) for side in sides]) for label in labels]
    league = match.fixture.get("league") or {}
    return [{"title": f"{home} {fixture_score(match.fixture)} {away}",
             "subtitle": f"{league.get('name') or 'Football'} / {period} / {fixture_datetime(match.fixture)} / {match.status}",
             "match": match.fixture, "comparison": comparisons[start:start + 6], "sides": (home, away),
             "media": [_value(match.fixture, "teams", side, "logo") for side in ("home", "away")]}
            for start in range(0, len(comparisons), 6)]


def render_match(page: dict[str, Any], *, lang: str, logos: dict[str, bytes], number: int, total: int) -> BytesIO:
    from services.football_formatter import fixture_teams, fixture_score, fixture_status

    fixture = page["match"]
    home, away = fixture_teams(fixture)
    sides = [_wrap(name, _font(28, True), 275) for name in (home, away)]
    subtitle = _wrap(page.get("subtitle", ""), _font(22), 730)
    heading_bottom = max(170, 116 + max(map(len, sides)) * 34)
    top = heading_bottom + len(subtitle) * 29 + 24
    comparisons, pitch = page.get("comparison"), page.get("pitch")
    blocks, height = [], top
    if comparisons:
        for label, left, right in comparisons:
            labels = _wrap(label, _font(24), 480)
            values = [_wrap(value, _font(29, True), 90) for value in (left,right)]
            row_height = max(len(labels) * 30 + 26, max(map(len, values)) * 36 + 26)
            blocks.append((height, row_height, labels, values))
            height += row_height + 12
    elif pitch:
        pitch_rows = {}
        pitch_height = 24
        for row in range(1, max(x for x,y,player in pitch) + 1):
            players = [(column, player) for x,column,player in pitch if x == row]
            columns = max((column for column, player in players), default=1)
            names = {column: _wrap(player.get("name") or "N/A", _font(22), int(752/(columns+1))-12)
                     for column, player in players}
            pitch_rows[row] = (pitch_height, columns, names)
            pitch_height += 64 + max((len(lines) for lines in names.values()), default=1) * 27
        height += pitch_height + 20
    elif page.get("roster"):
        for label, value in page["rows"]:
            labels, values = _wrap(label, _font(22), 100), _wrap(value, _font(27), 590)
            row_height = 20 + max(len(labels) * 28, len(values) * 33)
            blocks.append((height, row_height, labels, values))
            height += row_height + 4
    else:
        for label, value in page.get("rows", []):
            labels = _wrap(label, _font(22), 700)
            values = _wrap("N/A" if value is None else str(value), _font(27, True), 700)
            row_height = 28 + len(labels) * 28 + len(values) * 33
            blocks.append((height, row_height, labels, values))
            height += row_height + 12
    image = Image.new("RGB", (800, height + 42), "#141c29")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28,0,772,4),fill="#568bdf")
    for index, side in enumerate(("home", "away")):
        center = 180 if index == 0 else 620
        _paste_logo(image, logos, _value(fixture,"teams",side,"logo"),center-38,20,76)
        for line_index, line in enumerate(sides[index]):
            draw.text((center,116+line_index*34),line,font=_font(28,True),fill="#f2f5fa",anchor="mt")
    _paste_logo(image, logos, _value(fixture, "league", "logo"), 385, 8, 30)
    draw.text((400,48),fixture_score(fixture),font=_font(38,True),fill="#f2f5fa",anchor="mt")
    draw.text((400,96),fixture_status(fixture.get("fixture") or {}),font=_font(21),fill="#b8c5d6",anchor="mt")
    for index, line in enumerate(subtitle):
        draw.text((34,heading_bottom+index*29),line,font=_font(22),fill="#b8c5d6",anchor="lt")
    if pitch:
        draw.rounded_rectangle((24,top,776,top+pitch_height),radius=8,fill="#1b4039",outline="#70958c",width=2)
        draw.line((24,top+pitch_height//2,776,top+pitch_height//2),fill="#70958c",width=2)
        draw.ellipse((350,top+pitch_height//2-50,450,top+pitch_height//2+50),outline="#70958c",width=2)
        for row,column,player in pitch:
            offset, columns, names = pitch_rows[row]
            center = int(24 + column * 752/(columns+1))
            y = top + offset
            draw.ellipse((center-18,y,center+18,y+36),fill="#568bdf")
            draw.text((center,y+18),str(player.get("number") if player.get("number") is not None else "-"),font=_font(20,True),fill="#ffffff",anchor="mm")
            for index, line in enumerate(names[column]):
                draw.text((center,y+42+index*27),line,font=_font(22),fill="#f2f5fa",anchor="mt")
    elif comparisons:
        for y,row_height,labels,values in blocks:
            draw.rounded_rectangle((24,y,776,y+row_height),radius=7,fill="#1b2637")
            for index,line in enumerate(labels):
                draw.text((400,y+13+index*30),line,font=_font(24),fill="#b8c5d6",anchor="mt")
            for index,lines in enumerate(values):
                for line_index,line in enumerate(lines):
                    draw.text((78 if index==0 else 722,y+13+line_index*36),line,font=_font(29,True),fill="#f2f5fa",anchor="mt")
    elif page.get("roster"):
        for y,row_height,labels,values in blocks:
            draw.rounded_rectangle((24,y,776,y+row_height),radius=5,fill="#1b2637")
            for index,line in enumerate(labels):
                draw.text((42,y+10+index*28),line,font=_font(22),fill="#b8c5d6",anchor="lt")
            for index,line in enumerate(values):
                draw.text((166,y+10+index*33),line,font=_font(27),fill="#f2f5fa",anchor="lt")
    else:
        for y,row_height,labels,values in blocks:
            draw.rounded_rectangle((24,y,776,y+row_height),radius=7,fill="#1b2637")
            for index,line in enumerate(labels):
                draw.text((46,y+14+index*28),line,font=_font(22),fill="#b8c5d6",anchor="lt")
            for index,line in enumerate(values):
                draw.text((46,y+18+len(labels)*28+index*33),line,font=_font(27,True),fill="#f2f5fa",anchor="lt")
    if total > 1:
        draw.text((768,image.height-18),f"{number}/{total}",font=_font(18),fill="#b8c5d6",anchor="rm")
    output=BytesIO();image.save(output,format="PNG");output.seek(0)
    return output
