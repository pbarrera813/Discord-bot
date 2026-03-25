from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Any, Final

import discord
from discord import app_commands
from discord.ext import commands

from utils.i18n import tr


LEAGUE_CHOICES: Final[list[app_commands.Choice[str]]] = [
    app_commands.Choice(name="ligamx", value="ligamx"),
    app_commands.Choice(name="premier", value="premier"),
    app_commands.Choice(name="laliga", value="laliga"),
    app_commands.Choice(name="concacaf", value="concacaf"),
]
LEAGUE_CODES: Final[tuple[str, ...]] = tuple(choice.value for choice in LEAGUE_CHOICES)
LEAGUE_HELP_TEXT: Final[str] = f"League: {', '.join(LEAGUE_CODES)}"
LEAGUE_INVALID_TEXT_EN: Final[str] = f"Invalid league. Use one of: {', '.join(LEAGUE_CODES)}."
LEAGUE_INVALID_TEXT_ES: Final[str] = f"Liga inválida. Usa una de estas: {', '.join(LEAGUE_CODES)}."


class FootballCog(commands.Cog):
    _LEAGUE_ALIASES: Final[dict[str, str]] = {
        "ligamx": "ligamx",
        "ligabbvamx": "ligamx",
        "premier": "premier",
        "premierleague": "premier",
        "epl": "premier",
        "laliga": "laliga",
        "laligasantander": "laliga",
        "concacaf": "concacaf",
        "concacafchampions": "concacaf",
        "concacafchampionscup": "concacaf",
        "concacafchampionsleague": "concacaf",
    }

    _LEAGUE_LABELS: Final[dict[str, tuple[str, str]]] = {
        "ligamx": ("Liga BBVA MX", "Liga BBVA MX"),
        "premier": ("Premier League", "Premier League"),
        "laliga": ("LaLiga", "LaLiga"),
        "concacaf": ("CONCACAF Champions Cup", "Copa de Campeones CONCACAF"),
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _client_or_message(self, ctx: commands.Context, lang: str) -> Any | None:
        client = getattr(self.bot, "api_football_client", None)
        if client is not None:
            return client
        await ctx.send(
            tr(
                lang,
                "API-Football is not configured. Add `API_FOOTBALL_KEY` in `.env`.",
                "API-Football no está configurada. Agrega `API_FOOTBALL_KEY` en `.env`.",
            )
        )
        return None

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[\s_\-]+", "", value).casefold()

    def _normalize_league_key(self, raw: str | None) -> str | None:
        if not raw:
            return None
        return self._LEAGUE_ALIASES.get(self._slug(raw))

    def _league_label(self, league_key: str, lang: str) -> str:
        en, es = self._LEAGUE_LABELS.get(league_key, (league_key, league_key))
        return tr(lang, en, es)

    async def _resolve_league_context(
        self,
        ctx: commands.Context,
        *,
        client: Any,
        lang: str,
        raw_league: str,
    ) -> tuple[str, int, int] | None:
        league_key = self._normalize_league_key(raw_league)
        if league_key is None:
            await ctx.send(
                tr(
                    lang,
                    LEAGUE_INVALID_TEXT_EN,
                    LEAGUE_INVALID_TEXT_ES,
                )
            )
            return None
        league_id = await client.resolve_league_id(league_key)
        season = await client.get_current_season(league_id)
        return league_key, league_id, season

    @staticmethod
    def _fixture_status(fixture: dict[str, Any]) -> str:
        status = fixture.get("status")
        if not isinstance(status, dict):
            return "N/A"
        short = str(status.get("short", "")).strip()
        long = str(status.get("long", "")).strip()
        elapsed = status.get("elapsed")
        if isinstance(elapsed, int):
            return f"{short or long} {elapsed}'".strip()
        return short or long or "N/A"

    @staticmethod
    def _fixture_round(item: dict[str, Any]) -> str:
        league = item.get("league")
        if isinstance(league, dict):
            round_name = league.get("round")
            if isinstance(round_name, str) and round_name.strip():
                return round_name.strip()
        return ""

    @staticmethod
    def _fixture_teams(item: dict[str, Any]) -> tuple[str, str]:
        teams = item.get("teams")
        if not isinstance(teams, dict):
            return "Unknown", "Unknown"
        home = teams.get("home")
        away = teams.get("away")
        home_name = home.get("name") if isinstance(home, dict) else "Unknown"
        away_name = away.get("name") if isinstance(away, dict) else "Unknown"
        return str(home_name or "Unknown"), str(away_name or "Unknown")

    @staticmethod
    def _fixture_team_logos(item: dict[str, Any]) -> tuple[str | None, str | None]:
        teams = item.get("teams")
        if not isinstance(teams, dict):
            return None, None
        home = teams.get("home")
        away = teams.get("away")
        home_logo = home.get("logo") if isinstance(home, dict) else None
        away_logo = away.get("logo") if isinstance(away, dict) else None
        home_logo_url = (
            str(home_logo).strip()
            if isinstance(home_logo, str) and str(home_logo).strip()
            else None
        )
        away_logo_url = (
            str(away_logo).strip()
            if isinstance(away_logo, str) and str(away_logo).strip()
            else None
        )
        return home_logo_url, away_logo_url

    @staticmethod
    def _fixture_score(item: dict[str, Any]) -> str:
        goals = item.get("goals")
        if not isinstance(goals, dict):
            return "vs"
        home = goals.get("home")
        away = goals.get("away")
        if home is None and away is None:
            return "vs"
        return f"{home if home is not None else '-'} - {away if away is not None else '-'}"

    @staticmethod
    def _format_fixture_line(item: dict[str, Any]) -> tuple[str, str]:
        fixture = item.get("fixture")
        if not isinstance(fixture, dict):
            fixture = {}
        home, away = FootballCog._fixture_teams(item)
        score = FootballCog._fixture_score(item)
        status = FootballCog._fixture_status(fixture)
        round_name = FootballCog._fixture_round(item)
        title = f"{home} vs {away}"
        details = f"**{score}**\n{status}"
        if round_name:
            details = f"{details}\n{round_name}"
        return title, details

    @staticmethod
    def _match_datetime(item: dict[str, Any]) -> str:
        fixture = item.get("fixture")
        if not isinstance(fixture, dict):
            return "N/A"
        iso = fixture.get("date")
        if isinstance(iso, str) and iso.strip():
            return iso.strip().replace("T", " ").replace("+00:00", " UTC")
        return "N/A"

    def _build_match_embed(
        self,
        *,
        lang: str,
        league_label: str,
        item: dict[str, Any],
        title_en: str,
        title_es: str,
        color: discord.Color,
        index: int,
        total: int,
    ) -> discord.Embed:
        fixture = item.get("fixture")
        if not isinstance(fixture, dict):
            fixture = {}
        home, away = self._fixture_teams(item)
        home_logo, away_logo = self._fixture_team_logos(item)
        score = self._fixture_score(item)
        status = self._fixture_status(fixture)
        round_name = self._fixture_round(item)
        match_time = self._match_datetime(item)

        page_suffix = f" ({index}/{total})" if total > 1 else ""
        title = tr(
            lang,
            f"{league_label} - {title_en}{page_suffix}",
            f"{league_label} - {title_es}{page_suffix}",
        )
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        if home_logo:
            embed.set_author(name=home, icon_url=home_logo)
        else:
            embed.set_author(name=home)

        details = [f"**{home} vs {away}**", f"**{score}**", status, match_time]
        if round_name:
            details.append(round_name)
        embed.description = "\n".join(details)[:4096]

        if away_logo:
            embed.set_footer(text=away, icon_url=away_logo)
        else:
            embed.set_footer(text=away)

        return embed

    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                return

    @commands.hybrid_group(
        name="football",
        description="Football commands for selected leagues.",
        fallback="live",
        invoke_without_command=True,
    )
    @app_commands.describe(league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football(self, ctx: commands.Context, league: str) -> None:
        await self._run_football_live(ctx, league)

    async def _run_football_live(self, ctx: commands.Context, raw_league: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=raw_league,
            )
            if context is None:
                return
            league_key, league_id, _season = context
            fixtures = await client.get_live_fixtures(league_id=league_id, cache_ttl_seconds=30)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get live matches: {exc}",
                    f"No se pudieron obtener los partidos en vivo: {exc}",
                )
            )
            return

        league_label = self._league_label(league_key, lang)
        if not fixtures:
            await ctx.send(
                tr(
                    lang,
                    f"No live matches in {league_label} right now.",
                    f"No hay partidos en vivo de {league_label} en este momento.",
                )
            )
            return

        shown = min(len(fixtures), 10)
        embeds: list[discord.Embed] = []
        for idx, item in enumerate(fixtures[:shown], start=1):
            embeds.append(
                self._build_match_embed(
                    lang=lang,
                    league_label=league_label,
                    item=item,
                    title_en="Live",
                    title_es="En vivo",
                    color=discord.Color.green(),
                    index=idx,
                    total=shown,
                )
            )

        if len(fixtures) > shown:
            embeds[-1].add_field(
                name=tr(lang, "Notice", "Aviso"),
                value=tr(
                    lang,
                    f"Showing {shown} of {len(fixtures)} live matches.",
                    f"Mostrando {shown} de {len(fixtures)} partidos en vivo.",
                ),
                inline=False,
            )

        await ctx.send(embeds=embeds)

    @football.command(
        name="today",
        description="Get today's fixtures for a selected league.",
    )
    @app_commands.describe(league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_today(self, ctx: commands.Context, league: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            today = date.today().isoformat()
            fixtures = await client.get_fixtures_on_date(
                league_id=league_id,
                season=season,
                date_iso=today,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get today's fixtures: {exc}",
                    f"No se pudieron obtener los partidos de hoy: {exc}",
                )
            )
            return

        league_label = self._league_label(league_key, lang)
        if not fixtures:
            await ctx.send(
                tr(
                    lang,
                    f"No fixtures for today in {league_label}.",
                    f"No hay partidos de hoy en {league_label}.",
                )
            )
            return

        shown = min(len(fixtures), 10)
        embeds: list[discord.Embed] = []
        for idx, item in enumerate(fixtures[:shown], start=1):
            embeds.append(
                self._build_match_embed(
                    lang=lang,
                    league_label=league_label,
                    item=item,
                    title_en="Today",
                    title_es="Hoy",
                    color=discord.Color.blurple(),
                    index=idx,
                    total=shown,
                )
            )

        await ctx.send(embeds=embeds)

    @football.command(
        name="next",
        description="Get next fixtures (or next match for a team) in a selected league.",
    )
    @app_commands.describe(
        league=LEAGUE_HELP_TEXT,
        target="Optional: number of matches (1-10) or a team name",
    )
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_next(
        self,
        ctx: commands.Context,
        league: str,
        *,
        target: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        target_text = (target or "").strip()
        if target_text and not target_text.isdigit():
            await self._run_next_for_team(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
                team_query=target_text,
            )
            return

        count = 5
        if target_text.isdigit():
            count = int(target_text)
        count = max(1, min(count, 10))
        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            fixtures = await client.get_next_fixtures(
                league_id=league_id,
                season=season,
                next_count=count,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get upcoming fixtures: {exc}",
                    f"No se pudieron obtener los proximos partidos: {exc}",
                )
            )
            return

        league_label = self._league_label(league_key, lang)
        if not fixtures:
            await ctx.send(
                tr(
                    lang,
                    f"No upcoming fixtures found in {league_label}.",
                    f"No se encontraron proximos partidos en {league_label}.",
                )
            )
            return

        shown = min(len(fixtures), count)
        embeds: list[discord.Embed] = []
        for idx, item in enumerate(fixtures[:shown], start=1):
            embeds.append(
                self._build_match_embed(
                    lang=lang,
                    league_label=league_label,
                    item=item,
                    title_en="Upcoming",
                    title_es="Proximos",
                    color=discord.Color.gold(),
                    index=idx,
                    total=shown,
                )
            )

        await ctx.send(embeds=embeds)

    async def _run_next_for_team(
        self,
        ctx: commands.Context,
        *,
        client: Any,
        lang: str,
        raw_league: str,
        team_query: str,
    ) -> None:
        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=raw_league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            teams = await client.search_teams(
                name=team_query,
                league_id=league_id,
                season=season,
            )
            selected = self._pick_team(teams, team_query)
            if selected is None:
                await ctx.send(
                    tr(
                        lang,
                        "Team not found in that league.",
                        "No se encontro ese equipo en esa liga.",
                    )
                )
                return

            team_info = selected.get("team", {}) if isinstance(selected, dict) else {}
            team_id = team_info.get("id") if isinstance(team_info, dict) else None
            team_name = str(team_info.get("name", team_query))
            if not isinstance(team_id, int):
                await ctx.send(
                    tr(
                        lang,
                        "Could not resolve a valid team ID for that team.",
                        "No se pudo resolver un ID valido para ese equipo.",
                    )
                )
                return

            fixtures = await client.get_next_fixtures(
                league_id=league_id,
                season=season,
                next_count=1,
                team_id=team_id,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get upcoming team match: {exc}",
                    f"No se pudo obtener el proximo partido del equipo: {exc}",
                )
            )
            return

        if not fixtures:
            await ctx.send(
                tr(
                    lang,
                    "No upcoming match found for that team.",
                    "No se encontro un proximo partido para ese equipo.",
                )
            )
            return
        league_label = self._league_label(league_key, lang)
        item = fixtures[0]
        title, details = self._format_fixture_line(item)
        match_time = self._match_datetime(item)
        round_name = self._fixture_round(item)
        home, away = self._fixture_teams(item)
        home_logo, away_logo = self._fixture_team_logos(item)

        embed = discord.Embed(
            title=tr(
                lang,
                f"{league_label} Next Match: {team_name}",
                f"Proximo partido de {league_label}: {team_name}",
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        if home_logo:
            embed.set_author(name=home, icon_url=home_logo)
        elif home:
            embed.set_author(name=home)
        embed.add_field(
            name=tr(lang, "Match", "Partido"),
            value=title,
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Date", "Fecha"),
            value=match_time,
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Details", "Detalles"),
            value=details[:1024],
            inline=False,
        )
        if round_name:
            embed.add_field(
                name=tr(lang, "Round", "Jornada"),
                value=round_name,
                inline=False,
            )
        if away_logo:
            embed.set_footer(text=away, icon_url=away_logo)
        elif away:
            embed.set_footer(text=away)

        await ctx.send(embed=embed)

    @football.command(
        name="last",
        description="Get the last played match for a team in a selected league.",
    )
    @app_commands.describe(
        league=LEAGUE_HELP_TEXT,
        team="Team name",
    )
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_last(self, ctx: commands.Context, league: str, *, team: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        query = team.strip()
        if not query:
            await ctx.send(tr(lang, "Provide a team name.", "Proporciona un nombre de equipo."))
            return

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            teams = await client.search_teams(name=query, league_id=league_id, season=season)
            selected = self._pick_team(teams, query)
            if selected is None:
                await ctx.send(
                    tr(
                        lang,
                        "Team not found in that league.",
                        "No se encontro ese equipo en esa liga.",
                    )
                )
                return

            team_info = selected.get("team", {}) if isinstance(selected, dict) else {}
            team_id = team_info.get("id") if isinstance(team_info, dict) else None
            team_name = str(team_info.get("name", query))
            if not isinstance(team_id, int):
                await ctx.send(
                    tr(
                        lang,
                        "Could not resolve a valid team ID for that team.",
                        "No se pudo resolver un ID valido para ese equipo.",
                    )
                )
                return

            fixtures = await client.get_last_fixtures(
                league_id=league_id,
                season=season,
                last_count=1,
                team_id=team_id,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get last match data: {exc}",
                    f"No se pudieron obtener los datos del ultimo partido: {exc}",
                )
            )
            return

        if not fixtures:
            await ctx.send(
                tr(
                    lang,
                    "No last match data found for that team.",
                    "No se encontraron datos del ultimo partido para ese equipo.",
                )
            )
            return

        league_label = self._league_label(league_key, lang)
        item = fixtures[0]
        home, away = self._fixture_teams(item)
        home_logo, away_logo = self._fixture_team_logos(item)
        score = self._fixture_score(item)
        fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
        status = self._fixture_status(fixture)
        round_name = self._fixture_round(item)
        match_time = self._match_datetime(item)

        embed = discord.Embed(
            title=tr(
                lang,
                f"{league_label} Last Match: {team_name}",
                f"Ultimo partido de {league_label}: {team_name}",
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        if home_logo:
            embed.set_author(name=home, icon_url=home_logo)
        elif home:
            embed.set_author(name=home)
        embed.add_field(
            name=tr(lang, "Match", "Partido"),
            value=f"{home} vs {away}",
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Result", "Resultado"),
            value=f"**{score}**",
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Status", "Estado"),
            value=status,
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Date", "Fecha"),
            value=match_time,
            inline=False,
        )
        if round_name:
            embed.add_field(
                name=tr(lang, "Round", "Jornada"),
                value=round_name,
                inline=False,
            )
        if away_logo:
            embed.set_footer(text=away, icon_url=away_logo)
        elif away:
            embed.set_footer(text=away)

        await ctx.send(embed=embed)

    @football.command(
        name="table",
        description="Get standings for a selected league.",
    )
    @app_commands.describe(league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_table(self, ctx: commands.Context, league: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            rows = await client.get_standings(league_id=league_id, season=season)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get standings: {exc}",
                    f"No se pudieron obtener las posiciones: {exc}",
                )
            )
            return

        table = self._extract_table_rows(rows)
        if not table:
            await ctx.send(
                tr(
                    lang,
                    "No standings data available right now.",
                    "No hay datos de tabla disponibles en este momento.",
                )
            )
            return

        lines = []
        for row in table[:20]:
            rank = row.get("rank", "-")
            team_name = self._team_name_from_row(row)
            points = row.get("points", 0)
            played = row.get("all", {}).get("played", 0) if isinstance(row.get("all"), dict) else 0
            gd = row.get("goalsDiff", 0)
            lines.append(f"`{rank:>2}` {team_name} - {points} pts | PJ {played} | DG {gd}")

        league_label = self._league_label(league_key, lang)
        embed = discord.Embed(
            title=tr(lang, f"{league_label} - Table", f"{league_label} - Tabla"),
            description="\n".join(lines)[:4000],
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.send(embed=embed)

    @football.command(
        name="team",
        description="Get a team snapshot in a selected league.",
    )
    @app_commands.describe(
        league=LEAGUE_HELP_TEXT,
        team="Team name",
    )
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_team(self, ctx: commands.Context, league: str, *, team: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        query = team.strip()
        if not query:
            await ctx.send(tr(lang, "Provide a team name.", "Proporciona un nombre de equipo."))
            return

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            teams = await client.search_teams(name=query, league_id=league_id, season=season)
            selected = self._pick_team(teams, query)
            if selected is None:
                await ctx.send(
                    tr(
                        lang,
                        "Team not found in that league.",
                        "No se encontro ese equipo en esa liga.",
                    )
                )
                return

            team_info = selected.get("team", {}) if isinstance(selected, dict) else {}
            team_id = team_info.get("id") if isinstance(team_info, dict) else None
            team_name = str(team_info.get("name", query))

            standings_raw = await client.get_standings(league_id=league_id, season=season)
            standings = self._extract_table_rows(standings_raw)
            standing_row = self._find_team_row(standings, team_id)

            next_fixture = None
            if isinstance(team_id, int):
                upcoming = await client.get_next_fixtures(
                    league_id=league_id,
                    season=season,
                    next_count=1,
                    team_id=team_id,
                )
                if upcoming:
                    next_fixture = upcoming[0]
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get team data: {exc}",
                    f"No se pudieron obtener los datos del equipo: {exc}",
                )
            )
            return

        league_label = self._league_label(league_key, lang)
        embed = discord.Embed(
            title=tr(
                lang,
                f"{league_label} Team: {team_name}",
                f"Equipo de {league_label}: {team_name}",
            ),
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc),
        )

        if standing_row:
            form = standing_row.get("form") or "N/A"
            all_data = standing_row.get("all")
            played = won = drawn = lost = 0
            gf = ga = 0
            if isinstance(all_data, dict):
                played = int(all_data.get("played", 0) or 0)
                won = int(all_data.get("win", 0) or 0)
                drawn = int(all_data.get("draw", 0) or 0)
                lost = int(all_data.get("lose", 0) or 0)
                goals = all_data.get("goals")
                if isinstance(goals, dict):
                    gf = int(goals.get("for", 0) or 0)
                    ga = int(goals.get("against", 0) or 0)
            embed.add_field(
                name=tr(lang, "Position", "Posicion"),
                value=str(standing_row.get("rank", "-")),
                inline=True,
            )
            embed.add_field(
                name=tr(lang, "Points", "Puntos"),
                value=str(standing_row.get("points", 0)),
                inline=True,
            )
            embed.add_field(name=tr(lang, "Form", "Forma"), value=str(form), inline=True)
            embed.add_field(
                name=tr(lang, "Record", "Registro"),
                value=f"PJ {played} | G {won} E {drawn} P {lost}",
                inline=False,
            )
            embed.add_field(
                name=tr(lang, "Goals", "Goles"),
                value=f"GF {gf} | GC {ga}",
                inline=False,
            )

        if isinstance(next_fixture, dict):
            title, details = self._format_fixture_line(next_fixture)
            match_time = self._match_datetime(next_fixture)
            embed.add_field(
                name=tr(lang, "Next match", "Proximo partido"),
                value=f"{title}\n{match_time}\n{details}"[:1024],
                inline=False,
            )

        await ctx.send(embed=embed)

    @football.command(
        name="scorers",
        description="Get top scorers for a selected league.",
    )
    @app_commands.describe(league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_scorers(self, ctx: commands.Context, league: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            context = await self._resolve_league_context(
                ctx,
                client=client,
                lang=lang,
                raw_league=league,
            )
            if context is None:
                return
            league_key, league_id, season = context
            scorers = await client.get_top_scorers(league_id=league_id, season=season)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get top scorers: {exc}",
                    f"No se pudieron obtener los goleadores: {exc}",
                )
            )
            return

        if not scorers:
            await ctx.send(
                tr(
                    lang,
                    "No top scorer data available right now.",
                    "No hay datos de goleadores disponibles en este momento.",
                )
            )
            return

        lines = []
        for index, row in enumerate(scorers[:10], start=1):
            player = row.get("player") if isinstance(row, dict) else {}
            stats_list = row.get("statistics") if isinstance(row, dict) else None
            stats = stats_list[0] if isinstance(stats_list, list) and stats_list else {}
            team = stats.get("team") if isinstance(stats, dict) else {}
            goals_data = stats.get("goals") if isinstance(stats, dict) else {}
            assists = goals_data.get("assists") if isinstance(goals_data, dict) else None
            goals = goals_data.get("total") if isinstance(goals_data, dict) else None
            name = str(player.get("name", "Unknown")) if isinstance(player, dict) else "Unknown"
            team_name = str(team.get("name", "N/A")) if isinstance(team, dict) else "N/A"
            assists_text = assists if isinstance(assists, int) else 0
            goals_text = goals if isinstance(goals, int) else 0
            lines.append(
                tr(
                    lang,
                    f"`{index:>2}.` {name} ({team_name}) - Goals {goals_text} | Assists {assists_text}",
                    f"`{index:>2}.` {name} ({team_name}) - Goles {goals_text} | Asistencias {assists_text}",
                )
            )

        league_label = self._league_label(league_key, lang)
        embed = discord.Embed(
            title=tr(lang, f"{league_label} - Top Scorers", f"{league_label} - Goleadores"),
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.send(embed=embed)
    @staticmethod
    def _extract_table_rows(standings_response: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            return [row for row in standings[0] if isinstance(row, dict)]
        return [row for row in standings if isinstance(row, dict)]

    @staticmethod
    def _team_name_from_row(row: dict[str, Any]) -> str:
        team = row.get("team")
        if isinstance(team, dict):
            name = team.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "Unknown"

    @staticmethod
    def _find_team_row(rows: list[dict[str, Any]], team_id: int | None) -> dict[str, Any] | None:
        if not isinstance(team_id, int):
            return None
        for row in rows:
            team = row.get("team")
            if not isinstance(team, dict):
                continue
            if team.get("id") == team_id:
                return row
        return None

    @staticmethod
    def _pick_team(teams: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
        normalized = query.strip().casefold()
        if not normalized:
            return None

        exact: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for item in teams:
            team = item.get("team") if isinstance(item, dict) else None
            if not isinstance(team, dict):
                continue
            name = str(team.get("name", "")).strip()
            lowered = name.casefold()
            if lowered == normalized:
                exact.append(item)
            elif normalized in lowered:
                partial.append(item)

        if exact:
            return exact[0]
        if partial:
            return partial[0]
        return teams[0] if teams else None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FootballCog(bot))




