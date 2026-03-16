from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import discord
from discord.ext import commands

from utils.i18n import normalize_language, tr

HELP_SECTION_ALIASES: dict[str, str] = {
    "basic": "basic",
    "home": "basic",
    "start": "basic",
    "basico": "basic",
    "basica": "basic",
    "list": "sections",
    "sections": "sections",
    "index": "sections",
    "indice": "sections",
    "secciones": "sections",
    "general": "general",
    "ai": "general",
    "ia": "general",
    "chat": "general",
    "coding": "coding",
    "code": "coding",
    "programming": "coding",
    "programacion": "coding",
    "sports": "sports",
    "sport": "sports",
    "deportes": "sports",
    "liga": "sports",
    "ligamx": "sports",
    "fun": "fun",
    "diversion": "fun",
    "moderation": "moderation",
    "mod": "moderation",
    "moderacion": "moderation",
    "admin": "admin",
    "announcement": "announcements",
    "announcements": "announcements",
    "welcome": "announcements",
    "goodbye": "announcements",
    "anuncios": "announcements",
    "variables": "variables",
    "variable": "variables",
    "vars": "variables",
}

HELP_SECTION_LABELS_EN: dict[str, str] = {
    "basic": "basic",
    "sections": "sections",
    "general": "general",
    "coding": "coding",
    "sports": "sports",
    "fun": "fun",
    "moderation": "moderation",
    "admin": "admin",
    "announcements": "announcements",
    "variables": "variables",
}

HELP_SECTION_LABELS_ES: dict[str, str] = {
    "basic": "basico",
    "sections": "secciones",
    "general": "general",
    "coding": "programacion",
    "sports": "deportes",
    "fun": "diversion",
    "moderation": "moderacion",
    "admin": "admin",
    "announcements": "anuncios",
    "variables": "variables",
}


class HelpPaginatorView(discord.ui.View):
    def __init__(
        self,
        *,
        pages: list[discord.Embed],
        author_id: int,
        lang: str,
    ) -> None:
        super().__init__(timeout=180)
        self.pages = pages
        self.author_id = author_id
        self.lang = lang
        self.current_page = 0
        self.message: discord.Message | None = None

        self.prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=tr(lang, "Previous", "Anterior"),
            row=0,
        )
        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=tr(lang, "Next", "Siguiente"),
            row=0,
        )
        self.prev_button.callback = self._on_prev
        self.next_button.callback = self._on_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self._sync_controls()

    def _sync_controls(self) -> None:
        single_page = len(self.pages) <= 1
        self.prev_button.disabled = single_page
        self.next_button.disabled = single_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            tr(
                self.lang,
                "Only the user who opened this help panel can control these buttons.",
                "Solo el usuario que abriÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³ este panel de ayuda puede usar estos botones.",
            ),
            ephemeral=True,
        )
        return False

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page - 1) % len(self.pages)
        self._sync_controls()
        await interaction.response.edit_message(
            embed=self.pages[self.current_page], view=self
        )

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page + 1) % len(self.pages)
        self._sync_controls()
        await interaction.response.edit_message(
            embed=self.pages[self.current_page], view=self
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _update_prefix(self, ctx: commands.Context, new_prefix: str) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        new_prefix = new_prefix.strip()
        if not new_prefix:
            await ctx.send("Prefix cannot be empty.")
            return
        if len(new_prefix) > 5:
            await ctx.send("Prefix is too long. Use up to 5 characters.")
            return

        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_prefix(guild.id, new_prefix)
        await ctx.send(f"Prefix updated to `{new_prefix}`.")

    async def _collect_channel_messages(
        self, channel: discord.TextChannel, lang: str
    ) -> list[str]:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        lines: list[str] = []

        async for msg in channel.history(limit=1000, after=since, oldest_first=True):
            if msg.author.bot:
                continue

            content = (msg.content or "").strip()
            if not content:
                if msg.attachments:
                    content = tr(lang, "[attachment]", "[archivo adjunto]")
                else:
                    continue

            content = " ".join(content.split())
            if len(content) > 260:
                content = f"{content[:257]}..."

            lines.append(f"{msg.author.display_name}: {content}")
            if len(lines) >= 400:
                break

        return lines

    @commands.hybrid_command(name="setmodlog", description="Set the moderation log channel.")
    @commands.has_permissions(manage_guild=True)
    async def set_modlog(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_modlog_channel(guild.id, channel.id if channel else None)
        if channel is None:
            await ctx.send("Mod-log channel cleared.")
            return
        await ctx.send(f"Mod-log channel set to {channel.mention}.")

    @commands.hybrid_command(
        name="setprefix", description="Set the text command prefix for this server."
    )
    @commands.has_permissions(manage_guild=True)
    async def set_prefix(self, ctx: commands.Context, prefix: str) -> None:
        await self._update_prefix(ctx, prefix)

    @commands.hybrid_command(
        name="language",
        description="Set bot language for command responses. Allowed: en, es.",
    )
    @commands.has_permissions(manage_guild=True)
    async def language(self, ctx: commands.Context, language_code: str) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        requested = language_code.strip().lower()
        if requested not in {"en", "es"}:
            await ctx.send("Invalid language code. Use `en` or `es`.")
            return

        lang = normalize_language(requested)
        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_language(guild.id, lang)
        await ctx.send(
            tr(
                lang,
                f"Bot language updated to `{lang}`.",
                f"Idioma del bot actualizado a `{lang}`.",
            )
        )

    @commands.hybrid_command(
        name="setservercontext",
        description="Analyze one text channel (last 7 days) and update AI server context.",
    )
    @commands.has_permissions(manage_guild=True)
    async def set_server_context(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(guild)

        if ctx.interaction:
            await ctx.defer()

        try:
            lines = await self._collect_channel_messages(channel, lang)
        except discord.Forbidden:
            await ctx.send(
                tr(
                    lang,
                    "I do not have permission to read message history in that channel.",
                    "No tengo permisos para leer el historial de mensajes en ese canal.",
                )
            )
            return
        except discord.HTTPException as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to read channel history: {exc}",
                    f"No se pudo leer el historial del canal: {exc}",
                )
            )
            return

        if not lines:
            await ctx.send(
                tr(
                    lang,
                    "I could not find enough user messages from the last 7 days in that channel. "
                    "If the channel is old/inactive, choose a more active one.",
                    "No encontrÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© suficientes mensajes de usuarios de los ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âºltimos 7 dÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­as en ese canal. "
                    "Si el canal es antiguo o inactivo, elige uno mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡s activo.",
                )
            )
            return

        transcript = "\n".join(lines)
        if len(transcript) > 22000:
            transcript = transcript[:22000]

        try:
            summary = await self.bot.llm_client.summarize_server_context(
                channel_name=channel.name,
                messages_transcript=transcript,
                language=lang,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to analyze channel context: {exc}",
                    f"No se pudo analizar el contexto del canal: {exc}",
                )
            )
            return

        await self.bot.db.upsert_server_context_entry(
            guild_id=guild.id,
            channel_id=channel.id,
            channel_name=channel.name,
            summary=summary,
            max_entries=2,
        )
        await ctx.send(
            tr(
                lang,
                "I now understand how the people in the server behave, I will talk like you from now!",
                "Ahora entiendo cÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â³mo se comporta la gente en el servidor, ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡hablarÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â© como ustedes de ahora en adelante!",
            )
        )

    @commands.hybrid_command(name="setup", description="Show bot setup and capabilities.")
    async def setup_help(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await ctx.send(
            tr(
                lang,
                "Hello! I'm Nitori. I can help you moderate your server, chat with users, "
                "check Minecraft server status, follow Liga MX matches, and generate memes too!\n"
                "If you want me to learn your server vibe, run `/setservercontext` and choose a text channel from the server list. "
                "I will analyze the last 7 days of that channel.",
                "Hola! Soy Nitori. Puedo ayudarte a moderar tu servidor, chatear con los usuarios "
                "revisar el estado de servidores de Minecraft, seguir Liga MX y generar memes tambiÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©n!\n"
                "Si quieres que aprenda mejor la vibra del servidor, usa `/setservercontext` y elige un canal de texto de la lista del servidor. "
                "AnalizarÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© los ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âºltimos 7 dÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­as de ese canal.",
            )
        )

    @commands.hybrid_command(
        name="antispam", description="Enable or disable the basic anti-spam filter."
    )
    @commands.has_permissions(manage_guild=True)
    async def antispam(self, ctx: commands.Context, enabled: bool) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.set_anti_spam(ctx.guild.id, enabled)
        await ctx.send(f"Anti-spam is now {'enabled' if enabled else 'disabled'}.")

    @commands.hybrid_command(
        name="antilink", description="Enable or disable the basic anti-link filter."
    )
    @commands.has_permissions(manage_guild=True)
    async def antilink(self, ctx: commands.Context, enabled: bool) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.set_anti_link(ctx.guild.id, enabled)
        await ctx.send(f"Anti-link is now {'enabled' if enabled else 'disabled'}.")

    @commands.hybrid_group(
        name="aichannel",
        description="Manage AI-allowed channels.",
        invoke_without_command=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def aichannel(self, ctx: commands.Context) -> None:
        await self.aichannellist(ctx)

    @aichannel.command(
        name="add",
        description="Allow AI chat/translation in a specific channel.",
    )
    @commands.has_permissions(manage_guild=True)
    async def aichanneladd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.add_ai_channel(ctx.guild.id, channel.id)
        await ctx.send(f"AI channel added: {channel.mention}.")

    @aichannel.command(
        name="remove",
        description="Remove a channel from the AI-allowed channel list.",
    )
    @commands.has_permissions(manage_guild=True)
    async def aichannelremove(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        scope, allowed_channels = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        if scope == "all":
            await ctx.send(
                "AI is currently allowed in all channels. Use `/aichannel add` first to enable channel restrictions."
            )
            return
        if channel.id not in allowed_channels:
            await ctx.send(f"{channel.mention} is not currently in the AI-allowed list.")
            return

        await self.bot.db.remove_ai_channel(ctx.guild.id, channel.id)
        scope_after, allowed_after = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        if scope_after == "none":
            await ctx.send(
                f"AI channel removed: {channel.mention}. AI is now disabled in all channels "
                "until you add at least one channel with `/aichannel add`."
            )
            return
        await ctx.send(
            f"AI channel removed: {channel.mention}. Remaining AI-allowed channels: {len(allowed_after)}."
        )

    @aichannel.command(
        name="list",
        description="List channels where AI chat/translation is currently allowed.",
    )
    @commands.has_permissions(manage_guild=True)
    async def aichannellist(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        scope, channel_ids = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        if scope == "all":
            await ctx.send("AI is allowed in all channels (no channel restrictions set).")
            return
        if scope == "none":
            await ctx.send(
                "AI is currently disabled in all channels. Use `/aichannel add <#channel>` to allow specific channels."
            )
            return

        mentions = []
        for channel_id in channel_ids:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                mentions.append(channel.mention)
            else:
                mentions.append(f"`{channel_id}`")
        await ctx.send("AI allowed channels:\n" + "\n".join(mentions))

    @aichannel.command(
        name="clear",
        description="Clear AI channel restrictions (AI allowed in all channels).",
    )
    @commands.has_permissions(manage_guild=True)
    async def aichannelclear(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.clear_ai_channels(ctx.guild.id)
        await ctx.send("AI channel restrictions cleared. AI is now allowed in all channels.")

    @commands.hybrid_command(
        name="help",
        description="Show the full help guide with pages.",
    )
    @discord.app_commands.describe(
        section="Open a specific help section (for example: fun, sports, moderation, admin).",
    )
    async def help_cmd(
        self,
        ctx: commands.Context,
        *,
        section: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        pages = self._build_help_pages(lang, member=member)
        section_keys = self._help_section_keys(member)

        page_index = 0
        if section:
            resolved = self._resolve_help_section(section, section_keys)
            if resolved is None:
                labels = HELP_SECTION_LABELS_EN if lang == "en" else HELP_SECTION_LABELS_ES
                available = ", ".join(f"`{labels.get(key, key)}`" for key in section_keys)
                await ctx.send(
                    tr(
                        lang,
                        f"Unknown help section. Try one of: {available}",
                        f"Seccion de ayuda no valida. Prueba una de estas: {available}",
                    )
                )
                return
            page_index = resolved

        view = HelpPaginatorView(
            pages=pages,
            author_id=ctx.author.id,
            lang=lang,
        )
        view.current_page = page_index
        sent = await ctx.send(embed=pages[page_index], view=view)
        if isinstance(sent, discord.Message):
            view.message = sent

    @help_cmd.autocomplete("section")
    async def help_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        keys = self._help_section_keys(member)
        lang = await self._lang(interaction.guild)
        labels = HELP_SECTION_LABELS_EN if lang == "en" else HELP_SECTION_LABELS_ES
        current_norm = self._normalize_help_section(current)
        choices: list[discord.app_commands.Choice[str]] = []
        for key in keys:
            label = labels.get(key, key)
            if current_norm and current_norm not in label:
                continue
            choices.append(discord.app_commands.Choice(name=label, value=label))
        return choices[:25]

    def _help_section_keys(self, member: discord.Member | None) -> list[str]:
        is_admin = self._is_admin_member(member)
        is_mod = self._is_mod_member(member)
        keys = ["basic", "sections", "general", "coding", "sports", "fun"]
        if is_mod:
            keys.append("moderation")
        if is_admin:
            keys.extend(["admin", "announcements", "variables"])
        return keys

    @staticmethod
    def _normalize_help_section(value: str) -> str:
        normalized = value.strip().casefold().replace("_", " ").replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    def _resolve_help_section(
        self,
        raw_value: str,
        allowed_keys: list[str],
    ) -> int | None:
        normalized = self._normalize_help_section(raw_value)
        if not normalized:
            return 0
        if normalized.isdigit():
            page_number = int(normalized)
            if 1 <= page_number <= len(allowed_keys):
                return page_number - 1
            return None
        mapped = HELP_SECTION_ALIASES.get(normalized)
        if mapped is None or mapped not in allowed_keys:
            return None
        return allowed_keys.index(mapped)

    @staticmethod
    def _is_admin_member(member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    @staticmethod
    def _is_mod_member(member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = member.guild_permissions
        return bool(
            perms.administrator
            or perms.manage_guild
            or perms.manage_messages
            or perms.manage_channels
            or perms.manage_roles
            or perms.moderate_members
            or perms.kick_members
            or perms.ban_members
            or perms.manage_nicknames
        )

    def _build_help_pages(
        self,
        lang: str,
        *,
        member: discord.Member | None = None,
    ) -> list[discord.Embed]:
        quick_en = """`/help`
`/setup`
`/srvstatus <ip_or_domain>`
`/football live ligamx`
`/meme create <template> <top_text> [bottom_text]`"""
        quick_es = """`/help`
`/setup`
`/srvstatus <ip_o_dominio>`
`/football live ligamx`
`/meme create <plantilla> <texto_arriba> [texto_abajo]`"""

        is_admin = self._is_admin_member(member)
        is_mod = self._is_mod_member(member)

        general_en = """`@Nitori <message>`
`reply + @Nitori <language>`
`/translate <language> [text]`
`/roast <user|user-id|name>`
`/roastme`
`/remindme <time> <message>`
`/unremindme <reminder>`
`/help`
`/setup`
`/srvstatus <ip_or_domain>`
Time units: `m`, `h`, `d`, `w`, `mo`, `y`
Example: `/roast @User`"""
        general_es = """`@Nitori <mensaje>`
`responde + @Nitori <idioma>`
`/translate <idioma> [texto]`
`/roast <usuario|id-usuario|nombre>`
`/roastme`
`/remindme <tiempo> <mensaje>`
`/unremindme <recordatorio>`
`/help`
`/setup`
`/srvstatus <ip_o_dominio>`
Unidades de tiempo: `m`, `h`, `d`, `w`, `mo`, `y`
Ejemplo: `/roast @User`"""

        coding_en = """`/code code:<code> language:<language> [source_file]` - Compile/run code.
`/codelangs` - List supported languages and file extensions.
Languages: `c`, `c#`, `cpp`, `python`, `java`, `javascript`, `rust`
Files: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`"""
        coding_es = """`/code code:<codigo> language:<lenguaje> [archivo_fuente]` - Compila/ejecuta codigo.
`/codelangs` - Lista lenguajes y extensiones soportadas.
Lenguajes: `c`, `c#`, `cpp`, `python`, `java`, `javascript`, `rust`
Archivos: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`"""

        sports_en = """`/football live <league>` - Live matches happening now.
`/football today <league>` - Matches scheduled for today.
`/football next <league> [count|team]` - Next matches (global or by team).
`/football last <league> <team>` - Last result for a specific team.
`/football table <league>` - Current league standings table.
`/football team <league> <team>` - Team details and recent form.
`/football scorers <league>` - Top scorers leaderboard.
Leagues: `ligamx`, `premier`, `laliga`, `concacaf`."""
        sports_es = """`/football live <liga>` - Partidos en vivo ahora mismo.
`/football today <liga>` - Partidos programados para hoy.
`/football next <liga> [cantidad|equipo]` - Proximos partidos (global o por equipo).
`/football last <liga> <equipo>` - Ultimo resultado de un equipo.
`/football table <liga>` - Tabla de posiciones actual.
`/football team <liga> <equipo>` - Datos del equipo y forma reciente.
`/football scorers <liga>` - Tabla de goleadores.
Ligas: `ligamx`, `premier`, `laliga`, `concacaf`."""

        fun_en = """`/joke` - Random joke.
`/dadjoke` - Random dad joke.
`/advice` - Random advice.
`/whois <domain>` - Domain WHOIS info.
`/convert <amount> <from_unit> <to_unit>` - Unit conversion.
`/roast <user|user-id|name>` - Roast a target user.
`/roastme` - Roast yourself.
`/meme create <template> <top_text> [bottom_text]` - Template meme.
`/meme random <top_text> [bottom_text]` - Random template meme.
`/meme custom <image_url> <top_text> [bottom_text]` - URL image meme.
`/meme custom <top_text> [bottom_text]` + attach image - Attached image meme.
`/meme templates [query]` - List meme templates.
`/meme fonts [query]` - List meme fonts.
`/speech <user>` - Speech bubble avatar meme.
`/meme help` - Meme usage guide."""
        fun_es = """`/joke` - Chiste aleatorio.
`/dadjoke` - Chiste de papa aleatorio.
`/advice` - Consejo aleatorio.
`/whois <dominio>` - Info WHOIS del dominio.
`/convert <cantidad> <unidad_origen> <unidad_destino>` - Conversion de unidades.
`/roast <usuario|id-usuario|nombre>` - Roast a un usuario.
`/roastme` - Roast para ti.
`/meme create <plantilla> <texto_arriba> [texto_abajo]` - Meme con plantilla.
`/meme random <texto_arriba> [texto_abajo]` - Meme con plantilla aleatoria.
`/meme custom <url_imagen> <texto_arriba> [texto_abajo]` - Meme desde URL.
`/meme custom <texto_arriba> [texto_abajo]` + adjunta imagen - Meme con imagen adjunta.
`/meme templates [busqueda]` - Lista plantillas de meme.
`/meme fonts [busqueda]` - Lista fuentes de meme.
`/speech <usuario>` - Meme de burbuja de dialogo.
`/meme help` - Guia de memes."""

        mod_a_en = """`/message delete <amount>` - Delete recent messages.
`/utility say <message>` - Bot sends your text.
`/channel add <channel-name>` - Create a text channel.
`/channel delete <#channel>` - Delete a channel.
`/channel clear [#channel]` - Clear messages in a channel.
`/channel clone [#channel]` - Clone channel settings.
`/channel lock [#channel]` - Lock posting permissions.
`/channel unlock [#channel]` - Restore posting permissions.
`/channel slowmode <#channel|channel-id> <seconds|disable>` - Set or disable slowmode.
`/message purgeuser <user|user-id> <amount>` - Remove one user's messages.
`/user info <user|user-id>` - Show member info.
`/user warn <user> [reason]` - Add a warning.
`/user unwarn <user> <1|2|3>` - Remove a warning slot.
`/user warnings <user>` - View active warnings.
`/user clearwarnings <user> [reason]` - Clear all warnings."""
        mod_a_es = """`/message delete <cantidad>` - Borra mensajes recientes.
`/utility say <mensaje>` - El bot envia tu texto.
`/channel add <nombre-canal>` - Crea un canal de texto.
`/channel delete <#canal>` - Elimina un canal.
`/channel clear [#canal]` - Limpia mensajes de un canal.
`/channel clone [#canal]` - Clona configuracion del canal.
`/channel lock [#canal]` - Bloquea envio de mensajes.
`/channel unlock [#canal]` - Restaura envio de mensajes.
`/channel slowmode <#canal|id-canal> <segundos|disable>` - Configura o desactiva slowmode.
`/message purgeuser <usuario|id-usuario> <cantidad>` - Borra mensajes de un usuario.
`/user info <usuario|id-usuario>` - Muestra info del miembro.
`/user warn <usuario> [razon]` - Agrega una advertencia.
`/user unwarn <usuario> <1|2|3>` - Quita una advertencia.
`/user warnings <usuario>` - Ver advertencias activas.
`/user clearwarnings <usuario> [razon]` - Limpia todas las advertencias."""

        mod_b_en = """`/user mute <user> [reason]` - Mute a member.
`/user unmute <user> [reason]` - Remove mute role.
`/user kick <user> [reason]` - Kick a member.
`/user ban <user> [reason]` - Ban a member.
`/user unban <user_id> [reason]` - Unban by user ID.
`/user tempmute <user> <time> [reason]` - Temporary mute.
`/user tempban <user> <time> [reason]` - Temporary ban.
`/role add <user> <role|role-id>` - Add a role to a member.
`/role remove <user> <role|role-id>` - Remove a role from a member.
`/role create <role-name> [hex-color]` - Create a role.
`/user setnick <user> <nickname>` - Change member nickname."""
        mod_b_es = """`/user mute <usuario> [razon]` - Silencia a un miembro.
`/user unmute <usuario> [razon]` - Quita el rol de silencio.
`/user kick <usuario> [razon]` - Expulsa a un miembro.
`/user ban <usuario> [razon]` - Banea a un miembro.
`/user unban <id_usuario> [razon]` - Desbanea por ID.
`/user tempmute <usuario> <tiempo> [razon]` - Silencio temporal.
`/user tempban <usuario> <tiempo> [razon]` - Ban temporal.
`/role add <usuario> <rol|id-rol>` - Agrega un rol al miembro.
`/role remove <usuario> <rol|id-rol>` - Quita un rol al miembro.
`/role create <nombre-rol> [color-hex]` - Crea un rol.
`/user setnick <usuario> <apodo>` - Cambia el apodo del miembro."""

        admin_en = """`/config modlog [#channel]` - Set moderation log channel.
`/config prefix <new_prefix>` - Change server prefix.
`/config language <en|es>` - Set response language.
`/config servercontext <#channel>` - Add/update AI context channel.
`/config antispam <true|false>` - Toggle anti-spam filter.
`/config antilink <true|false>` - Toggle anti-link filter.
`/color setup` - Create default color roles.
`/color list` - Show public color panel.
`/color channel <#channel>` - Set color panel channel.
`/color add <hex-color> [name]` - Add a custom color role.
`/color remove <name>` - Remove a color role.
`/color reload` - Repost/update color panel."""
        admin_es = """`/config modlog [#canal]` - Define canal de logs de moderacion.
`/config prefix <nuevo_prefijo>` - Cambia el prefijo del servidor.
`/config language <en|es>` - Define idioma de respuestas.
`/config servercontext <#canal>` - Agrega/actualiza canal de contexto IA.
`/config antispam <true|false>` - Activa/desactiva filtro anti-spam.
`/config antilink <true|false>` - Activa/desactiva filtro anti-links.
`/color setup` - Crea roles de color por defecto.
`/color list` - Muestra panel publico de colores.
`/color channel <#canal>` - Define canal del panel de colores.
`/color add <color-hex> [nombre]` - Agrega rol de color personalizado.
`/color remove <nombre>` - Elimina un rol de color.
`/color reload` - Republica/actualiza panel de colores."""

        announcements_en = """`/welcome show`
`/welcome set mode:<text|embed|both> [channel] [message] [image] [color]`
`/welcome edit [message] [color] [mode] [image] [channel]`
`/welcome test`
Example: `/welcome set mode:both message:Hello {user}, welcome to {server}! image:https://... color:#00FFAA`

`/goodbye show`
`/goodbye set mode:<text|embed|both> [channel] [message] [image] [color]`
`/goodbye edit [message] [color] [mode] [image] [channel]`
`/goodbye test`
Example: `/goodbye set mode:both message:Bye {user}, thanks for being here. image:https://... color:#FF4D4D`"""

        announcements_es = """`/welcome show`
`/welcome set mode:<text|embed|both> [channel] [message] [image] [color]`
`/welcome edit [message] [color] [mode] [image] [channel]`
`/welcome test`
Ejemplo: `/welcome set mode:both message:Hola {user}, bienvenido a {server}! image:https://... color:#00FFAA`

`/goodbye show`
`/goodbye set mode:<text|embed|both> [channel] [message] [image] [color]`
`/goodbye edit [message] [color] [mode] [image] [channel]`
`/goodbye test`
Ejemplo: `/goodbye set mode:both message:Adios {user}, gracias por estar aqui. image:https://... color:#FF4D4D`"""

        variables_en = """`{user}` -> @John
`{username}` -> John
`{avatar}` -> https://imagelink.../avatar.png
`{server}` -> Server name
`{channel}` -> #Channel (current welcome/goodbye channel)
`{role}` -> @Moderators (replace `role` with role name, example: `{Moderators}`)
For channels in custom text: use `{#channel-name}` or `{123456789012345678}`."""

        variables_es = """`{user}` -> @John
`{username}` -> John
`{avatar}` -> https://imagelink.../avatar.png
`{server}` -> Nombre de servidor
`{channel}` -> #Canal (canal actual de welcome/goodbye)
`{role}` -> @Moderadores (reemplaza `role` por el nombre del rol, ejemplo: `{Moderadores}`)
Para canales en texto personalizado: usa `{#nombre-canal}` o `{123456789012345678}`."""

        ai_channels_en = """`/aichannel add <#channel>` - Allow AI in one channel.
`/aichannel remove <#channel>` - Remove one allowed AI channel.
`/aichannel list` - Show current AI-allowed channels/scope.
`/aichannel clear` - Remove restrictions (AI allowed in all channels)."""
        ai_channels_es = """`/aichannel add <#canal>` - Permite IA en un canal.
`/aichannel remove <#canal>` - Quita un canal permitido para IA.
`/aichannel list` - Muestra canales/alcance actual de IA.
`/aichannel clear` - Quita restricciones (IA permitida en todos los canales)."""

        page1 = discord.Embed(
            title=tr(lang, "Nitori Help", "Ayuda de Nitori"),
            description=tr(
                lang,
                "Welcome. Use the buttons below to navigate command pages.",
                "Bienvenido. Usa los botones de abajo para navegar por las paginas de comandos.",
            ),
            color=discord.Color.blurple(),
        )
        page1.add_field(name=tr(lang, "Quick Start", "Inicio Rapido"), value=tr(lang, quick_en, quick_es), inline=False)
        page1.add_field(
            name=tr(lang, "Usage Note", "Nota de uso"),
            value=tr(
                lang,
                "Most commands support prefix usage too.",
                "La mayoria de comandos tambien soportan prefijo.",
            ),
            inline=False,
        )
        page1.add_field(
            name=tr(lang, "Notes", "Notas"),
            value=tr(lang, "Some commands require mod/admin permissions.", "Algunos comandos requieren permisos de mod/admin."),
            inline=False,
        )

        section_items_en = [
            "1. Basic help",
            "2. Sections index",
            "3. General / AI",
            "4. Coding",
            "5. Sports",
            "6. Fun",
        ]
        section_items_es = [
            "1. Ayuda basica",
            "2. Indice de secciones",
            "3. General / IA",
            "4. Programacion",
            "5. Deportes",
            "6. Diversion",
        ]
        if is_mod:
            section_items_en.append("7. Moderation")
            section_items_es.append("7. Moderacion")
        if is_admin:
            section_items_en.extend(["8. Admin", "9. Welcome/Goodbye", "10. Variables"])
            section_items_es.extend(["8. Admin", "9. Welcome/Goodbye", "10. Variables"])
        sections_en = "\n".join(section_items_en)
        sections_es = "\n".join(section_items_es)

        page2 = discord.Embed(title=tr(lang, "Help Sections", "Secciones de Ayuda"), color=discord.Color.blurple())
        page2.add_field(name=tr(lang, "This Guide Contains", "Esta Guia Contiene"), value=tr(lang, sections_en, sections_es), inline=False)
        page2.add_field(
            name=tr(lang, "Target Formats", "Formatos de objetivo"),
            value=tr(lang, "Moderation commands accept: @mention, user ID, username, or display name.", "Los comandos de moderacion aceptan: @mencion, ID, nombre de usuario o nombre visible."),
            inline=False,
        )

        page3 = discord.Embed(title=tr(lang, "General / AI", "General / IA"), color=discord.Color.green())
        page3.add_field(name=tr(lang, "General + AI", "General + IA"), value=tr(lang, general_en, general_es), inline=False)

        page4 = discord.Embed(title=tr(lang, "Coding", "Programacion"), color=discord.Color.dark_teal())
        page4.add_field(name=tr(lang, "Code Execution", "Ejecucion de codigo"), value=tr(lang, coding_en, coding_es), inline=False)

        page5 = discord.Embed(title=tr(lang, "Sports", "Deportes"), color=discord.Color.gold())
        page5.add_field(name=tr(lang, "Football", "Football"), value=tr(lang, sports_en, sports_es), inline=False)

        page6 = discord.Embed(title=tr(lang, "Fun", "Diversion"), color=discord.Color.teal())
        page6.add_field(name=tr(lang, "Meme + Ninja APIs", "Memes + APIs Ninja"), value=tr(lang, fun_en, fun_es), inline=False)

        pages = [page1, page2, page3, page4, page5, page6]
        if is_mod:
            page_mod = discord.Embed(title=tr(lang, "Moderation Commands", "Comandos de Moderacion"), color=discord.Color.orange())
            page_mod.add_field(name=tr(lang, "Messages + Warnings", "Mensajes + Advertencias"), value=tr(lang, mod_a_en, mod_a_es), inline=False)
            page_mod.add_field(name=tr(lang, "Member Actions", "Acciones de miembros"), value=tr(lang, mod_b_en, mod_b_es), inline=False)
            pages.append(page_mod)

        if is_admin:
            page_admin = discord.Embed(title=tr(lang, "Admin Commands", "Comandos de Admin"), color=discord.Color.red())
            page_admin.add_field(name=tr(lang, "Server Configuration", "Configuracion del servidor"), value=tr(lang, admin_en, admin_es), inline=False)
            page_admin.add_field(name=tr(lang, "AI Channel Restrictions", "Canales de IA"), value=tr(lang, ai_channels_en, ai_channels_es), inline=False)
            pages.append(page_admin)

            page_ann = discord.Embed(
                title=tr(lang, "Welcome / Goodbye", "Welcome / Goodbye"),
                color=discord.Color.dark_gold(),
            )
            page_ann.add_field(
                name=tr(lang, "Announcement Commands", "Comandos de anuncios"),
                value=tr(lang, announcements_en, announcements_es),
                inline=False,
            )
            pages.append(page_ann)

            page_vars = discord.Embed(
                title=tr(lang, "Template Variables", "Variables de plantilla"),
                color=discord.Color.dark_blue(),
            )
            page_vars.add_field(
                name=tr(lang, "Variable Examples", "Ejemplos de variables"),
                value=tr(lang, variables_en, variables_es),
                inline=False,
            )
            pages.append(page_vars)

        self._set_page_footers(pages, lang)
        return pages

    def _set_page_footers(self, pages: list[discord.Embed], lang: str) -> None:
        total = len(pages)
        for index, embed in enumerate(pages, start=1):
            prefix = tr(lang, "Page", "PÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡gina")
            footer = tr(
                lang,
                "Use /help and the buttons to navigate.",
                "Usa /help y los botones para navegar.",
            )
            embed.set_footer(text=f"{footer} | {prefix} {index}/{total}")

    @set_modlog.error
    @set_prefix.error
    @language.error
    @set_server_context.error
    @setup_help.error
    @antispam.error
    @antilink.error
    @aichannel.error
    @aichanneladd.error
    @aichannelremove.error
    @aichannellist.error
    @aichannelclear.error
    async def admin_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You do not have permission to use this command.")
            return
        await ctx.send(f"Command failed: {error}")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))



