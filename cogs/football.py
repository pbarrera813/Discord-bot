from __future__ import annotations

from dataclasses import replace
import asyncio
import logging
from typing import Any, Final

import discord
from discord import app_commands
from discord.ext import commands

from services import football_formatter as football_fmt
from services import football_resolver
from services import football_cards
from services.api_football import ApiFootballClient, FootballApiError
from services.football_api_request_compiler import _make_league_slot, build_league_search_request
from services.football_api_request_compiler import build_prediction_request
from services.football_live_match_service import FootballLiveMatchService
from services.football_operation_service import FootballOperationService, FootballOutcome, FootballRetrievalResult
from services.football_query_service import compile_football_operation
from services.server_memory import ServerMemoryInput, ServerMemoryService
from utils.i18n import tr


LEAGUE_CHOICES: Final[list[app_commands.Choice[str]]] = [
    app_commands.Choice(name="ligamx", value="ligamx"),
    app_commands.Choice(name="premier", value="premier"),
    app_commands.Choice(name="laliga", value="laliga"),
    app_commands.Choice(name="concacaf", value="concacaf"),
    app_commands.Choice(name="worldcup", value="worldcup"),
]
LEAGUE_CODES: Final[tuple[str, ...]] = tuple(choice.value for choice in LEAGUE_CHOICES)
LEAGUE_HELP_TEXT: Final[str] = f"Search a league or use: {', '.join(LEAGUE_CODES)}"
LEAGUE_INVALID_TEXT_EN: Final[str] = f"Could not identify the league. Choose a suggestion or use: {', '.join(LEAGUE_CODES)}."
# The member's saved team: a FOOTBALL_PREFERENCE memory whose metadata holds the provider team id.
MY_TEAM_KEY: Final[str] = "my_team"
LEAGUE_INVALID_TEXT_ES: Final[str] = f"No se pudo identificar la liga. Elige una sugerencia o usa: {', '.join(LEAGUE_CODES)}."


class FootballCardView(discord.ui.View):
    def __init__(self, *, author_id: int, lang: str, pages: dict, labels: dict[str, str],
                 section: str, media_cache: dict[str, bytes], can_attach: bool,
                 refresh, load_section=None, fixtures: list[dict] | None = None, select_fixture=None, refresh_list=None,
                 load_more=None) -> None:
        super().__init__(timeout=180)
        self.author_id, self.lang = author_id, lang
        self.pages, self.labels, self.section = pages, labels, section
        self.media_cache, self.can_attach = media_cache, can_attach
        self.refresh, self.load_section = refresh, load_section
        self.load_more = load_more
        self.index = 0
        self.message = None
        self.lock = asyncio.Lock()
        self.expired = False
        self.pending_fixture = bool(fixtures)
        self.fixtures, self.fixture_page = fixtures, 0
        self.refresh_list = refresh_list
        self.select_fixture = select_fixture
        self.fixture_menu = None
        if fixtures:
            controls = list(self.children)
            for child in controls:
                self.remove_item(child)
            for child in controls:
                child.row += 1
                self.add_item(child)
            self.fixture_menu = discord.ui.Select(placeholder=tr(lang, "Choose a match", "Elige un partido"), row=0)
            self.fixture_menu.callback = self._choose_fixture
            self.add_item(self.fixture_menu)
        self.section_menu.options = [discord.SelectOption(label=label, value=key) for key, label in labels.items()]
        self.section_menu.placeholder = tr(lang, "Section", "Seccion")
        self.previous.label = tr(lang, "Previous", "Anterior")
        self.next_page.label = tr(lang, "Next", "Siguiente")
        self.refresh_button.label = tr(lang, "Refresh", "Actualizar")
        self._controls()

    @property
    def current(self) -> dict:
        return self.pages[self.section][self.index]

    def _controls(self) -> None:
        list_mode = "fixtures" in self.pages
        blocked = self.pending_fixture and not list_mode
        self.section_menu.options = [discord.SelectOption(label=label, value=key) for key, label in self.labels.items()
                                     if not (list_mode and self.pending_fixture) or key in {"fixtures", "comparison"}]
        self.section_menu.disabled = blocked
        self.refresh_button.disabled = blocked
        self.previous.disabled = blocked or self.index == 0
        self.next_page.disabled = blocked or (self.index + 1 >= len(self.pages.get(self.section) or [])
                                              and not self.current.get("more"))
        if self.fixture_menu is not None:
            shown = self.fixtures
            if list_mode:
                shown = self.pages["fixtures"][self.fixture_page].get("fixtures") or []
            selected = next((option.value for option in self.fixture_menu.options if option.default), None)
            self.fixture_menu.options = []
            for item in shown:
                home, away = football_fmt.fixture_teams(item)
                label = f"{home} {football_fmt.fixture_score(item)} {away}"
                details = f"{(item.get('league') or {}).get('name') or 'Football'} / {football_fmt.fixture_datetime(item)} / {football_fmt.fixture_status(item.get('fixture') or {})}"
                value = str(item["fixture"]["id"])
                self.fixture_menu.add_option(label=label if len(label) <= 100 else label[:99] + "…",
                                             description=details if len(details) <= 100 else details[:99] + "…",
                                             value=value, default=value == selected)
            self.fixture_menu.disabled = not shown
            if not shown:
                self.fixture_menu.add_option(label=tr(self.lang, "No matches", "Sin partidos"), value="none")
        for option in self.section_menu.options:
            option.default = option.value == self.section

    def text_payload(self) -> dict:
        # Used when the card image cannot be sent; a Discord message holds 2000 characters.
        return {"content": football_cards.page_text(self.current)[:1900], "attachments": [], "view": self,
                "allowed_mentions": discord.AllowedMentions.none()}

    async def payload(self) -> dict:
        if not self.can_attach or self.current.get("text"):
            return self.text_payload()
        try:
            logos = await football_cards.load_logos(self.current.get("media", []), self.media_cache)
            image = await asyncio.to_thread(football_cards.render_profile, self.current, lang=self.lang,
                                           logos=logos, number=self.index + 1, total=len(self.pages[self.section]))
            return {"content": None, "attachments": [discord.File(image, filename="football.png",
                    description=football_cards.page_text(self.current)[:1024])], "view": self,
                    "allowed_mentions": discord.AllowedMentions.none()}
        except Exception:
            logging.exception("Football card rendering failed; using text")
            return self.text_payload()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id or self.expired:
            await interaction.response.send_message(
                tr(self.lang, "These controls have expired or belong to another user.",
                   "Estos controles han caducado o pertenecen a otro usuario."), ephemeral=True)
            return False
        return True

    async def _update(self, interaction: discord.Interaction, action: str) -> None:
        if not await self.interaction_check(interaction):
            return
        selection = (self.fixture_menu.values[0] if action == "fixture" else
                     self.section_menu.values[0] if action == "section" else None)
        await interaction.response.defer()
        async with self.lock:
            if self.expired:
                return
            try:
                if action == "fixture":
                    choice = selection
                    if choice not in {option.value for option in self.fixture_menu.options}:
                        raise ValueError("Unknown fixture selection")
                    self.pages = await self.select_fixture(int(choice))
                    self.pending_fixture = False
                    self.section, self.index = "overview", 0
                    for option in self.fixture_menu.options:
                        option.default = option.value == choice
                elif self.pending_fixture and "fixtures" not in self.pages:
                    return
                elif action == "refresh":
                    if self.section in {"fixtures", "comparison"}:
                        self.pages = await self.refresh_list()
                        self.pending_fixture = True
                        self.fixture_page = 0
                        for option in self.fixture_menu.options:
                            option.default = False
                    else:
                        self.pages = await self.refresh()
                    self.index = 0
                elif action == "section":
                    if selection not in {option.value for option in self.section_menu.options}:
                        raise ValueError("Unknown section selection")
                    self.section = selection
                    self.index = self.fixture_page if self.section == "fixtures" else 0
                else:
                    if action == "next" and self.index + 1 == len(self.pages[self.section]) and self.current.get("more"):
                        extra = await self.load_more(self.section, self.current["more"])
                        self.current.pop("more")
                        self.pages[self.section].extend(extra)
                    self.index = max(0, min(self.index + (1 if action == "next" else -1),
                                            len(self.pages[self.section]) - 1))
                    if self.section == "fixtures":
                        self.fixture_page = self.index
                if self.pages[self.section] is None:
                    self.pages[self.section] = await self.load_section(self.section)
            except Exception:
                logging.exception("Football card section loading failed")
                retry = tr(self.lang, "Could not open that match. Select a match again.",
                           "No se pudo abrir ese partido. Selecciona un partido otra vez.") if self.pending_fixture and self.section != "fixtures" else tr(
                    self.lang, "Could not load this section. Use Refresh to try again.",
                    "No se pudo cargar esta seccion. Usa Actualizar para reintentar.")
                self.pages[self.section] = [{"title": self.labels[self.section], "text": retry}]
                self.index = 0
                if self.section == "fixtures":
                    self.fixture_page = 0
            self._controls()
            payload = await self.payload()
            try:
                await interaction.edit_original_response(**payload)
            except discord.HTTPException:
                if not payload["attachments"]:
                    raise
                await interaction.edit_original_response(**self.text_payload())

    async def _choose_fixture(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, "fixture")

    @discord.ui.select(placeholder="Section", row=0)
    async def section_menu(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await self._update(interaction, "section")

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update(interaction, "previous")

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update(interaction, "next")

    @discord.ui.button(label="Refresh", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update(interaction, "refresh")

    async def on_timeout(self) -> None:
        self.expired = True
        async with self.lock:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass


class FootballChoiceView(discord.ui.View):
    """The author picks one of several provider records; labels carry names and facts, never provider ids."""

    def __init__(self, *, author_id: int, lang: str, labels: list[str], choose) -> None:
        super().__init__(timeout=180)
        self.author_id, self.lang, self.choose, self.message = author_id, lang, choose, None
        self.menu = discord.ui.Select(placeholder=tr(lang, "Choose one", "Elige uno"),
                                      options=[discord.SelectOption(label=label, value=str(index))
                                               for index, label in enumerate(labels)])
        self.menu.callback = self._picked
        self.add_item(self.menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                tr(self.lang, "These controls belong to another user.", "Estos controles pertenecen a otro usuario."),
                ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()
        await self.choose(int(self.menu.values[0]))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


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
        self._table_logo_cache: dict[str, bytes] = {}
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
        season: int | None = None,
        identity_selector=None,
    ) -> tuple[str, int, int] | None:
        if raw_league.strip().isdigit():
            league_id = int(raw_league.strip())
            rows = await client.get_league_by_id(league_id=league_id)
            rows = [row for row in rows if (row.get("league") or {}).get("id") == league_id]
            year = season if season is not None else ApiFootballClient._extract_current_season(rows)
            if rows and year is not None:
                return str(rows[0]["league"].get("name") or raw_league), league_id, year
            await ctx.send(tr(lang, "The provider has no league/season data for that selection.",
                              "El proveedor no tiene datos de liga/temporada para esa seleccion."))
            return None
        league_key = self._normalize_league_key(raw_league)
        service = FootballOperationService(client, identity_selector=identity_selector)
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
                    season=season,
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
        league_id = await client.resolve_league_id(league_key)
        resolved_season = season if season is not None else await client.get_current_season(league_id)
        return league_key, league_id, resolved_season

    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                return

    @commands.hybrid_group(name="football", description="Football matches, teams, tables and players.",
                           invoke_without_command=True)
    async def football(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await ctx.send(tr(lang, "Use /football match, fixtures, team, table, player, h2h, top or myteam.",
                          "Usa /football match, fixtures, team, table, player, h2h, top o myteam."))

    @football.command(name="fixtures", description="A league's live, today's, upcoming or finished matches.")
    @app_commands.describe(league=LEAGUE_HELP_TEXT, when="Auto shows live matches, then today's, then upcoming ones")
    @app_commands.choices(when=[app_commands.Choice(name=label, value=value) for label, value in
                                [("Auto", "auto"), ("Live", "live"), ("Today", "today"), ("Next", "next"), ("Last", "last")]])
    async def football_fixtures(self, ctx: commands.Context, league: str, when: str = "auto") -> None:
        if when not in {"auto", "live", "today", "next", "last"}:
            lang = await self._lang(ctx.guild)
            await ctx.send(tr(lang, "Choose Auto, Live, Today, Next or Last.", "Elige Auto, Live, Today, Next o Last."))
            return
        await self._run_fixture_list(ctx, league=league, kind=when)

    async def _run_fixture_list(self, ctx: commands.Context, *, league: str, kind: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        kinds = ("live", "today", "next") if kind == "auto" else (kind,)
        try:
            selector = football_resolver.make_identity_selector(
                getattr(self.bot, "llm_client", None), request_text=league, history=[])
            context = await self._resolve_league_context(ctx, client=client, lang=lang, raw_league=league,
                                                          identity_selector=selector)
            if context is None:
                return
            league_key, league_id, season = context

            async def fetch_kind(item: str) -> list[dict]:
                focus = {"live": "live_fixtures", "today": "today_fixtures", "next": "next_fixtures", "last": "last_fixtures"}[item]
                operation = compile_football_operation("FOOTBALL_WATCH_TODAY" if item in {"live", "today"} else "FOOTBALL_LOOKUP",
                                                       "", {"data_focus": focus, "team_candidates": []})
                operation = replace(operation, fixture_count=10 if item in {"next", "last"} else None)
                result = await FootballOperationService(client, identity_selector=selector).execute(
                    operation, league_id=league_id, season=season, data_focus=focus)
                if result.outcome not in {FootballOutcome.SELECTED, FootballOutcome.NO_DATA_FOR_SCOPE}:
                    raise ValueError(self._football_result_message(result, lang))
                return list({item["fixture"]["id"]: item for item in result.fixtures
                             if type((item.get("fixture") or {}).get("id")) is int and item["fixture"]["id"] > 0}.values())

            fixtures: list[dict] = []
            for shown in kinds:
                fixtures = await fetch_kind(shown)
                if fixtures:
                    break

            async def fetch() -> list[dict]:
                return await fetch_kind(shown)
        except Exception as exc:
            await ctx.send(tr(lang, f"Could not load fixtures: {exc}", f"No se pudieron cargar los partidos: {exc}"))
            return
        label = self._league_label(league_key, lang)
        if not fixtures:
            description = tr(lang, *{"live": ("live", "en vivo"), "today": ("today's", "de hoy"),
                                     "next": ("upcoming", "proximos"), "last": ("finished", "finalizados")}[shown])
            await ctx.send(tr(lang, f"No {description} fixtures are listed for {label}.",
                              f"No hay partidos {description} registrados para {label}."))
            return
        heading = tr(lang, *{"live": ("Live matches", "Partidos en vivo"), "today": ("Today's matches", "Partidos de hoy"),
                              "next": ("Upcoming matches", "Proximos partidos"), "last": ("Finished matches", "Partidos finalizados")}[shown])
        scope = f"{label} / {season}" if shown in {"next", "last"} else label
        await self._send_match_card(ctx, client=client, lang=lang, fixture_id=fixtures[0]["fixture"]["id"],
                                    section="fixtures", fixture=fixtures[0], fixtures=fixtures,
                                    list_title=f"{scope} / {heading}", reload_fixtures=fetch)

    @football.command(
        name="table",
        description="Get standings for a selected league.",
    )
    @app_commands.describe(league=LEAGUE_HELP_TEXT)
    async def football_table(self, ctx: commands.Context, league: str) -> None:
        await self._send_league_card(ctx, league=league, focus="standings")

    async def _saved_teams(self, ctx: commands.Context) -> list[dict[str, Any]]:
        if ctx.guild is None:
            return []
        rows = await ServerMemoryService(self.bot.db).list_memories(
            ctx.guild.id, memory_type="FOOTBALL_PREFERENCE", subject_user_id=ctx.author.id)
        return [row for row in rows if row.get("key") == MY_TEAM_KEY]

    async def _resolve_team(self, ctx: commands.Context, *, client: Any, lang: str, team: str) -> dict[str, Any] | None:
        """The provider row of the team the author named, or of their saved team when they named none."""
        name, saved_id = team.strip(), None
        if not name:
            saved = next((row for row in await self._saved_teams(ctx)
                          if type((row.get("metadata") or {}).get("team_id")) is int), None)
            if saved is None:
                await ctx.send(tr(lang, "Name a team, or save yours with /football myteam <team>.",
                                  "Escribe un equipo o guarda el tuyo con /football myteam <equipo>."))
                return None
            name, saved_id = saved["value"], saved["metadata"]["team_id"]
        selector = football_resolver.make_identity_selector(getattr(self.bot, "llm_client", None), request_text=name, history=[])
        operation = compile_football_operation("FOOTBALL_TEAM_QUERY", name, {"data_focus": "team", "team_candidates": [name]})
        rows, outcome = await FootballOperationService(client, identity_selector=selector).resolve_team_rows(
            operation, league_id=None, season=None, max_teams=1, allow_global=True)
        # A saved team is looked up by its exact provider name and must come back as the same team.
        if outcome != FootballOutcome.SELECTED or not rows or (saved_id is not None and rows[0]["team"].get("id") != saved_id):
            await ctx.send(tr(lang, "The team could not be identified clearly. Try its full name.",
                              "No se pudo identificar el equipo con claridad. Prueba su nombre completo."))
            return None
        return rows[0]

    @football.command(name="myteam", description="Save your team, or show the one you saved.")
    @app_commands.describe(team="Team name; leave empty to see your saved team")
    async def football_myteam(self, ctx: commands.Context, team: str = "") -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Your team is saved per server; use this in a server.",
                              "Tu equipo se guarda por servidor; usa esto en un servidor."))
            return
        saved = await self._saved_teams(ctx)
        if not team.strip():
            await ctx.send(tr(lang, f"Your team is {saved[0]['value']}.", f"Tu equipo es {saved[0]['value']}.") if saved else
                           tr(lang, "You have no team saved. Use /football myteam <team>.",
                              "No tienes equipo guardado. Usa /football myteam <equipo>."))
            return
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            row = await self._resolve_team(ctx, client=client, lang=lang, team=team)
        except Exception as exc:
            await ctx.send(tr(lang, f"Could not look up the team: {exc}", f"No se pudo buscar el equipo: {exc}"))
            return
        if row is None:
            return
        memory = ServerMemoryService(self.bot.db)
        for old in saved:
            await memory.delete_memory(ctx.guild.id, old["id"])
        await memory.create_memory(ServerMemoryInput(
            guild_id=ctx.guild.id, memory_type="FOOTBALL_PREFERENCE", key=MY_TEAM_KEY, value=row["team"]["name"],
            created_by_user_id=ctx.author.id, subject_user_id=ctx.author.id, source_type="slash_command",
            metadata={"team_id": row["team"]["id"]}))
        await ctx.send(tr(lang, f"Saved: your team is {row['team']['name']}.", f"Guardado: tu equipo es {row['team']['name']}."))

    @football.command(name="team", description="A team's card: table, season stats, squad, coach, venue, injuries and transfers.")
    @app_commands.describe(team="Team name; leave empty for your team (/football myteam)")
    async def football_team(self, ctx: commands.Context, team: str = "") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            row = await self._resolve_team(ctx, client=client, lang=lang, team=team)
            if row is None:
                return
            team_id, name = row["team"]["id"], row["team"]["name"]
            # The card is scoped to the team's domestic league; a team without one still gets its card.
            league_id = season = None
            league_label = tr(lang, "League", "Liga")
            league_row, outcome = await FootballOperationService(client).resolve_current_domestic_league(row)
            if outcome == FootballOutcome.SELECTED:
                league_id = league_row["league"]["id"]
                season = ApiFootballClient._extract_current_season([league_row])
                league_label = league_row["league"].get("name") or league_label
            operation = compile_football_operation("FOOTBALL_TEAM_QUERY", name, {"data_focus": "team", "team_candidates": [name]})
            result = await FootballOperationService(client).execute(operation, league_id=league_id, season=season, data_focus="team")
            if (result.team_context_row or {}).get("team", {}).get("id") != team_id:
                await ctx.send(tr(lang, "The team could not be identified clearly. Try its full name.",
                                  "No se pudo identificar el equipo con claridad. Prueba su nombre completo."))
                return
            team_row = result.team_context_row
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get team data: {exc}", f"No se pudieron obtener los datos del equipo: {exc}"))
            return

        labels = {"overview": tr(lang, "Overview", "Resumen"),
                  "season": tr(lang, "Season statistics", "Estadisticas de temporada"),
                  "team_squad": tr(lang, "Current squad", "Plantilla actual"),
                  "team_coach": tr(lang, "Coach", "Entrenador"),
                  "team_venue": tr(lang, "Venue", "Estadio"),
                  "injuries": tr(lang, "Injuries", "Lesiones"),
                  "transfers": tr(lang, "Transfers", "Transferencias")}

        def overview(snapshot):
            pages = football_cards.team_pages(
                snapshot.team_context_row, snapshot.standing_row, snapshot.fixtures,
                league=league_label, season=season, lang=lang)
            if snapshot.outcome != FootballOutcome.SELECTED:
                pages[0]["rows"].append((tr(lang, "Data availability", "Disponibilidad de datos"),
                                         self._football_result_message(snapshot, lang)))
            return {key: pages if key == "overview" else None for key in labels}

        async def refresh():
            nonlocal team_row
            snapshot = await FootballOperationService(client).execute(
                operation, league_id=league_id, season=season, data_focus="team")
            if (snapshot.team_context_row or {}).get("team", {}).get("id") != team_id:
                raise ValueError("Team identity changed during refresh")
            team_row = snapshot.team_context_row
            return overview(snapshot)

        async def load_section(section):
            service = FootballOperationService(client)
            if section in {"injuries", "transfers"}:
                records = compile_football_operation("FOOTBALL_LOOKUP", name, {"data_focus": section, "team_candidates": [name]})
                snapshot = await service.execute(records, league_id=league_id, season=season, data_focus=section)
                if snapshot.outcome != FootballOutcome.SELECTED:
                    return [{"title": labels[section], "text": self._football_result_message(snapshot, lang)}]
                return football_cards.record_pages(snapshot.generic_rows, team=team_row["team"], season=season,
                                                   focus=section, lang=lang) or [{"title": labels[section], "text": self._football_result_message(
                    replace(snapshot, outcome=FootballOutcome.NO_DATA_FOR_SCOPE), lang)}]
            snapshot = FootballRetrievalResult(team_context_row=team_row,
                requested_scope={"operation_type": "team_season_statistics" if section == "season" else section,
                                 "league_id": league_id, "season": season})
            if section != "season":
                await service.load_profile_section(snapshot, section)
                if snapshot.outcome != FootballOutcome.SELECTED:
                    return [{"title": labels[section], "text": self._football_result_message(snapshot, lang)}]
                return football_cards.profile_section_pages(snapshot, section, lang=lang)
            if league_id is None:
                return [{"title": name, "text": tr(lang, "No domestic league season was found for this team.",
                                                   "No se encontro una temporada de liga nacional para este equipo.")}]
            await service.load_team_statistics(snapshot, team_row=team_row, league_id=league_id, season=season)
            if snapshot.outcome != FootballOutcome.SELECTED or not snapshot.team_season_statistics:
                return [{"title": name, "text": self._football_result_message(snapshot, lang)}]
            return football_cards.team_statistics_pages(team_row, snapshot.team_season_statistics,
                                                       league=league_label, season=season, lang=lang)

        await self._send_card(ctx, lang=lang, pages=overview(result), labels=labels,
                              section="overview", refresh=refresh, load_section=load_section)

    @football.command(name="match", description="A team's live, next or last match with lineups, events, statistics and odds.")
    @app_commands.describe(team="Team name; leave empty for your team (/football myteam)",
                           when="Auto shows the live match, then today's, then the next ones")
    @app_commands.choices(when=[app_commands.Choice(name=label, value=value) for label, value in
                                [("Auto", "auto"), ("Live", "live"), ("Next", "next"), ("Last", "last")]])
    async def football_match(self, ctx: commands.Context, team: str = "", when: str = "auto") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        if when not in {"auto", "live", "next", "last"}:
            await ctx.send(tr(lang, "Choose Auto, Live, Next or Last.", "Elige Auto, Live, Next o Last."))
            return
        await self._defer_if_needed(ctx)
        try:
            row = await self._resolve_team(ctx, client=client, lang=lang, team=team)
            if row is None:
                return
            provider_team = row["team"]
            fixtures = await FootballLiveMatchService(client).get_team_fixture_choices(provider_team["id"], when=when)
            fixtures = list({item["fixture"]["id"]: item for item in fixtures
                             if type((item.get("fixture") or {}).get("id")) is int and item["fixture"]["id"] > 0}.values())
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get match data: {exc}", f"No se pudo obtener el partido: {exc}"))
            return
        if not fixtures:
            target = provider_team.get("name") or team
            en, es = {
                "live": (f"{target} is not playing a match right now. Check its next or last match.",
                         f"{target} no esta disputando un partido ahora. Revisa su proximo o ultimo partido."),
                "next": (f"No upcoming fixtures are listed for {target}.", f"No hay proximos partidos registrados para {target}."),
                "last": (f"No finished fixtures are recorded for {target}.", f"No hay partidos finalizados registrados para {target}."),
                "auto": (f"No fixtures are available for {target}.", f"No hay partidos disponibles para {target}."),
            }[when]
            await ctx.send(tr(lang, en, es))
            return
        await self._send_match_card(ctx, client=client, lang=lang, fixture_id=fixtures[0]["fixture"]["id"],
                                    section="overview", fixture=fixtures[0],
                                    fixtures=fixtures if len(fixtures) > 1 else None)

    @football.command(name="player", description="A player's profile, stats by competition, career, transfers, trophies and absences.")
    @app_commands.describe(player="Player name", team="Optional team, only when several players share the name")
    async def football_player(self, ctx: commands.Context, player: str, team: str = "") -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            team = team.strip()
            request_text = " / ".join(value for value in (player, team) if value)
            operation = compile_football_operation(
                "FOOTBALL_PLAYER_QUERY",
                request_text,
                {"data_focus": "player_recent_stats", "player_candidates": [player], "team_candidates": [team] if team else []},
            )
            if not operation.player_slots:
                await ctx.send(tr(lang, "I need a clearer player name.", "Necesito un nombre de jugador mas claro."))
                return
            result = await FootballOperationService(
                client,
                player_canonicalizer=self._football_player_canonicalizer(),
                player_alias_cache=self._football_player_alias_cache(),
                player_selector=football_resolver.make_player_selector(
                    getattr(self.bot, "llm_client", None), request_text=request_text, history=[],
                ),
                identity_selector=football_resolver.make_identity_selector(
                    getattr(self.bot, "llm_client", None), request_text=request_text, history=[],
                ),
            ).execute(operation, league_id=None, season=None, data_focus="player_recent_stats")
            self._log_football_diagnostic(
                command="player",
                resolver_input=player,
                normalized_query=" | ".join(operation.player_candidates),
                explicit_context=bool(team),
                endpoint=",".join(result.endpoints),
                params={"team": team or None},
                response_count=len(result.generic_rows) or (1 if result.player_context_row else 0),
                top_candidates=self._player_candidate_names(list(result.generic_rows)),
                final_decision="ambiguous" if result.outcome == FootballOutcome.AMBIGUOUS else ("selected" if result.player_context_row else "not_found"),
                fallback_reason=None if result.player_context_row else "no_player_match",
            )
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get player data: {exc}", f"No se pudo obtener el jugador: {exc}"))
            return
        if result.player_context_row is None and ("team" in result.missing_inputs or
                result.outcome not in {FootballOutcome.AMBIGUOUS, FootballOutcome.NOT_FOUND}):
            await ctx.send(self._football_result_message(result, lang))
            return
        if result.outcome == FootballOutcome.AMBIGUOUS:
            await self._send_player_choices(ctx, client=client, lang=lang, rows=list(result.generic_rows)[:5])
            return
        if result.player_context_row is None:
            await ctx.send(tr(lang, "Player not found.", "No se encontro el jugador."))
            return
        await self._send_player_card(ctx, client=client, lang=lang, result=result)

    async def _send_player_choices(self, ctx: commands.Context, *, client: Any, lang: str, rows: list[dict[str, Any]]) -> None:
        async def choose(index: int) -> None:
            snapshot = FootballRetrievalResult(player_context_row=rows[index])
            await FootballOperationService(client).load_player_statistics(snapshot, rows[index])
            await self._send_player_card(ctx, client=client, lang=lang, result=snapshot)

        view = FootballChoiceView(author_id=ctx.author.id, lang=lang,
                                  labels=[self._player_choice_label(row, lang) for row in rows], choose=choose)
        view.message = await ctx.send(tr(lang, "Several players share that name. Which one?",
                                         "Varios jugadores tienen ese nombre. ¿Cual?"), view=view)

    @staticmethod
    def _player_choice_label(row: dict[str, Any], lang: str) -> str:
        """Name and the facts that tell same-named players apart; never the provider id."""
        player = row.get("player") or {}
        stats = row.get("statistics") or []
        team = ((stats[0] if stats else {}).get("team") or {}).get("name")
        age = tr(lang, f"{player['age']} yrs", f"{player['age']} años") if player.get("age") else None
        parts = [player.get("name") or tr(lang, "Player", "Jugador"), age, player.get("position"), team, player.get("nationality")]
        return " · ".join(str(part) for part in parts if part)[:100]

    async def _send_player_card(self, ctx: commands.Context, *, client: Any, lang: str, result: FootballRetrievalResult) -> None:
        player_row = result.player_context_row
        latest = result.requested_scope.get("latest_registered_season")
        labels = {"profile": tr(lang, "Profile", "Perfil"),
                  "statistics": tr(lang, "Statistics", "Estadisticas"),
                  "previous_season": tr(lang, "Previous season", "Temporada anterior"),
                  "player_teams": tr(lang, "Career teams", "Trayectoria"),
                  "player_transfers": tr(lang, "Transfers", "Transferencias"),
                  "player_trophies": tr(lang, "Trophies", "Palmarés"),
                  "player_sidelined": tr(lang, "Absence history", "Historial de ausencias")}

        def pages(snapshot):
            result_pages = football_cards.player_pages(snapshot.player_context_row,
                season=snapshot.requested_scope.get("latest_registered_season"), lang=lang,
                notice=self._football_result_message(snapshot, lang), latest=True)
            return {key: result_pages.get(key) for key in labels}

        async def refresh():
            nonlocal player_row
            snapshot = FootballRetrievalResult(player_context_row=player_row)
            await FootballOperationService(client).load_player_statistics(snapshot, player_row)
            player_row = snapshot.player_context_row
            return pages(snapshot)

        async def load_section(section):
            if section == "previous_season":
                if type(latest) is not int:
                    return [{"title": labels[section], "text": tr(lang, "No earlier season is registered for this player.",
                                                                  "No hay una temporada anterior registrada para este jugador.")}]
                snapshot = FootballRetrievalResult(player_context_row=player_row)
                await FootballOperationService(client).load_player_statistics(snapshot, player_row, season=latest - 1)
                return football_cards.player_pages(snapshot.player_context_row, season=latest - 1, lang=lang,
                                                   notice=self._football_result_message(snapshot, lang))["statistics"]
            snapshot = FootballRetrievalResult(player_context_row=player_row)
            await FootballOperationService(client).load_profile_section(snapshot, section)
            if snapshot.outcome != FootballOutcome.SELECTED:
                return [{"title": labels[section], "text": self._football_result_message(snapshot, lang)}]
            return football_cards.profile_section_pages(snapshot, section, lang=lang)

        await self._send_card(ctx, lang=lang, pages=pages(result), labels=labels,
                              section="statistics" if player_row.get("statistics") else "profile",
                              refresh=refresh, load_section=load_section)

    @football.command(name="h2h", description="Show head-to-head between two teams.")
    @app_commands.describe(team="Team name", opponent="Opponent name")
    async def football_h2h(self, ctx: commands.Context, team: str, opponent: str) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)
        try:
            selector = football_resolver.make_identity_selector(getattr(self.bot, "llm_client", None),
                                                               request_text=f"{team} vs {opponent}", history=[])
            operation = compile_football_operation("FOOTBALL_COMPARISON", f"{team} vs {opponent}",
                                                   {"data_focus": "h2h", "team_candidates": [team, opponent]})
            operation = replace(operation, fixture_count=10)
            result = await FootballOperationService(client, identity_selector=selector).execute(
                operation, league_id=None, season=None, data_focus="h2h")
            if result.outcome != FootballOutcome.SELECTED:
                await ctx.send(self._football_result_message(result, lang))
                return
            fixtures = result.generic_rows
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get H2H: {exc}", f"No se pudo obtener H2H: {exc}"))
            return
        if not fixtures:
            await ctx.send(tr(lang, f"No meetings are recorded between {team} and {opponent}.", f"No hay enfrentamientos registrados entre {team} y {opponent}."))
            return
        teams = (result.resolved_entities["team"], result.resolved_entities["opponent"])

        async def reload_fixtures():
            return await client.get_head_to_head(team_a_id=teams[0]["id"], team_b_id=teams[1]["id"], last=10)

        await self._send_match_card(ctx, client=client, lang=lang, fixture_id=fixtures[0]["fixture"]["id"],
                                    section="comparison", fixture=fixtures[0], fixtures=fixtures,
                                    list_title=tr(lang, "Head to head", "Historial"),
                                    reload_fixtures=reload_fixtures, h2h_teams=teams)

    @football.command(name="top", description="Show top scorers, assists, or cards.")
    @app_commands.describe(category="scorers, assists, yellowcards, redcards", league=LEAGUE_HELP_TEXT)
    @app_commands.choices(
        category=[
            app_commands.Choice(name="scorers", value="scorers"),
            app_commands.Choice(name="assists", value="assists"),
            app_commands.Choice(name="yellowcards", value="yellowcards"),
            app_commands.Choice(name="redcards", value="redcards"),
        ],
    )
    async def football_top(self, ctx: commands.Context, category: str = "scorers", league: str = "ligamx") -> None:
        key = category.strip().casefold()
        if key in {"assists", "asistencias"}:
            focus = "top_assists"
        elif key in {"yellowcards", "yellow", "amarillas"}:
            focus = "top_yellow_cards"
        elif key in {"redcards", "red", "rojas"}:
            focus = "top_red_cards"
        else:
            focus = "top_scorers"
        await self._send_league_card(ctx, league=league, focus=focus)

    @football_fixtures.autocomplete("league")
    @football_table.autocomplete("league")
    @football_top.autocomplete("league")
    async def football_match_league_autocomplete(self, interaction: discord.Interaction, current: str):
        query = current.strip()
        choices = [app_commands.Choice(name=self._league_label(code, "en"), value=code) for code in LEAGUE_CODES
                   if not query or query.casefold() in code.casefold() or query.casefold() in self._league_label(code, "en").casefold()]
        client = getattr(self.bot, "api_football_client", None)
        if client is None or len(query) < 3 or self._normalize_league_key(query):
            return choices
        try:
            request = build_league_search_request(search=_make_league_slot(query, source="slash_arg"))
            rows = await client.search_leagues(request)
            seen = set()
            for row in rows:
                item = row.get("league") or {}
                league_id, name = item.get("id"), item.get("name")
                if type(league_id) is not int or not name or league_id in seen:
                    continue
                seen.add(league_id)
                country = (row.get("country") or {}).get("name")
                label = f"{name} ({country})" if country else name
                choices.append(app_commands.Choice(name=label[:100], value=str(league_id)))
                if len(choices) == 25:
                    break
        except Exception:
            logging.debug("Football league autocomplete unavailable", exc_info=True)
        return choices

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

    async def _send_match_card(self, ctx: commands.Context, *, client: Any, lang: str,
                               fixture_id: int, section: str, fixture: dict | None = None,
                               fixtures: list[dict] | None = None, list_title: str | None = None,
                               reload_fixtures=None, h2h_teams: tuple[dict, dict] | None = None) -> None:
        service = FootballLiveMatchService(client)
        labels = {"overview": tr(lang, "Match", "Partido"),
                  "statistics": tr(lang, "Statistics", "Estadisticas"),
                  "lineups": tr(lang, "Lineups", "Alineaciones"),
                  "events": tr(lang, "Events", "Eventos"),
                  "players": tr(lang, "Player performances", "Actuaciones de jugadores"),
                  "periods": tr(lang, "Period scores", "Marcadores por periodo"),
                  "preview": tr(lang, "Preview", "Previa"),
                  "season_comparison": tr(lang, "Season comparison", "Comparación de temporada"),
                  "predictions": tr(lang, "Provider forecast", "Pronóstico"),
                  "odds": tr(lang, "Odds", "Cuotas")}
        if list_title is not None:
            labels = {"fixtures": tr(lang, "Fixtures", "Partidos"), **labels}
        if h2h_teams is not None:
            labels = {"comparison": tr(lang, "Comparison", "Comparación"), **labels}
        try:
            base = await service.get_match_center(fixture_id, time_scope="fixture_id", fixture=fixture,
                                                   cache_ttl_seconds=30)
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get fixture details: {exc}", f"No se pudo obtener detalles: {exc}"))
            return
        if base.fixture is None:
            await ctx.send(tr(lang, "Fixture not found.", "No se encontro el partido."))
            return

        def overview():
            pages = {key: football_cards.match_pages(base, key, lang=lang) if key in {"overview", "periods"}
                     else None for key in labels}
            if not pages["periods"]:
                pages["periods"] = [{"title": labels["periods"], "text": tr(lang,
                    "The provider has not supplied period scores for this fixture.",
                    "El proveedor no ha proporcionado marcadores por periodo para este partido.")}]
            if list_title is not None:
                pages["fixtures"] = football_cards.fixture_list_pages(fixtures, title=list_title, lang=lang)
            if h2h_teams is not None:
                pages["comparison"] = football_cards.h2h_summary_pages(fixtures, h2h_teams, lang=lang)
            return pages

        async def refresh_list():
            fixtures[:] = await reload_fixtures()
            return overview()

        async def refresh():
            nonlocal base
            snapshot = await service.get_match_center(fixture_id, time_scope="fixture_id", cache_ttl_seconds=30)
            if snapshot.fixture_id != fixture_id:
                raise ValueError("The selected fixture is no longer available")
            base = snapshot
            if fixtures is not None:
                for index, item in enumerate(fixtures):
                    if item["fixture"]["id"] == fixture_id:
                        fixtures[index] = snapshot.fixture
            return overview()

        async def select_fixture(selected_id):
            nonlocal base, fixture_id
            selected = next(item for item in fixtures if item["fixture"]["id"] == selected_id)
            snapshot = await service.get_match_center(selected_id, time_scope="fixture_id", fixture=selected)
            base, fixture_id = snapshot, selected_id
            return overview()

        async def load_odds(page_number=1):
            league = base.fixture.get("league") or {}
            if type(league.get("id")) is not int or type(league.get("season")) is not int:
                return [{"title": labels["odds"], "text": tr(lang, "Competition/season scope is unavailable for these odds.",
                    "No hay contexto de competición/temporada para estas cuotas.")}]
            live = base.lifecycle.is_live
            focus = "live_odds" if live else "pre_match_odds"
            operation = compile_football_operation("FOOTBALL_LOOKUP", focus,
                {"data_focus": focus, "fixture_focus": str(fixture_id), "reference_page": None if live else page_number})
            result = await FootballOperationService(client).execute(operation, league_id=league["id"],
                                                                    season=league["season"], data_focus=focus)
            if result.outcome != FootballOutcome.SELECTED:
                message = self._football_result_message(result, lang)
                if result.outcome == FootballOutcome.NO_DATA_FOR_SCOPE:
                    message = tr(lang, "No odds are published for this match in the selected feed.",
                                 "No hay cuotas publicadas para este partido en la fuente seleccionada.")
                return [{"title": labels["odds"], "text": message}]
            pages = football_cards.odds_pages(result.odds, fixture=base.fixture, lang=lang, live=live)
            pages = pages or [{"title": labels["odds"], "text": tr(lang, "No market selections are published for this match.",
                                "No hay selecciones de mercado publicadas para este partido.")}]
            paging = result.requested_scope.get("paging") or {}
            current, total = paging.get("current"), paging.get("total")
            if not live and type(current) is int and type(total) is int and page_number <= current < total:
                pages[-1]["more"] = current + 1
            return pages

        async def load_section(key):
            if key == "odds":
                return await load_odds()
            if key == "preview":
                result = await FootballOperationService(client).get_fixture_preview(base)
                return football_cards.preview_pages(base, result, lang=lang)
            if key in {"predictions", "season_comparison"}:
                league = base.fixture.get("league") or {}
                if type(league.get("id")) is not int or type(league.get("season")) is not int:
                    return [{"title": labels[key], "text": tr(lang, "Competition/season details are unavailable.",
                                                              "No hay datos de competición/temporada.")}]
                operations = FootballOperationService(client)
                if key == "predictions":
                    guard = FootballRetrievalResult(match_data=base)
                    if await operations.check_coverage(guard, "predictions", league_id=league["id"], season=league["season"]):
                        return [{"title": labels[key], "text": self._football_result_message(guard, lang)}]
                    rows = await client.get_predictions(build_prediction_request(fixture_id=fixture_id))
                    return football_cards.prediction_pages(rows, fixture=base.fixture, lang=lang) or [
                        {"title": labels[key], "text": tr(lang, "No provider forecast is published for this match.",
                                                          "No hay pronóstico del proveedor publicado para este partido.")}]
                metadata = await client.get_league_by_id(league_id=league["id"], season=league["season"])
                if not any((row.get("league") or {}).get("id") == league["id"] and
                           any(item.get("year") == league["season"] for item in row.get("seasons") or []) for row in metadata):
                    return [{"title": labels[key], "text": tr(lang, "The provider has no data for this competition/season.",
                        "El proveedor no tiene datos para esta competición/temporada.")}]
                snapshots = []
                for side in ("home", "away"):
                    snapshot = FootballRetrievalResult()
                    try:
                        await operations.load_team_statistics(snapshot, team_row={"team": base.fixture["teams"][side]},
                                                              league_id=league["id"], season=league["season"])
                    except FootballApiError:
                        snapshot.outcome = FootballOutcome.PROVIDER_FAILURE
                    snapshots.append(snapshot)
                pages = football_cards.season_comparison_pages(base.fixture, snapshots, lang=lang) if any(
                    item.team_season_statistics for item in snapshots) else []
                pages.extend({"title": base.fixture["teams"][side].get("name") or labels[key],
                              "text": self._football_result_message(item, lang)}
                             for side, item in zip(("home", "away"), snapshots) if item.outcome != FootballOutcome.SELECTED)
                return pages
            flag, capability, embedded = {
                "statistics": ("include_stats", "statistics_fixtures", "statistics"),
                "events": ("include_events", "events", "events"),
                "lineups": ("include_lineups", "lineups", "lineups"),
                "players": ("include_players", "statistics_players", "players"),
            }[key]
            league = base.fixture.get("league") or {}
            if not base.fixture.get(embedded) and type(league.get("id")) is int and type(league.get("season")) is int:
                guard = FootballRetrievalResult(match_data=base)
                unsupported = await FootballOperationService(client).check_coverage(
                    guard, capability, league_id=league.get("id"), season=league.get("season"))
                if unsupported:
                    return [{"title": labels[key], "text": self._football_result_message(guard, lang)}]
            snapshot = await service.get_match_center(fixture_id, time_scope="fixture_id", fixture=base.fixture,
                                                       **{flag: True})
            pages = football_cards.match_pages(snapshot, key, lang=lang)
            empty = {
                "statistics": ("The provider has not supplied statistics for this fixture yet.",
                               "El proveedor todavia no ha proporcionado estadisticas de este partido."),
                "events": ("No events are recorded for this fixture yet.",
                           "Todavia no hay eventos registrados para este partido."),
                "lineups": ("No lineups have been published for this fixture yet.",
                            "Todavia no se han publicado alineaciones para este partido."),
                "players": ("No player performance records are available for this fixture yet.",
                            "Todavia no hay registros de actuaciones de jugadores para este partido."),
            }
            return pages or [{"title": labels[key], "text": tr(lang, *empty[key])}]

        pages = overview()
        if fixtures and list_title is None:
            section = "overview"
            pages["overview"] = [{"title": tr(lang, "Choose a match", "Elige un partido"), "text": tr(lang,
                "Select a match below to open its scoreboard and details.",
                "Selecciona un partido para abrir su marcador y detalles.")}]
        elif pages[section] is None:
            try:
                pages[section] = await load_section(section)
            except Exception:
                logging.exception("Football match section loading failed fixture=%s section=%s", fixture_id, section)
                pages[section] = [{"title": labels[section], "text": tr(lang,
                    "Could not load this section. Use Refresh to try again.",
                    "No se pudo cargar esta seccion. Usa Actualizar para reintentar.")}]
        await self._send_card(ctx, lang=lang, pages=pages, labels=labels, section=section,
                              refresh=refresh, load_section=load_section,
                              fixtures=fixtures, select_fixture=select_fixture,
                              refresh_list=refresh_list if list_title is not None else None,
                              load_more=lambda key, page: load_odds(page))

    async def _send_card(self, ctx: commands.Context, *, lang: str, pages: dict,
                         labels: dict[str, str], section: str, refresh, load_section=None,
                         fixtures: list[dict] | None = None, select_fixture=None, refresh_list=None, load_more=None) -> None:
        view = FootballCardView(author_id=ctx.author.id, lang=lang, pages=pages, labels=labels,
                                section=section, media_cache=self._table_logo_cache,
                                can_attach=getattr(ctx, "bot_permissions", discord.Permissions.all()).attach_files,
                                refresh=refresh, load_section=load_section,
                                fixtures=fixtures, select_fixture=select_fixture, refresh_list=refresh_list,
                                load_more=load_more)
        payload = await view.payload()
        files = payload.pop("attachments")
        try:
            view.message = await ctx.send(**payload, files=files)
        except discord.HTTPException:
            if not files:
                raise
            payload = view.text_payload()
            payload.pop("attachments")
            view.message = await ctx.send(**payload)

    async def _send_league_card(self, ctx: commands.Context, *, league: str, focus: str) -> None:
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
            is_table = focus == "standings"
            operation = compile_football_operation("FOOTBALL_TABLE" if is_table else "FOOTBALL_LOOKUP", league,
                {"data_focus": "scorers" if focus == "top_scorers" else focus, "league_candidates": [league]})

            async def retrieve():
                return await FootballOperationService(client).execute(operation, league_id=league_id,
                                                                       season=season, data_focus=focus)

            def build(snapshot):
                if is_table:
                    labels = {"compact": tr(lang, "Table", "Tabla"), "detailed": tr(lang, "Detailed", "Detallada"),
                              "home": tr(lang, "Home", "Local"), "away": tr(lang, "Away", "Visitante")}
                else:
                    labels = {"ranking": tr(lang, "Ranking", "Clasificación")}
                rows = snapshot.standings_table if is_table else snapshot.generic_rows
                if snapshot.outcome != FootballOutcome.SELECTED:
                    notice = self._football_result_message(snapshot, lang)
                elif not rows:
                    name = self._league_label(league_key, lang)
                    if is_table:
                        notice = tr(lang, f"No standings are recorded for {name} (season {season}).",
                                    f"No hay tabla registrada para {name} (temporada {season}).")
                    elif focus == "top_scorers":
                        notice = tr(lang, f"No scorer entries are recorded for {name} (season {season}).",
                                    f"No hay registros de goleadores para {name} (temporada {season}).")
                    else:
                        notice = tr(lang, f"No ranking entries are recorded for {name} (season {season}).",
                                    f"No hay registros de clasificación para {name} (temporada {season}).")
                else:
                    notice = None
                if notice:
                    return {key: [{"title": label, "text": notice}] for key, label in labels.items()}, labels
                branding = dict((snapshot.league_context_row or {}).get("league") or {})
                branding["name"] = branding.get("name") or self._league_label(league_key, lang)
                if is_table:
                    pages = {key: football_cards.standings_pages(rows, league=branding, season=season,
                                                                 section=key, lang=lang) for key in labels}
                else:
                    pages = {"ranking": football_cards.leaderboard_pages(rows, league=branding, season=season,
                                                                         focus=focus, lang=lang)}
                return pages, labels

            result = await retrieve()
            pages, labels = build(result)
        except Exception as exc:
            await ctx.send(tr(lang, f"Failed to get league data: {exc}", f"No se pudieron obtener los datos de liga: {exc}"))
            return
        if pages[next(iter(labels))][0].get("text"):
            await ctx.send(pages[next(iter(labels))][0]["text"])
            return

        async def refresh():
            return build(await retrieve())[0]

        await self._send_card(ctx, lang=lang, pages=pages, labels=labels,
                              section=next(iter(labels)), refresh=refresh)

    @staticmethod
    def _football_result_message(result: Any, lang: str) -> str:
        if result.outcome == FootballOutcome.AMBIGUOUS:
            return tr(lang, "The provider returned multiple matches. Please provide more specific details.", "El proveedor devolvio varias coincidencias. Proporciona detalles mas especificos.")
        if result.outcome == FootballOutcome.NOT_FOUND:
            return tr(lang, "No matching provider data was found.", "No se encontraron datos del proveedor.")
        if result.outcome == FootballOutcome.NO_DATA_FOR_SCOPE:
            team = (result.team_context_row or {}).get("team") or {}
            name = team.get("name") or tr(lang, "this team", "este equipo")
            if "/injuries" in result.endpoints and team:
                league = (result.league_context_row or {}).get("league") or {}
                competition = league.get("name") or tr(lang, "the selected competition", "la competicion seleccionada")
                season = result.requested_scope.get("season")
                scope = competition + (tr(lang, f" (season {season})", f" (temporada {season})") if season is not None else "")
                return tr(lang, f"No injuries are recorded for {name} in {scope}.", f"No hay lesiones registradas para {name} en {scope}.")
            if "/transfers" in result.endpoints and team:
                return tr(lang, f"No transfers are recorded for {name}.", f"No hay transferencias registradas para {name}.")
            operation = result.requested_scope.get("operation_type")
            if operation == "player_recent_stats":
                season = result.requested_scope.get("season")
                scope = tr(lang, f"season {season}", f"temporada {season}") if season is not None else tr(lang, "the selected scope", "el alcance seleccionado")
                return tr(lang, f"No season statistics are recorded for this player in {scope}.",
                          f"No hay estadisticas de temporada registradas para este jugador en {scope}.")
            if operation == "team_season_statistics":
                return tr(lang, "No team season statistics are recorded in the selected scope.",
                          "No hay estadisticas de temporada del equipo en el alcance seleccionado.")
            if "/fixtures" in result.endpoints and operation in {"fixture_next", "fixture_last"}:
                target = team.get("name") or tr(lang, "the selected competition", "la competicion seleccionada")
                if operation == "fixture_next":
                    return tr(lang, f"No upcoming fixtures are listed for {target}.", f"No hay proximos partidos registrados para {target}.")
                return tr(lang, f"No previous fixtures are recorded for {target}.", f"No hay partidos anteriores registrados para {target}.")
            return tr(lang, "No matching records were returned for this request.", "No se devolvieron registros coincidentes para esta consulta.")
        if result.outcome == FootballOutcome.UNSUPPORTED_BY_COVERAGE:
            return tr(lang, "The provider does not cover that data for this competition or season.", "El proveedor no cubre esos datos para esta competicion o temporada.")
        if result.outcome == FootballOutcome.PROVIDER_FAILURE:
            return tr(lang, "The provider could not load this data. Please try again.",
                      "El proveedor no pudo cargar estos datos. Intentalo de nuevo.")
        return tr(lang, "The requested football data could not be resolved.", "No se pudo resolver la informacion de futbol solicitada.")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FootballCog(bot))
