from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import re
from typing import Any, Final

import discord
from discord import app_commands
from discord.ext import commands

from services import football_formatter as football_fmt
from services import football_resolver
from services.football_live_match_service import normalize_match_statistics
from services.football_operation_service import FootballOperationService, FootballOutcome
from services.football_query_service import compile_football_operation
from utils.i18n import tr


LEAGUE_CHOICES: Final[list[app_commands.Choice[str]]] = [
    app_commands.Choice(name="ligamx", value="ligamx"),
    app_commands.Choice(name="premier", value="premier"),
    app_commands.Choice(name="laliga", value="laliga"),
    app_commands.Choice(name="concacaf", value="concacaf"),
    app_commands.Choice(name="worldcup", value="worldcup"),
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
        "worldcup": "worldcup",
        "fifaworldcup": "worldcup",
    }

    _LEAGUE_LABELS: Final[dict[str, tuple[str, str]]] = {
        "ligamx": ("Liga BBVA MX", "Liga BBVA MX"),
        "premier": ("Premier League", "Premier League"),
        "laliga": ("LaLiga", "LaLiga"),
        "concacaf": ("CONCACAF Champions Cup", "Copa de Campeones CONCACAF"),
        "worldcup": ("FIFA World Cup", "Copa Mundial FIFA"),
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        if not hasattr(bot, "football_player_alias_cache"):
            setattr(bot, "football_player_alias_cache", {})

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
        return football_resolver.normalize_key(value)

    def _normalize_league_key(self, raw: str | None) -> str | None:
        return football_resolver.normalize_league_key(raw) or (self._LEAGUE_ALIASES.get(self._slug(raw)) if raw else None)

    def _league_label(self, league_key: str, lang: str) -> str:
        return football_resolver.league_label(league_key, lang)

    def _football_player_alias_cache(self) -> dict[str, dict[str, Any]]:
        cache = getattr(self.bot, "football_player_alias_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self.bot, "football_player_alias_cache", cache)
        return cache

    def _football_player_canonicalizer(self):
        llm = getattr(self.bot, "llm_client", None)
        method = getattr(llm, "canonicalize_football_player_query", None)
        if method is None:
            return None

        async def _canonicalize(query: football_resolver.PlayerQuery) -> dict[str, Any] | None:
            clean_query = query.candidates[0] if query.candidates else query.raw
            return await method(
                original_query=query.raw,
                clean_query=clean_query,
                stat_focus=query.stat_focus,
            )

        return _canonicalize

    async def _resolve_league_context(
        self,
        ctx: commands.Context,
        *,
        client: Any,
        lang: str,
        raw_league: str,
    ) -> tuple[str, int, int] | None:
        league_key = self._normalize_league_key(raw_league)
        service = FootballOperationService(client)
        if league_key is None:
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                raw_league,
                {"data_focus": "league_lookup", "league_candidates": [raw_league]},
            )
            if operation.league_slots:
                resolved_league_id, resolved_season, row, outcome = await service.resolve_league_and_season(
                    operation,
                    league_id=None,
                    season=None,
                )
                if outcome == FootballOutcome.SELECTED and resolved_league_id is not None and resolved_season is not None:
                    league = row.get("league") if isinstance(row, dict) else {}
                    label = str(league.get("name", raw_league)).strip() if isinstance(league, dict) else raw_league.strip()
                    league_key = football_resolver.normalize_key(label) or "custom"
                    return league_key, resolved_league_id, resolved_season
        if league_key is None:
            await ctx.send(
                tr(
                    lang,
                    LEAGUE_INVALID_TEXT_EN,
                    LEAGUE_INVALID_TEXT_ES,
                )
            )
            return None
        league_id, season = await service.resolve_league_key(league_key)
        return league_key, league_id, season

    @staticmethod
    def _fixture_status(fixture: dict[str, Any]) -> str:
        return football_fmt.fixture_status(fixture)

    @staticmethod
    def _fixture_round(item: dict[str, Any]) -> str:
        return football_fmt.fixture_round(item)

    @staticmethod
    def _fixture_teams(item: dict[str, Any]) -> tuple[str, str]:
        return football_fmt.fixture_teams(item)

    @staticmethod
    def _fixture_team_logos(item: dict[str, Any]) -> tuple[str | None, str | None]:
        return football_fmt.fixture_team_logos(item)

    @staticmethod
    def _fixture_score(item: dict[str, Any]) -> str:
        return football_fmt.fixture_score(item)

    @staticmethod
    def _format_fixture_line(item: dict[str, Any]) -> tuple[str, str]:
        return football_fmt.format_fixture_line(item)

    @staticmethod
    def _match_datetime(item: dict[str, Any]) -> str:
        return football_fmt.fixture_datetime(item)

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
            operation = compile_football_operation(
                "FOOTBALL_FIXTURE_QUERY",
                "live fixtures",
                {"data_focus": "live_fixtures", "league_candidates": [raw_league]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=None,
                data_focus="live_fixtures",
            )
            fixtures = result.fixtures
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
            operation = compile_football_operation(
                "FOOTBALL_WATCH_TODAY",
                "today fixtures",
                {"data_focus": "today_fixtures", "league_candidates": [league]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="today_fixtures",
            )
            fixtures = result.fixtures
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
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                f"next fixtures {league}",
                {"data_focus": "next_fixtures", "league_candidates": [league]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="schedule_next_fixtures",
            )
            fixtures = result.fixtures[:count]
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
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                team_query,
                {"data_focus": "next_fixtures", "team_candidates": [team_query]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="next_fixtures",
            )
            if result.outcome == FootballOutcome.AMBIGUOUS:
                await ctx.send(tr(lang, "Multiple teams matched. Please be more specific.", "Varios equipos coinciden. Se mas especifico."))
                return
            team_row = result.team_context_row or {}
            team_info = team_row.get("team") if isinstance(team_row, dict) else {}
            team_name = str(team_info.get("name", team_query)) if isinstance(team_info, dict) else team_query
            fixtures = result.fixtures[:1]
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
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                query,
                {"data_focus": "last_fixtures", "team_candidates": [query]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="last_fixtures",
            )
            if result.outcome == FootballOutcome.AMBIGUOUS:
                await ctx.send(tr(lang, "Multiple teams matched. Please be more specific.", "Varios equipos coinciden. Se mas especifico."))
                return
            team_row = result.team_context_row or {}
            team_info = team_row.get("team") if isinstance(team_row, dict) else {}
            team_name = str(team_info.get("name", query)) if isinstance(team_info, dict) else query
            fixtures = result.fixtures[:1]
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
            operation = compile_football_operation(
                "FOOTBALL_TABLE",
                league,
                {"data_focus": "standings", "league_candidates": [league]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="standings",
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get standings: {exc}",
                    f"No se pudieron obtener las posiciones: {exc}",
                )
            )
            return

        table = result.standings_table
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
            operation = compile_football_operation(
                "FOOTBALL_TEAM_QUERY",
                query,
                {"data_focus": "team", "team_candidates": [query]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="team",
            )
            if result.outcome == FootballOutcome.AMBIGUOUS:
                await ctx.send(tr(lang, "Multiple teams matched. Please be more specific.", "Varios equipos coinciden. Se mas especifico."))
                return
            if result.team_context_row is None:
                await ctx.send(tr(lang, "Team not found in that league.", "No se encontro ese equipo en esa liga."))
                return
            team_row = result.team_context_row
            team = team_row.get("team") if isinstance(team_row, dict) else {}
            team_id = team.get("id") if isinstance(team, dict) else None
            team_name = str(team.get("name", query)) if isinstance(team, dict) else query
            standing_row = result.standing_row
            next_fixture = result.fixtures[0] if result.fixtures else None
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
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                league,
                {"data_focus": "scorers", "league_candidates": [league]},
            )
            result = await FootballOperationService(client).execute(
                operation,
                league_id=league_id,
                season=season,
                data_focus="top_scorers",
            )
            scorers = result.generic_rows
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

    @football.command(name="match", description="Show a fixture match center by fixture ID or team.")
    @app_commands.describe(query="Fixture ID or team name", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_match(self, ctx: commands.Context, query: str, league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            events = []
            stats = []
            if query.strip().isdigit():
                fixtures = await self._fixtures_for_query(client, query=query, league=league, lang=lang, ctx=ctx)
                if not fixtures:
                    await ctx.send(tr(lang, "No match found.", "No se encontro partido."))
                    return
                item = fixtures[0]
                fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
                fixture_id = fixture.get("id")
                events = await client.get_fixture_events(fixture_id=fixture_id) if isinstance(fixture_id, int) else []
                stats = await client.get_fixture_statistics(fixture_id=fixture_id) if isinstance(fixture_id, int) else []
            else:
                fixtures = await self._fixtures_for_query(client, query=query, league=league, lang=lang, ctx=ctx)
                if not fixtures:
                    await ctx.send(tr(lang, "No match found.", "No se encontro partido."))
                    return
                item = fixtures[0]
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get match data: {exc}", f"No se pudo obtener el partido: {exc}"))
            return
        embed = self._build_match_embed(
            lang=lang,
            league_label=str((item.get("league") or {}).get("name", self._league_label(self._normalize_league_key(league) or league, lang))),
            item=item,
            title_en="Match Center",
            title_es="Centro del Partido",
            color=discord.Color.teal(),
            index=1,
            total=1,
        )
        if events:
            embed.add_field(name=tr(lang, "Key events", "Eventos"), value=self._format_events(events), inline=False)
        if stats:
            embed.add_field(name=tr(lang, "Stats", "Estadisticas"), value=self._format_statistics(stats), inline=False)
        await ctx.send(embed=embed)

    @football.command(name="schedule", description="Show next or last fixtures for a team or league.")
    @app_commands.describe(target="Team name or 'league'", mode="next, last, or season", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_schedule(self, ctx: commands.Context, target: str = "league", mode: str = "next", league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        mode_key = mode.strip().casefold()
        try:
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
            if context is None:
                return
            league_key, league_id, season = context
            target_text = target.strip()
            is_team_target = bool(target_text and target_text.casefold() not in {"league", "liga", "all"})
            data_focus = "last_fixtures" if mode_key in {"last", "previous", "pasados", "ultimos"} else "next_fixtures"
            operation = compile_football_operation(
                "FOOTBALL_LOOKUP",
                target_text if is_team_target else f"{data_focus} {league}",
                {"data_focus": data_focus, "team_candidates": [target_text] if is_team_target else []},
            )
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus=f"schedule_{data_focus}")
            if result.outcome != FootballOutcome.SELECTED:
                await ctx.send(tr(lang, "No fixtures found.", "No se encontraron partidos."))
                return
            fixtures = result.fixtures
            if data_focus == "last_fixtures":
                title_en, title_es = "Last Fixtures", "Ultimos Partidos"
            else:
                title_en, title_es = "Upcoming Fixtures", "Proximos Partidos"
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get schedule: {exc}", f"No se pudo obtener el calendario: {exc}"))
            return
        await self._send_fixture_embeds(ctx, fixtures, lang=lang, league_label=self._league_label(league_key, lang), title_en=title_en, title_es=title_es)

    @football.command(name="player", description="Show a player profile and season stats.")
    @app_commands.describe(player="Player name", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_player(self, ctx: commands.Context, player: str, league: str = "") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            league_id = season = None
            explicit_context = bool(str(league or "").strip())
            if explicit_context:
                context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
                if context is None:
                    return
                _league_key, league_id, season = context
            operation = compile_football_operation(
                "FOOTBALL_PLAYER_QUERY",
                player,
                {"data_focus": "player", "player_candidates": [player]},
            )
            if not operation.player_slots:
                await ctx.send(tr(lang, "I need a clearer player name.", "Necesito un nombre de jugador mas claro."))
                return
            result = await FootballOperationService(
                client,
                player_canonicalizer=self._football_player_canonicalizer(),
                player_alias_cache=self._football_player_alias_cache(),
            ).execute(operation, league_id=league_id, season=season, data_focus="player")
            self._log_football_diagnostic(
                command="player",
                resolver_input=player,
                normalized_query=" | ".join(operation.player_candidates),
                explicit_context=explicit_context,
                endpoint=",".join(result.endpoints),
                params={"league": league_id, "season": season},
                response_count=len(result.generic_rows) or (1 if result.player_context_row else 0),
                top_candidates=self._player_candidate_names(list(result.generic_rows)),
                final_decision="ambiguous" if result.outcome == FootballOutcome.AMBIGUOUS else ("selected" if result.player_context_row else "not_found"),
                fallback_reason=None if result.player_context_row else "no_player_match",
            )
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get player data: {exc}", f"No se pudo obtener el jugador: {exc}"))
            return
        if result.outcome == FootballOutcome.AMBIGUOUS:
            await ctx.send(self._format_player_disambiguation(list(result.generic_rows), lang))
            return
        if result.player_context_row is None:
            await ctx.send(tr(lang, "Player not found.", "No se encontro el jugador."))
            return
        await ctx.send(embed=self._build_player_embed(result.player_context_row, lang=lang))

    @football.command(name="lineup", description="Show confirmed lineups for a fixture ID.")
    async def football_lineup(self, ctx: commands.Context, fixture_id: int) -> None:
        await self._send_fixture_detail(ctx, fixture_id=fixture_id, kind="lineup")

    @football.command(name="stats", description="Show fixture statistics for a fixture ID.")
    async def football_stats(self, ctx: commands.Context, fixture_id: int) -> None:
        await self._send_fixture_detail(ctx, fixture_id=fixture_id, kind="stats")

    @football.command(name="injuries", description="Show injuries/unavailable players for a team.")
    @app_commands.describe(team="Team name", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_injuries(self, ctx: commands.Context, team: str, league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
            if context is None:
                return
            _league_key, league_id, season = context
            operation = compile_football_operation("FOOTBALL_LOOKUP", team, {"data_focus": "injuries", "team_candidates": [team]})
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus="injuries")
            if result.outcome != FootballOutcome.SELECTED:
                return
            rows = result.generic_rows
            team_row = result.team_context_row or {}
            team_info = team_row.get("team") if isinstance(team_row, dict) else {}
            team_name = str(team_info.get("name", team)) if isinstance(team_info, dict) else team
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get injuries: {exc}", f"No se pudieron obtener lesionados: {exc}"))
            return
        await ctx.send(embed=self._build_simple_rows_embed(title=f"Injuries - {team_name}", rows=rows, lang=lang))

    @football.command(name="transfers", description="Show recent transfers for a team.")
    @app_commands.describe(team="Team name", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_transfers(self, ctx: commands.Context, team: str, league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
            if context is None:
                return
            _league_key, league_id, season = context
            operation = compile_football_operation("FOOTBALL_LOOKUP", team, {"data_focus": "transfers", "team_candidates": [team]})
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus="transfers")
            if result.outcome != FootballOutcome.SELECTED:
                return
            rows = result.generic_rows
            team_row = result.team_context_row or {}
            team_info = team_row.get("team") if isinstance(team_row, dict) else {}
            team_name = str(team_info.get("name", team)) if isinstance(team_info, dict) else team
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get transfers: {exc}", f"No se pudieron obtener transferencias: {exc}"))
            return
        await ctx.send(embed=self._build_simple_rows_embed(title=f"Transfers - {team_name}", rows=rows, lang=lang))

    @football.command(name="h2h", description="Show head-to-head between two teams.")
    @app_commands.describe(team_a="First team", team_b="Second team", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_h2h(self, ctx: commands.Context, team_a: str, team_b: str, league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
            if context is None:
                return
            _league_key, league_id, season = context
            operation = compile_football_operation("FOOTBALL_COMPARISON", f"{team_a} vs {team_b}", {"data_focus": "h2h", "team_candidates": [team_a, team_b]})
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus="h2h")
            if result.outcome != FootballOutcome.SELECTED:
                return
            fixtures = result.generic_rows
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get H2H: {exc}", f"No se pudo obtener H2H: {exc}"))
            return
        await self._send_fixture_embeds(ctx, fixtures, lang=lang, league_label=f"{team_a} vs {team_b}", title_en="Head to Head", title_es="Historial")

    @football.command(name="top", description="Show top scorers, assists, or cards.")
    @app_commands.describe(category="scorers, assists, yellowcards, redcards", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def football_top(self, ctx: commands.Context, category: str = "scorers", league: str = "ligamx") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
            if context is None:
                return
            league_key, league_id, season = context
            key = category.strip().casefold()
            data_focus = "top_scorers"
            if key in {"assists", "asistencias"}:
                data_focus = "top_assists"
                label = "Top Assists"
            elif key in {"yellowcards", "yellow", "amarillas"}:
                data_focus = "top_yellow_cards"
                label = "Top Yellow Cards"
            elif key in {"redcards", "red", "rojas"}:
                data_focus = "top_red_cards"
                label = "Top Red Cards"
            else:
                label = "Top Scorers"
            operation = compile_football_operation("FOOTBALL_LOOKUP", f"{category} {league}", {"data_focus": data_focus, "league_candidates": [league]})
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus=data_focus)
            rows = result.generic_rows
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get leaderboard: {exc}", f"No se pudo obtener la tabla: {exc}"))
            return
        await ctx.send(embed=self._build_player_leaderboard_embed(rows, title=f"{self._league_label(league_key, lang)} - {label}", lang=lang))

    @football.command(name="preview", description="Show data-only preview for a fixture ID.")
    async def football_preview(self, ctx: commands.Context, fixture_id: int) -> None:
        await self._send_fixture_detail(ctx, fixture_id=fixture_id, kind="preview")

    @football.command(name="summary", description="Show data-only summary for a fixture ID.")
    async def football_summary(self, ctx: commands.Context, fixture_id: int) -> None:
        await self._send_fixture_detail(ctx, fixture_id=fixture_id, kind="summary")

    async def _fixtures_for_query(
        self,
        client: Any,
        *,
        query: str,
        league: str,
        lang: str,
        ctx: commands.Context,
    ) -> list[dict[str, Any]]:
        stripped = query.strip()
        if stripped.isdigit():
            return await client.get_fixture_by_id(fixture_id=int(stripped))
        context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league)
        if context is None:
            return []
        _league_key, league_id, season = context
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            stripped,
            {"data_focus": "next_fixtures", "team_candidates": [stripped]},
        )
        result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus="single_next_fixtures")
        if result.outcome != FootballOutcome.SELECTED:
            return []
        return result.fixtures[:1]

    async def _resolve_team(
        self,
        ctx: commands.Context,
        *,
        client: Any,
        lang: str,
        league_id: int,
        season: int,
        query: str,
    ) -> tuple[int, str] | None:
        operation = compile_football_operation(
            "FOOTBALL_LOOKUP",
            query,
            {"data_focus": "team", "team_candidates": [query]},
        )
        if not operation.team_slots:
            await ctx.send(tr(lang, "I need a clearer team name.", "Necesito un nombre de equipo mas claro."))
            return None
        normalized = football_resolver.canonical_team_query(operation.team_slots[0].name)
        service = FootballOperationService(client)
        team_row, outcome = await service.resolve_team(operation, league_id=league_id, season=season)
        self._log_football_diagnostic(
            command="team_resolver",
            resolver_input=query,
            normalized_query=normalized,
            explicit_context=True,
            endpoint="/teams",
            params={"league": league_id, "season": season},
            response_count=1 if team_row else 0,
            top_candidates=self._team_candidate_names([team_row] if team_row else []),
            final_decision="ambiguous" if outcome == FootballOutcome.AMBIGUOUS else ("selected" if team_row else "not_found"),
            fallback_reason=None if team_row else "no_team_match",
        )
        if outcome == FootballOutcome.AMBIGUOUS:
            await ctx.send(tr(lang, "Multiple teams matched. Please be more specific.", "Varios equipos coinciden. Se mas especifico."))
            return None
        if team_row is None:
            await ctx.send(tr(lang, "Team not found in that league.", "No se encontro ese equipo en esa liga."))
            return None
        team = team_row.get("team") if isinstance(team_row, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        team_name = str(team.get("name", query)) if isinstance(team, dict) else query
        if not isinstance(team_id, int):
            await ctx.send(tr(lang, "Could not resolve a valid team ID.", "No se pudo resolver un ID valido."))
            return None
        return team_id, team_name

    @staticmethod
    def _team_candidate_names(rows: list[dict[str, Any]]) -> list[str]:
        names = []
        for item in rows[:5]:
            team = item.get("team") if isinstance(item, dict) else {}
            if isinstance(team, dict):
                value = team.get("name")
                team_id = team.get("id")
                names.append(f"{value}#{team_id}"[:120])
        return names

    @staticmethod
    def _player_candidate_names(rows: list[dict[str, Any]]) -> list[str]:
        names = []
        for item in rows[:5]:
            player = item.get("player") if isinstance(item, dict) else {}
            if isinstance(player, dict):
                value = player.get("name")
                player_id = player.get("id")
                names.append(f"{value}#{player_id}"[:120])
        return names

    @staticmethod
    def _format_player_disambiguation(rows: list[dict[str, Any]], lang: str) -> str:
        names = []
        for item in rows[:5]:
            player = item.get("player") if isinstance(item, dict) else {}
            stats = item.get("statistics") if isinstance(item, dict) else []
            first_stats = stats[0] if isinstance(stats, list) and stats else {}
            team = first_stats.get("team") if isinstance(first_stats, dict) else {}
            name = player.get("name") if isinstance(player, dict) else "Unknown"
            team_name = team.get("name") if isinstance(team, dict) else "N/A"
            names.append(f"{name} ({team_name})")
        joined = ", ".join(names) or "Unknown"
        return tr(lang, f"Multiple players matched: {joined}", f"Varios jugadores coinciden: {joined}")

    @staticmethod
    def _football_today_iso(client: Any) -> str:
        method = getattr(client, "today_iso", None)
        if callable(method):
            try:
                return str(method())
            except Exception:
                logging.warning("API-Football timezone date helper failed; falling back to local date")
        return date.today().isoformat()

    @staticmethod
    def _log_football_diagnostic(
        *,
        command: str,
        resolver_input: str,
        normalized_query: str,
        explicit_context: bool,
        endpoint: str,
        params: dict[str, Any],
        response_count: int,
        top_candidates: list[str] | None = None,
        final_decision: str,
        fallback_reason: str | None = None,
    ) -> None:
        logging.info(
            "Football command=%s resolver_input=%s normalized_query=%s explicit_context=%s endpoint=%s params=%s response_count=%s top_candidates=%s final_decision=%s fallback_reason=%s",
            command,
            str(resolver_input)[:120],
            str(normalized_query)[:160],
            explicit_context,
            endpoint,
            {key: str(value)[:80] for key, value in params.items() if value is not None},
            response_count,
            top_candidates or [],
            final_decision,
            fallback_reason,
        )

    async def _send_fixture_embeds(
        self,
        ctx: commands.Context,
        fixtures: list[dict[str, Any]],
        *,
        lang: str,
        league_label: str,
        title_en: str,
        title_es: str,
    ) -> None:
        if not fixtures:
            await ctx.send(tr(lang, "No fixtures found.", "No se encontraron partidos."))
            return
        shown = min(len(fixtures), 10)
        embeds = [
            self._build_match_embed(
                lang=lang,
                league_label=league_label,
                item=item,
                title_en=title_en,
                title_es=title_es,
                color=discord.Color.blurple(),
                index=index,
                total=shown,
            )
            for index, item in enumerate(fixtures[:shown], start=1)
        ]
        await ctx.send(embeds=embeds)

    async def _send_fixture_detail(self, ctx: commands.Context, *, fixture_id: int, kind: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            fixtures = await client.get_fixture_by_id(fixture_id=fixture_id)
            item = fixtures[0] if fixtures else {}
            events = await client.get_fixture_events(fixture_id=fixture_id) if kind in {"summary", "preview"} else []
            stats = await client.get_fixture_statistics(fixture_id=fixture_id) if kind in {"stats", "summary", "preview"} else []
            lineups = await client.get_fixture_lineups(fixture_id=fixture_id) if kind in {"lineup", "preview"} else []
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get fixture details: {exc}", f"No se pudo obtener detalles: {exc}"))
            return
        if not item:
            await ctx.send(tr(lang, "Fixture not found.", "No se encontro el partido."))
            return
        league = item.get("league") if isinstance(item.get("league"), dict) else {}
        embed = self._build_match_embed(
            lang=lang,
            league_label=str(league.get("name", "Football")),
            item=item,
            title_en=kind.title(),
            title_es=kind.title(),
            color=discord.Color.teal(),
            index=1,
            total=1,
        )
        if lineups:
            embed.add_field(name=tr(lang, "Lineups", "Alineaciones"), value=self._format_lineups(lineups), inline=False)
        if stats:
            embed.add_field(name=tr(lang, "Stats", "Estadisticas"), value=self._format_statistics(stats), inline=False)
        if events:
            embed.add_field(name=tr(lang, "Events", "Eventos"), value=self._format_events(events), inline=False)
        await ctx.send(embed=embed)

    def _build_player_embed(self, row: dict[str, Any], *, lang: str) -> discord.Embed:
        player = row.get("player") if isinstance(row, dict) else {}
        stats_list = row.get("statistics") if isinstance(row, dict) else []
        stats = stats_list[0] if isinstance(stats_list, list) and stats_list else {}
        team = stats.get("team") if isinstance(stats, dict) else {}
        games = stats.get("games") if isinstance(stats, dict) else {}
        goals = stats.get("goals") if isinstance(stats, dict) else {}
        name = str(player.get("name", "Unknown")) if isinstance(player, dict) else "Unknown"
        embed = discord.Embed(title=tr(lang, f"Player: {name}", f"Jugador: {name}"), color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name=tr(lang, "Team", "Equipo"), value=str(team.get("name", "N/A")) if isinstance(team, dict) else "N/A", inline=True)
        embed.add_field(name=tr(lang, "Position", "Posicion"), value=str(games.get("position", "N/A")) if isinstance(games, dict) else "N/A", inline=True)
        embed.add_field(name=tr(lang, "Appearances", "Partidos"), value=str(games.get("appearences", 0)) if isinstance(games, dict) else "0", inline=True)
        embed.add_field(name=tr(lang, "Goals", "Goles"), value=str(goals.get("total", 0)) if isinstance(goals, dict) else "0", inline=True)
        embed.add_field(name=tr(lang, "Assists", "Asistencias"), value=str(goals.get("assists", 0)) if isinstance(goals, dict) else "0", inline=True)
        return embed

    def _build_player_leaderboard_embed(self, rows: list[dict[str, Any]], *, title: str, lang: str) -> discord.Embed:
        lines = []
        for index, row in enumerate(rows[:10], start=1):
            player = row.get("player") if isinstance(row, dict) else {}
            stats_list = row.get("statistics") if isinstance(row, dict) else []
            stats = stats_list[0] if isinstance(stats_list, list) and stats_list else {}
            team = stats.get("team") if isinstance(stats, dict) else {}
            goals = stats.get("goals") if isinstance(stats, dict) else {}
            cards = stats.get("cards") if isinstance(stats, dict) else {}
            total = goals.get("total", goals.get("assists", cards.get("yellow", cards.get("red", 0)))) if isinstance(goals, dict) and isinstance(cards, dict) else 0
            lines.append(f"`{index:>2}.` {player.get('name', 'Unknown') if isinstance(player, dict) else 'Unknown'} ({team.get('name', 'N/A') if isinstance(team, dict) else 'N/A'}) - {total}")
        return discord.Embed(title=title, description=("\n".join(lines) or tr(lang, "No data.", "Sin datos."))[:4000], color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))

    def _build_simple_rows_embed(self, *, title: str, rows: list[dict[str, Any]], lang: str) -> discord.Embed:
        lines = []
        for item in rows[:10]:
            player = item.get("player") if isinstance(item, dict) else {}
            team = item.get("team") if isinstance(item, dict) else {}
            reason = item.get("reason") or item.get("type") or item.get("date") or ""
            name = player.get("name") if isinstance(player, dict) else None
            team_name = team.get("name") if isinstance(team, dict) else None
            lines.append(f"{name or team_name or 'Unknown'} - {reason or 'N/A'}")
        return discord.Embed(title=title, description=("\n".join(lines) or tr(lang, "No data.", "Sin datos."))[:4000], color=discord.Color.dark_teal(), timestamp=datetime.now(timezone.utc))

    @staticmethod
    def _format_events(events: list[dict[str, Any]]) -> str:
        lines = []
        for item in events[:10]:
            time_data = item.get("time") if isinstance(item.get("time"), dict) else {}
            player = item.get("player") if isinstance(item.get("player"), dict) else {}
            team = item.get("team") if isinstance(item.get("team"), dict) else {}
            minute = time_data.get("elapsed", "?")
            lines.append(f"{minute}' {team.get('name', '')} - {player.get('name', '')} ({item.get('type', '')} {item.get('detail', '')})")
        return ("\n".join(lines) or "N/A")[:1024]

    @staticmethod
    def _format_statistics(stats: list[dict[str, Any]]) -> str:
        lines = []
        for team_stats in list(normalize_match_statistics(stats).values())[:2]:
            compact = []
            team_name = "Team"
            for stat in list(team_stats.values())[:6]:
                team_name = stat.team_name or team_name
                compact.append(f"{stat.original_label}: {stat.display_value}")
            lines.append(f"**{team_name}**\n" + "\n".join(compact))
        return ("\n\n".join(lines) or "N/A")[:1024]

    @staticmethod
    def _format_lineups(lineups: list[dict[str, Any]]) -> str:
        lines = []
        for item in lineups[:2]:
            team = item.get("team") if isinstance(item.get("team"), dict) else {}
            coach = item.get("coach") if isinstance(item.get("coach"), dict) else {}
            lines.append(f"**{team.get('name', 'Team')}** - {item.get('formation', 'N/A')} | {coach.get('name', 'N/A')}")
        return ("\n".join(lines) or "N/A")[:1024]
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
            rows: list[dict[str, Any]] = []
            for group in standings:
                if isinstance(group, list):
                    rows.extend(row for row in group if isinstance(row, dict))
            return rows
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




