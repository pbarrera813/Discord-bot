from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re

import discord
from discord.ext import commands, tasks

from services.database import AI_INTERACTIONS_CONTEXT_CHANNEL_ID
from services.server_memory import ServerMemoryInput, ServerMemoryService
from utils.i18n import normalize_language, tr
from utils.permissions import owner_or_has_permissions

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
    "birthday": "birthday",
    "birthdays": "birthday",
    "cumple": "birthday",
    "cumples": "birthday",
    "cumpleanos": "birthday",
    "cumpleaños": "birthday",
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
    "birthday": "birthday",
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
    "birthday": "cumpleaños",
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
                "Solo el usuario que abrió este panel de ayuda puede usar estos botones.",
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
        self.server_context_refresh_worker.start()

    def cog_unload(self) -> None:
        self.server_context_refresh_worker.cancel()

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
            if self._is_context_line_suspicious(content):
                continue
            if len(content) > 260:
                content = f"{content[:257]}..."

            lines.append(f"{msg.author.display_name}: {content}")
            if len(lines) >= 400:
                break

        return lines

    async def _summarize_channel_context(
        self,
        *,
        channel: discord.TextChannel,
        lang: str,
    ) -> str | None:
        lines = await self._collect_channel_messages(channel, lang)
        if not lines:
            return None

        transcript = "\n".join(lines)
        if len(transcript) > 22000:
            transcript = transcript[:22000]

        return await self.bot.llm_client.summarize_server_context(
            channel_name=channel.name,
            messages_transcript=transcript,
            language=lang,
        )

    @staticmethod
    def _is_context_entry_stale(entry: dict[str, object]) -> bool:
        raw = str(entry.get("updated_at", "")).strip()
        if not raw:
            return True
        try:
            updated_at = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated_at >= timedelta(days=7)

    @staticmethod
    def _has_enough_interaction_context(rows: list[dict[str, object]]) -> bool:
        if len(rows) < 20:
            return False

        user_rows = [
            row for row in rows if str(row.get("role", "")).strip().lower() == "user"
        ]
        if len(user_rows) < 8:
            return False

        speakers = {
            str(row.get("speaker", "")).strip().casefold()
            for row in user_rows
            if str(row.get("speaker", "")).strip()
        }
        if len(speakers) < 2:
            return False

        total_chars = sum(
            len(str(row.get("content", "")).strip())
            for row in rows
        )
        return total_chars >= 500

    @staticmethod
    def _interaction_rows_to_transcript(rows: list[dict[str, object]]) -> str:
        lines: list[str] = []
        for row in rows:
            speaker = str(row.get("speaker", "")).strip() or "User"
            content = " ".join(str(row.get("content", "")).strip().split())
            if not content:
                continue
            if not content.casefold().startswith(f"{speaker.casefold()}:"):
                content = f"{speaker}: {content}"
            if len(content) > 260:
                content = f"{content[:257]}..."
            lines.append(content)
            if len(lines) >= 400:
                break
        return "\n".join(lines)

    @tasks.loop(hours=24)
    async def server_context_refresh_worker(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._refresh_guild_server_context(guild)
            except Exception:
                logging.exception("AI server context refresh failed in guild=%s", guild.id)

    @server_context_refresh_worker.before_loop
    async def before_server_context_refresh_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_guild_server_context(self, guild: discord.Guild) -> None:
        entries = await self.bot.db.get_server_context_entries(guild.id)
        real_entries = [
            entry for entry in entries if int(entry.get("channel_id", 0)) > 0
        ]
        lang = await self._lang(guild)

        if real_entries:
            for entry in real_entries:
                if not self._is_context_entry_stale(entry):
                    continue
                channel_id = int(entry["channel_id"])
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    summary = await self._summarize_channel_context(
                        channel=channel,
                        lang=lang,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logging.exception(
                        "Failed to refresh AI channel context guild=%s channel=%s",
                        guild.id,
                        channel_id,
                    )
                    continue
                if not summary:
                    continue
                await self.bot.db.upsert_server_context_entry(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    channel_name=channel.name,
                    summary=summary,
                    max_entries=2,
                )
            return

        sentinel_entry = next(
            (
                entry
                for entry in entries
                if int(entry.get("channel_id", 0)) == AI_INTERACTIONS_CONTEXT_CHANNEL_ID
            ),
            None,
        )
        if sentinel_entry is not None and not self._is_context_entry_stale(sentinel_entry):
            return

        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = await self.bot.db.get_recent_ai_conversation_turns(guild.id, since)
        if not self._has_enough_interaction_context(rows):
            return

        transcript = self._interaction_rows_to_transcript(rows)
        if not transcript:
            return
        summary = await self.bot.llm_client.summarize_server_context(
            channel_name="ai-interactions",
            messages_transcript=transcript,
            language=lang,
        )
        await self.bot.db.upsert_server_context_entry(
            guild_id=guild.id,
            channel_id=AI_INTERACTIONS_CONTEXT_CHANNEL_ID,
            channel_name="ai-interactions",
            summary=summary,
            max_entries=2,
        )

    @staticmethod
    def _is_context_line_suspicious(content: str) -> bool:
        lowered = content.casefold()
        markers = (
            "ignore previous instructions",
            "ignore all instructions",
            "disregard previous instructions",
            "you are now system",
            "act as system",
            "developer prompt",
            "system prompt",
            "reveal prompt",
            "jailbreak",
            "prompt injection",
        )
        return any(marker in lowered for marker in markers)

    @commands.hybrid_command(name="setmodlog", description="Set the moderation log channel.")
    @owner_or_has_permissions(manage_guild=True)
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
    @owner_or_has_permissions(manage_guild=True)
    async def set_prefix(self, ctx: commands.Context, prefix: str) -> None:
        await self._update_prefix(ctx, prefix)

    @commands.hybrid_command(
        name="language",
        description="Set bot language for command responses. Allowed: en, es.",
    )
    @owner_or_has_permissions(manage_guild=True)
    @discord.app_commands.rename(language_code="language")
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
    @owner_or_has_permissions(manage_guild=True)
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
            summary = await self._summarize_channel_context(
                channel=channel,
                lang=lang,
            )
        except discord.Forbidden:
            await ctx.send(
                tr(
                    lang,
                    "I do not have permission to read message history in that channel.",
                    "No tengo permisos para leer el historial de mensajes en ese canal.",
                )
            )
            return
        except discord.HTTPException:
            logging.exception("Failed to read channel history for context in guild=%s", guild.id)
            await ctx.send(
                tr(
                    lang,
                    "Failed to read channel history.",
                    "No se pudo leer el historial del canal.",
                )
            )
            return
        except Exception:
            logging.exception("Failed to summarize server context in guild=%s", guild.id)
            await ctx.send(
                tr(
                    lang,
                    "Failed to analyze channel context right now. Try again in a moment.",
                    "No se pudo analizar el contexto del canal en este momento. Intenta de nuevo en un momento.",
                )
            )
            return

        if not summary:
            await ctx.send(
                tr(
                    lang,
                    "I could not find enough user messages from the last 7 days in that channel. "
                    "If the channel is old/inactive, choose a more active one.",
                    "No encontré suficientes mensajes de usuarios de los últimos 7 días en ese canal. "
                    "Si el canal es antiguo o inactivo, elige uno más activo.",
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
                "Ahora entiendo cómo se comporta la gente en el servidor, ¡hablaré como ustedes de ahora en adelante!",
            )
        )

    @commands.hybrid_command(
        name="resetservercontext",
        description="Reset AI server context and stored AI conversation memory.",
    )
    @discord.app_commands.describe(scope="What to reset: summaries, memory, ai_history, or all.")
    @owner_or_has_permissions(manage_guild=True)
    async def reset_server_context(self, ctx: commands.Context, scope: str = "all") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(guild)
        scope_key = scope.strip().casefold()
        if scope_key not in {"summaries", "memory", "ai_history", "all"}:
            await ctx.send(
                tr(
                    lang,
                    "Invalid scope. Use `summaries`, `memory`, `ai_history`, or `all`.",
                    "Scope invalido. Usa `summaries`, `memory`, `ai_history` o `all`.",
                )
            )
            return
        await self._reset_server_context_scope(guild.id, scope_key)
        await ctx.send(
            tr(
                lang,
                f"AI server context reset complete for scope `{scope_key}`.",
                f"Reinicio de contexto IA completado para scope `{scope_key}`.",
            )
        )

    async def _reset_server_context_scope(self, guild_id: int, scope: str) -> None:
        if scope == "all":
            await self.bot.db.reset_ai_server_context(guild_id)
            await ServerMemoryService(self.bot.db).clear_memories(guild_id)
        elif scope == "summaries":
            await self.bot.db.reset_server_context_summaries(guild_id)
        elif scope == "memory":
            await ServerMemoryService(self.bot.db).clear_memories(guild_id)
        elif scope == "ai_history":
            await self.bot.db.reset_ai_conversation_turns(guild_id)
        if scope in {"all", "ai_history"}:
            ai_cog = self.bot.get_cog("AIChatCog")
            if ai_cog is not None and hasattr(ai_cog, "clear_guild_history"):
                ai_cog.clear_guild_history(guild_id)

    @commands.hybrid_command(
        name="viewservercontext",
        description="View the stored AI server context for this server.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def view_server_context(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        settings = await self.bot.db.get_guild_settings(guild.id)
        entries = await self.bot.db.get_server_context_entries(guild.id)
        counts = await ServerMemoryService(self.bot.db).counts(guild.id)
        output = self._format_server_context_view(settings.server_context, entries, counts)
        await self._send_server_context_view(ctx, output)

    @commands.hybrid_group(
        name="servercontext",
        description="Manage structured AI server memory.",
        fallback="view",
        invoke_without_command=True,
    )
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_group(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        settings = await self.bot.db.get_guild_settings(guild.id)
        entries = await self.bot.db.get_server_context_entries(guild.id)
        counts = await ServerMemoryService(self.bot.db).counts(guild.id)
        output = self._format_server_context_view(settings.server_context, entries, counts)
        await self._send_server_context_view(ctx, output)

    @server_context_group.command(name="remember", description="Store a structured server memory.")
    @discord.app_commands.describe(
        memory_type="Type, for example USER_NICKNAME, SERVER_RULE, CHANNEL_CONTEXT.",
        value="Value to remember.",
        user="Optional target user.",
        channel="Optional target channel.",
        key="Optional memory key.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_remember(
        self,
        ctx: commands.Context,
        memory_type: str,
        value: str,
        user: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
        key: str = "",
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        try:
            normalized_type = ServerMemoryService.normalize_memory_type(memory_type)
            memory_key = key or ("preferred_nickname" if normalized_type == "USER_NICKNAME" else normalized_type.casefold())
            row = await ServerMemoryService(self.bot.db).create_memory(
                ServerMemoryInput(
                    guild_id=guild.id,
                    memory_type=normalized_type,
                    subject_user_id=user.id if user else None,
                    subject_channel_id=channel.id if channel else None,
                    key=memory_key,
                    value=value,
                    created_by_user_id=ctx.author.id,
                    source_type="command",
                    approved_by_user_id=ctx.author.id,
                )
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(tr(lang, f"Saved structured memory `{row.get('id')}`.", f"Memoria estructurada guardada `{row.get('id')}`."))

    @server_context_group.command(name="forget", description="Archive a structured server memory.")
    @discord.app_commands.describe(memory_id="Memory ID from /servercontext list or view.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_forget(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        ok = await ServerMemoryService(self.bot.db).archive_memory(guild.id, memory_id)
        await ctx.send(tr(lang, "Memory archived." if ok else "Memory not found.", "Memoria archivada." if ok else "Memoria no encontrada."))

    @server_context_group.command(name="list", description="List structured server memories.")
    @discord.app_commands.describe(memory_type="Optional memory type.", status="active, pending, rejected, archived, or expired.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_list(self, ctx: commands.Context, memory_type: str = "", status: str = "active") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        try:
            rows = await ServerMemoryService(self.bot.db).list_memories(
                guild.id,
                memory_type=memory_type or None,
                status=status or None,
                limit=25,
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self._send_server_context_view(ctx, self._format_server_memory_rows(rows))

    @server_context_group.command(name="user", description="List structured memories about a user.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_user(self, ctx: commands.Context, user: discord.Member) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        rows = await ServerMemoryService(self.bot.db).list_user_memories(guild.id, user.id)
        await self._send_server_context_view(ctx, self._format_server_memory_rows(rows))

    @server_context_group.command(name="approve", description="Approve a pending structured memory.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_approve(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok = await ServerMemoryService(self.bot.db).approve_memory(guild.id, memory_id, ctx.author.id)
        await ctx.send("Memory approved." if ok else "Memory not found.")

    @server_context_group.command(name="reject", description="Reject a pending structured memory.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_reject(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok = await ServerMemoryService(self.bot.db).reject_memory(guild.id, memory_id, ctx.author.id)
        await ctx.send("Memory rejected." if ok else "Memory not found.")

    @server_context_group.command(name="reset", description="Reset one server context scope.")
    @discord.app_commands.describe(scope="summaries, memory, ai_history, or all.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_reset(self, ctx: commands.Context, scope: str = "all") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        scope_key = scope.strip().casefold()
        if scope_key not in {"summaries", "memory", "ai_history", "all"}:
            await ctx.send(
                tr(
                    lang,
                    "Invalid scope. Use `summaries`, `memory`, `ai_history`, or `all`.",
                    "Scope invalido. Usa `summaries`, `memory`, `ai_history` o `all`.",
                )
            )
            return
        await self._reset_server_context_scope(guild.id, scope_key)
        await ctx.send(
            tr(
                lang,
                f"AI server context reset complete for scope `{scope_key}`.",
                f"Reinicio de contexto IA completado para scope `{scope_key}`.",
            )
        )

    @staticmethod
    def _format_server_context_view(
        server_context: str | None,
        entries: list[dict[str, object]],
        memory_counts: list[dict[str, object]] | None = None,
    ) -> str:
        context = (server_context or "").strip()
        if not context and not entries and not memory_counts:
            return "No AI server context is currently stored."

        lines = ["AI server context currently stored:"]
        if memory_counts:
            lines.append("")
            lines.append("Structured memory:")
            for item in memory_counts:
                lines.append(
                    f"- {item.get('memory_type', 'UNKNOWN')} / {item.get('status', 'unknown')}: {item.get('count', 0)}"
                )
        if entries:
            lines.append("")
            lines.append("Sources:")
            for entry in entries:
                channel_id = int(entry.get("channel_id", 0))
                channel_name = str(entry.get("channel_name", "")).strip() or "unknown"
                updated_at = str(entry.get("updated_at", "")).strip() or "unknown time"
                if channel_id == AI_INTERACTIONS_CONTEXT_CHANNEL_ID:
                    label = f"AI interactions ({channel_id})"
                else:
                    label = f"#{channel_name} ({channel_id})"
                lines.append(f"- {label}, updated {updated_at}")

        if context:
            lines.append("")
            lines.append("Context:")
            lines.append(context)
        else:
            lines.append("")
            lines.append("No combined context text is currently stored.")
        return "\n".join(lines)

    async def _send_server_context_view(self, ctx: commands.Context, output: str) -> None:
        chunks = self._split_context_view_output(output)
        ephemeral = bool(ctx.interaction)
        for chunk in chunks:
            if ephemeral:
                await ctx.send(chunk, ephemeral=True)
            else:
                await ctx.send(chunk)

    @staticmethod
    def _format_server_memory_rows(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "No structured server memories found."
        lines = ["Structured server memories:"]
        for row in rows:
            target = ""
            if row.get("subject_user_id"):
                target = f" user={row.get('subject_user_id')}"
            elif row.get("subject_channel_id"):
                target = f" channel={row.get('subject_channel_id')}"
            lines.append(
                f"- `{row.get('id')}` {row.get('memory_type')} {row.get('status')}{target} "
                f"{row.get('key')}: {row.get('value')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _split_context_view_output(output: str, limit: int = 1900) -> list[str]:
        text = output.strip() or "No AI server context is currently stored."
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        if remaining:
            chunks.append(remaining)
        return chunks or ["No AI server context is currently stored."]

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
                "revisar el estado de servidores de Minecraft, seguir Liga MX y generar memes también!\n"
                "Si quieres que aprenda mejor la vibra del servidor, usa `/setservercontext` y elige un canal de texto de la lista del servidor. "
                "Analizaré los últimos 7 días de ese canal.",
            )
        )

    @commands.hybrid_command(
        name="antispam", description="Enable or disable the basic anti-spam filter."
    )
    @owner_or_has_permissions(manage_guild=True)
    async def antispam(self, ctx: commands.Context, enabled: bool) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.set_anti_spam(ctx.guild.id, enabled)
        await ctx.send(f"Anti-spam is now {'enabled' if enabled else 'disabled'}.")

    @commands.hybrid_command(
        name="antilink", description="Enable or disable the basic anti-link filter."
    )
    @owner_or_has_permissions(manage_guild=True)
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
    @owner_or_has_permissions(manage_guild=True)
    async def aichannel(self, ctx: commands.Context) -> None:
        await self.aichannellist(ctx)

    @aichannel.command(
        name="add",
        description="Allow AI chat/translation in a specific channel.",
    )
    @owner_or_has_permissions(manage_guild=True)
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
    @owner_or_has_permissions(manage_guild=True)
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
    @owner_or_has_permissions(manage_guild=True)
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
    @owner_or_has_permissions(manage_guild=True)
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
                if ctx.interaction is not None:
                    await ctx.send(
                        tr(
                            lang,
                            f"Unknown help section. Try one of: {available}",
                            f"Seccion de ayuda no valida. Prueba una de estas: {available}",
                        ),
                        ephemeral=True,
                    )
                else:
                    await ctx.send(
                        tr(
                            lang,
                            f"Unknown help section. Try one of: {available}",
                            f"Seccion de ayuda no valida. Prueba una de estas: {available}",
                        ),
                    )
                return
            page_index = resolved

        view = HelpPaginatorView(
            pages=pages,
            author_id=ctx.author.id,
            lang=lang,
        )
        view.current_page = page_index
        if ctx.interaction is not None:
            sent = await ctx.send(embed=pages[page_index], view=view, ephemeral=True)
        else:
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
        keys = ["basic", "sections", "general", "birthday", "coding", "sports", "fun"]
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

        birthday_en = """`/birthday set <MM-DD|DD/MM> [birth_year]` - Save your birthday.
`/birthday remove` - Remove your birthday data from this server.
`/birthday next [count]` - Show upcoming birthdays in this server.
Examples:
`/birthday set 07-14`
`/birthday set 14/07 2001`
`/birthday remove`"""
        birthday_es = """`/birthday set <MM-DD|DD/MM> [año_nacimiento]` - Guarda tu cumpleaños.
`/birthday remove` - Elimina tus datos de cumpleaños de este servidor.
`/birthday next [cantidad]` - Muestra los próximos cumpleaños del servidor.
Ejemplos:
`/birthday set 07-14`
`/birthday set 14/07 2001`
`/birthday remove`"""
        birthday_admin_en = """`/birthday setup [channel] [role]` - Quick server birthday setup.
`/birthday channel [#channel]` - Set/clear announcement channel.
`/birthday role [@role]` - Set/clear birthday role.
`/birthday timezone <iana_tz>` - Set server timezone.
`/birthday mode <user|server>` - Choose timezone mode for birthdays.
`/birthday ages <true|false>` - Show or hide ages.
`/birthday event <default|year|join|server|disable> [color] [image] [message]` - Configure/disable event messages.
`/birthday preview <default|year|server|user>` - Preview how each event will be posted.
`/birthday templateadd|templatelist|templateremove` - Manage custom templates.
`/birthday blacklistuser|blacklistrole <target> <true|false>` - Exclude users/roles.
`/birthday trusted [role] [prevent_message] [prevent_role] [prevent_list]` - Trusted-role restrictions."""
        birthday_admin_es = """`/birthday setup [canal] [rol]` - Configuración rápida del módulo.
`/birthday channel [#canal]` - Define/limpia el canal de anuncios.
`/birthday role [@rol]` - Define/limpia el rol de cumpleaños.
`/birthday timezone <zona_iana>` - Define zona horaria del servidor.
`/birthday mode <user|server>` - Modo de zona horaria para cumpleaños.
`/birthday ages <true|false>` - Muestra u oculta edades.
`/birthday event <default|year|join|server|disable> [color] [image] [message]` - Configura/desactiva mensajes del evento.
`/birthday preview <default|year|server|user>` - Vista previa de cómo se publica cada evento.
`/birthday templateadd|templatelist|templateremove` - Gestiona plantillas personalizadas.
`/birthday blacklistuser|blacklistrole <objetivo> <true|false>` - Excluye usuarios/roles.
`/birthday trusted [rol] [prevent_message] [prevent_role] [prevent_list]` - Restricciones por rol confiable."""

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
`/football match <fixture|team>` - Match center by fixture ID or team.
`/football schedule <team|league> [next|last|season]` - Fixture schedule.
`/football player <player>` - Player profile and season stats.
`/football lineup <fixture_id>` - Confirmed fixture lineups.
`/football stats <fixture_id>` - Fixture statistics.
`/football injuries <team>` - Injuries/unavailable players.
`/football transfers <team>` - Recent transfers.
`/football h2h <team_a> <team_b>` - Head-to-head matches.
`/football top <scorers|assists|yellowcards|redcards>` - Leaderboards.
`/football preview <fixture_id>` - Data-only match preview.
`/football summary <fixture_id>` - Data-only match summary.
Leagues: `ligamx`, `premier`, `laliga`, `concacaf`, `worldcup`."""
        sports_es = """`/football live <liga>` - Partidos en vivo ahora mismo.
`/football today <liga>` - Partidos programados para hoy.
`/football next <liga> [cantidad|equipo]` - Próximos partidos (global o por equipo).
`/football last <liga> <equipo>` - Último resultado de un equipo.
`/football table <liga>` - Tabla de posiciones actual.
`/football team <liga> <equipo>` - Datos del equipo y forma reciente.
`/football scorers <liga>` - Tabla de goleadores.
`/football match <partido|equipo>` - Centro de partido por ID o equipo.
`/football schedule <equipo|liga> [next|last|season]` - Calendario de partidos.
`/football player <jugador>` - Perfil y estadísticas del jugador.
`/football lineup <fixture_id>` - Alineaciones confirmadas.
`/football stats <fixture_id>` - Estadísticas del partido.
`/football injuries <equipo>` - Lesiones/jugadores no disponibles.
`/football transfers <equipo>` - Transferencias recientes.
`/football h2h <equipo_a> <equipo_b>` - Historial entre equipos.
`/football top <scorers|assists|yellowcards|redcards>` - Tablas de líderes.
`/football preview <fixture_id>` - Previa del partido con datos.
`/football summary <fixture_id>` - Resumen del partido con datos.
Ligas: `ligamx`, `premier`, `laliga`, `concacaf`, `worldcup`."""

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
`/setservercontext <#channel>` - Add/update AI context channel.
`/resetservercontext [scope]` - Clear summaries, memory, AI history, or all.
`/viewservercontext` - View summary context and structured memory counts.
`/servercontext remember|forget|list|user|approve|reject|reset` - Manage structured AI memory.
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
`/setservercontext <#canal>` - Agrega/actualiza canal de contexto IA.
`/resetservercontext [scope]` - Limpia resumenes, memoria, historial IA o todo.
`/viewservercontext` - Muestra contexto resumen y conteos de memoria estructurada.
`/servercontext remember|forget|list|user|approve|reject|reset` - Gestiona memoria IA estructurada.
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

        variables_en = """Bot variables:
`{user}` -> @John
`{username}` -> John
`{avatar}` -> Shows the user's avatar
`{server}` -> Server name
`{channel}` -> Supports channel name or channel ID. Example: `{rules}` -> #rules
`{role}` -> Role helper. Use `{role:member}` or `{role:123456789012345678}`
`{RoleName}` -> Also works by role name. Example: `{member}` -> @Member
`{123456789012345678}` -> Also works by ID (channel/role auto-detected)

Birthday variables:
`{age}` -> Shows the user's new age. Only applies when birth year is set.
`{year}` -> Shows birth year (birthday) or years count (member/server anniversary)."""

        variables_es = """Variables del bot:
`{user}` -> @John
`{username}` -> John
`{avatar}` -> Muestra el avatar del usuario
`{server}` -> Nombre de servidor
`{channel}` -> Soporta nombre de canal o ID de canal. Ejemplo: `{reglas}` -> #reglas
`{role}` -> Helper de rol. Usa `{role:miembro}` o `{role:123456789012345678}`
`{NombreRol}` -> Tambien funciona por nombre de rol. Ejemplo: `{miembro}` -> @Miembro
`{123456789012345678}` -> Tambien funciona por ID (deteccion automatica canal/rol)

Variables de cumpleaños:
`{age}` -> Muestra la nueva edad del usuario. Solo aplica si se define el año de nacimiento.
`{year}` -> Muestra el año de nacimiento (cumpleaños) o los años transcurridos (aniversarios)."""

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
            "4. Birthday",
            "5. Coding",
            "6. Sports",
            "7. Fun",
        ]
        section_items_es = [
            "1. Ayuda basica",
            "2. Indice de secciones",
            "3. General / IA",
            "4. Cumpleaños",
            "5. Programacion",
            "6. Deportes",
            "7. Diversion",
        ]
        if is_mod:
            section_items_en.append("8. Moderation")
            section_items_es.append("8. Moderacion")
        if is_admin:
            section_items_en.extend(["9. Admin", "10. Welcome/Goodbye", "11. Variables"])
            section_items_es.extend(["9. Admin", "10. Welcome/Goodbye", "11. Variables"])
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

        page4 = discord.Embed(title=tr(lang, "Birthday", "Cumpleaños"), color=discord.Color.purple())
        page4.add_field(name=tr(lang, "User Commands", "Comandos de usuario"), value=tr(lang, birthday_en, birthday_es), inline=False)
        if is_admin:
            page4.add_field(
                name=tr(lang, "Admin Commands", "Comandos de admin"),
                value=tr(lang, birthday_admin_en, birthday_admin_es),
                inline=False,
            )

        page5 = discord.Embed(title=tr(lang, "Coding", "Programacion"), color=discord.Color.dark_teal())
        page5.add_field(name=tr(lang, "Code Execution", "Ejecucion de codigo"), value=tr(lang, coding_en, coding_es), inline=False)

        page6 = discord.Embed(title=tr(lang, "Sports", "Deportes"), color=discord.Color.gold())
        page6.add_field(name=tr(lang, "Football", "Football"), value=tr(lang, sports_en, sports_es), inline=False)

        page7 = discord.Embed(title=tr(lang, "Fun", "Diversion"), color=discord.Color.teal())
        page7.add_field(name=tr(lang, "Meme + Ninja APIs", "Memes + APIs Ninja"), value=tr(lang, fun_en, fun_es), inline=False)

        pages = [page1, page2, page3, page4, page5, page6, page7]
        if is_mod:
            page_mod = discord.Embed(title=tr(lang, "Moderation Commands", "Comandos de Moderacion"), color=discord.Color.orange())
            page_mod.add_field(name=tr(lang, "Messages + Warnings", "Mensajes + Advertencias"), value=tr(lang, mod_a_en, mod_a_es), inline=False)
            page_mod.add_field(name=tr(lang, "Member Actions", "Acciones de miembros"), value=tr(lang, mod_b_en, mod_b_es), inline=False)
            pages.append(page_mod)

        if is_admin:
            page_admin = discord.Embed(title=tr(lang, "Admin Commands", "Comandos de Admin"), color=discord.Color.red())
            page_admin.add_field(name=tr(lang, "Server Configuration", "Configuración del servidor"), value=tr(lang, admin_en, admin_es), inline=False)
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
            prefix = tr(lang, "Page", "P\u00e1gina")
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
    @reset_server_context.error
    @view_server_context.error
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
        logging.exception("Admin command failed", exc_info=error)
        await ctx.send("Command failed due to an internal error. Please try again.")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
