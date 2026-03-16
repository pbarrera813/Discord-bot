from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, time as dt_time, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.i18n import tr


class ReminderOwnerChoiceView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        reminder_id: int,
        owner_user_id: int,
        reminder_message: str,
        lang: str,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.reminder_id = reminder_id
        self.owner_user_id = owner_user_id
        self.reminder_message = reminder_message
        self.lang = lang
        self.message: discord.Message | None = None

        self.yes_button = discord.ui.Button(
            label=tr(lang, "Yes", "Si"),
            style=discord.ButtonStyle.success,
            row=0,
        )
        self.no_button = discord.ui.Button(
            label=tr(lang, "No", "No"),
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.join_button = discord.ui.Button(
            label=tr(lang, "Me too", "A mi tambien"),
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.yes_button.callback = self._on_yes
        self.no_button.callback = self._on_no
        self.join_button.callback = self._on_join
        self.add_item(self.yes_button)
        self.add_item(self.no_button)
        self.add_item(self.join_button)

    async def _set_choice(self, interaction: discord.Interaction, mention_in_channel: bool) -> None:
        await self.bot.db.set_reminder_mention(
            reminder_id=self.reminder_id,
            user_id=self.owner_user_id,
            mention_in_channel=mention_in_channel,
        )
        self.yes_button.disabled = True
        self.no_button.disabled = True
        await interaction.response.edit_message(view=self)

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != self.owner_user_id:
            await interaction.response.send_message(
                tr(
                    self.lang,
                    "Only the user who created this reminder can choose Yes/No.",
                    "Solo el usuario que creo este recordatorio puede elegir Si/No.",
                ),
                ephemeral=True,
            )
            return
        await self._set_choice(interaction, True)

    async def _on_no(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != self.owner_user_id:
            await interaction.response.send_message(
                tr(
                    self.lang,
                    "Only the user who created this reminder can choose Yes/No.",
                    "Solo el usuario que creo este recordatorio puede elegir Si/No.",
                ),
                ephemeral=True,
            )
            return
        await self._set_choice(interaction, False)

    async def _on_join(self, interaction: discord.Interaction) -> None:
        if interaction.user is None:
            return

        if interaction.user.id == self.owner_user_id:
            await interaction.response.send_message(
                tr(
                    self.lang,
                    "You already created this reminder. Use the Yes/No buttons above.",
                    "Tu ya creaste este recordatorio. Usa los botones Si/No de arriba.",
                ),
                ephemeral=True,
            )
            return

        exists = await self.bot.db.reminder_exists(self.reminder_id)
        if not exists:
            await interaction.response.send_message(
                tr(
                    self.lang,
                    "That reminder is no longer active.",
                    "Ese recordatorio ya no esta activo.",
                ),
                ephemeral=True,
            )
            return

        added = await self.bot.db.add_reminder_subscriber(
            reminder_id=self.reminder_id,
            user_id=interaction.user.id,
            mention_in_channel=True,
        )
        if not added:
            await interaction.response.send_message(
                tr(
                    self.lang,
                    "You are already subscribed to this reminder.",
                    "Ya estas suscrito a este recordatorio.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr(
                self.lang,
                f"Ok, I will remind you too about \"{self.reminder_message}\" {interaction.user.mention}",
                f"Ok, te recordare tambien sobre \"{self.reminder_message}\" {interaction.user.mention}",
            ),
            allowed_mentions=discord.AllowedMentions(users=True),
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


class FunCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminder_worker.start()

    def cog_unload(self) -> None:
        self.reminder_worker.cancel()

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _parse_reminder_input(raw: str) -> tuple[int, str] | None:
        match = re.fullmatch(r"\s*(\d+)\s*(mo|m|h|d|w|y)\s*", raw.lower())
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2)
        if value <= 0:
            return None
        return value, unit

    @staticmethod
    def _add_months(base: datetime, months: int) -> datetime:
        total_months = (base.year * 12 + (base.month - 1)) + months
        target_year = total_months // 12
        target_month = (total_months % 12) + 1
        max_day = calendar.monthrange(target_year, target_month)[1]
        target_day = min(base.day, max_day)
        return base.replace(year=target_year, month=target_month, day=target_day)

    def _compute_reminder_timestamp(
        self,
        *,
        value: int,
        unit: str,
    ) -> tuple[int, bool]:
        now_ts = int(time.time())
        now_local = datetime.now().astimezone()

        if unit == "m":
            return now_ts + (value * 60), True
        if unit == "h":
            return now_ts + (value * 3600), True

        if unit == "d":
            target = now_local + timedelta(days=value)
        elif unit == "w":
            target = now_local + timedelta(weeks=value)
        elif unit == "mo":
            target = self._add_months(now_local, value)
        else:  # unit == "y"
            target = self._add_months(now_local, value * 12)

        reminder_local = datetime.combine(
            target.date(),
            dt_time(hour=0, minute=0),
            tzinfo=now_local.tzinfo,
        )
        if reminder_local <= now_local:
            reminder_local += timedelta(days=1)
        return int(reminder_local.timestamp()), False

    @tasks.loop(seconds=30)
    async def reminder_worker(self) -> None:
        due_reminders = await self.bot.db.get_due_reminders(int(time.time()))
        if not due_reminders:
            return

        for reminder in due_reminders:
            reminder_id = int(reminder["id"])
            guild_id = int(reminder["guild_id"])
            owner_user_id = int(reminder["user_id"])
            channel_id = int(reminder["channel_id"])
            reminder_message = str(reminder["message"])
            owner_mention_in_channel = bool(reminder["mention_in_channel"])

            guild = self.bot.get_guild(guild_id)
            lang = "en"
            if guild is not None:
                settings = await self.bot.db.get_guild_settings(guild_id)
                lang = settings.language_code

            recipients: dict[int, bool] = {owner_user_id: owner_mention_in_channel}
            subscribers = await self.bot.db.get_reminder_subscribers(reminder_id)
            for sub in subscribers:
                sub_user_id = int(sub["user_id"])
                sub_mention = bool(sub.get("mention_in_channel", 1))
                recipients[sub_user_id] = recipients.get(sub_user_id, False) or sub_mention

            dm_text = tr(
                lang,
                f"Reminder: {reminder_message}",
                f"Recordatorio: {reminder_message}",
            )
            for recipient_id in recipients.keys():
                user = self.bot.get_user(recipient_id)
                if user is None:
                    try:
                        user = await self.bot.fetch_user(recipient_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        user = None
                if user is None:
                    continue
                try:
                    await user.send(dm_text)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            mention_ids = [uid for uid, mention in recipients.items() if mention]
            if mention_ids:
                channel = self.bot.get_channel(channel_id)
                if channel and hasattr(channel, "send"):
                    mentions = " ".join(f"<@{uid}>" for uid in mention_ids)
                    try:
                        await channel.send(
                            tr(
                                lang,
                                f"{mentions} reminder: {reminder_message}",
                                f"{mentions} recordatorio: {reminder_message}",
                            ),
                            allowed_mentions=discord.AllowedMentions(
                                users=True,
                                roles=False,
                                everyone=False,
                            ),
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            await self.bot.db.delete_reminder(reminder_id)

    @reminder_worker.before_loop
    async def before_reminder_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_say_as_bot(self, ctx: commands.Context, text: str) -> bool:
        channel = getattr(ctx, "channel", None)
        if channel is None:
            return False
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @commands.hybrid_command(
        name="say",
        description="Make the bot repeat a message.",
    )
    @commands.has_permissions(manage_messages=True)
    async def say(self, ctx: commands.Context, *, message: str) -> None:
        text = message.strip()
        if not text:
            await ctx.send("Message cannot be empty.")
            return

        if ctx.interaction is None and ctx.message:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            await self._send_say_as_bot(ctx, text)
            return

        try:
            if not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

        sent = await self._send_say_as_bot(ctx, text)
        try:
            if sent:
                await ctx.interaction.followup.send("Sent.", ephemeral=True)
            else:
                await ctx.interaction.followup.send(
                    "Failed to send the message in this channel.",
                    ephemeral=True,
                )
        except (discord.NotFound, discord.HTTPException):
            pass

    @commands.hybrid_command(
        name="remindme",
        description="Set a reminder with m, h, d, w, mo or y.",
    )
    async def remindme(self, ctx: commands.Context, duration: str, *, reminder_message: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server text channel.",
                    "Este comando solo se puede usar en un canal de texto del servidor.",
                )
            )
            return

        parsed = self._parse_reminder_input(duration)
        if parsed is None:
            await ctx.send(
                tr(
                    lang,
                    "Invalid duration. Use `m`, `h`, `d`, `w`, `mo`, or `y` (example: `10m`, `2h`, `7d`, `3mo`).",
                    "Duracion invalida. Usa `m`, `h`, `d`, `w`, `mo` o `y` (ejemplo: `10m`, `2h`, `7d`, `3mo`).",
                )
            )
            return

        message_text = reminder_message.strip()
        if not message_text:
            await ctx.send(
                tr(
                    lang,
                    "Reminder message cannot be empty.",
                    "El mensaje del recordatorio no puede estar vacio.",
                )
            )
            return

        value, unit = parsed
        remind_at_ts, exact_time = self._compute_reminder_timestamp(value=value, unit=unit)
        when_full = f"<t:{remind_at_ts}:F>"
        when_relative = f"<t:{remind_at_ts}:R>"

        reminder_id = await self.bot.db.create_reminder(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            channel_id=ctx.channel.id,
            message=message_text,
            remind_at=remind_at_ts,
        )

        if exact_time:
            confirm_text = tr(
                lang,
                f'Done! I will remind you "{message_text}" on {when_full} ({when_relative}).',
                f'De acuerdo! Te recordare que "{message_text}" el {when_full} ({when_relative}).',
            )
        else:
            confirm_text = tr(
                lang,
                f'Done! I will remind you "{message_text}" on {when_full} ({when_relative}).',
                f'De acuerdo! Te recordare que "{message_text}" el {when_full} ({when_relative}).',
            )

        question_text = tr(
            lang,
            "Do you want me to also @ you in this chat?",
            "Quieres que te recuerde igualmente etiquetandote en este canal?",
        )

        owner_view = ReminderOwnerChoiceView(
            bot=self.bot,
            reminder_id=reminder_id,
            owner_user_id=ctx.author.id,
            reminder_message=message_text,
            lang=lang,
        )
        owner_msg = await ctx.send(f"{confirm_text}\n{question_text}", view=owner_view)
        if isinstance(owner_msg, discord.Message):
            owner_view.message = owner_msg

    async def unremindme_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild_id
        user = interaction.user
        if guild_id is None or user is None:
            return []

        rows = await self.bot.db.list_active_reminders_for_user(guild_id, user.id, limit=25)
        current_cf = (current or "").casefold().strip()

        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            reminder_id = int(row["reminder_id"])
            message = str(row["message"])
            if current_cf and current_cf not in message.casefold():
                continue
            when = datetime.fromtimestamp(int(row["remind_at"])).strftime("%Y-%m-%d")
            label = f"{message} ({when})"
            if len(label) > 100:
                label = label[:97] + "..."
            choices.append(app_commands.Choice(name=label, value=f"id:{reminder_id}"))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.describe(reminder="Reminder from your list")
    @app_commands.autocomplete(reminder=unremindme_autocomplete)
    @commands.hybrid_command(
        name="unremindme",
        description="Remove one of your active reminders.",
    )
    async def unremindme(self, ctx: commands.Context, *, reminder: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        active = await self.bot.db.list_active_reminders_for_user(ctx.guild.id, ctx.author.id, limit=100)
        if not active:
            await ctx.send(
                tr(
                    lang,
                    "You have no active reminders.",
                    "No tienes recordatorios activos.",
                )
            )
            return

        reminder_input = reminder.strip()
        reminder_id: int | None = None

        if reminder_input.lower().startswith("id:"):
            maybe_id = reminder_input[3:].strip()
            if maybe_id.isdigit():
                reminder_id = int(maybe_id)

        if reminder_id is None:
            matches = [
                row for row in active if str(row["message"]).casefold() == reminder_input.casefold()
            ]
            if matches:
                reminder_id = int(matches[0]["reminder_id"])

        if reminder_id is None:
            await ctx.send(
                tr(
                    lang,
                    "Reminder not found in your active list.",
                    "No se encontro ese recordatorio en tu lista activa.",
                )
            )
            return

        result = await self.bot.db.remove_reminder_for_user(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            reminder_id=reminder_id,
        )
        if result is None:
            await ctx.send(
                tr(
                    lang,
                    "Reminder not found in your active list.",
                    "No se encontro ese recordatorio en tu lista activa.",
                )
            )
            return

        if result == "owner_deleted":
            await ctx.send(
                tr(
                    lang,
                    "Reminder removed.",
                    "Recordatorio eliminado.",
                )
            )
        else:
            await ctx.send(
                tr(
                    lang,
                    "You were removed from that reminder.",
                    "Se elimino tu suscripcion a ese recordatorio.",
                )
            )

    @say.error
    @remindme.error
    @unremindme.error
    async def say_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        lang = await self._lang(ctx.guild)
        if isinstance(error, commands.MissingPermissions):
            msg = tr(
                lang,
                "You do not have permission to use this command.",
                "No tienes permiso para usar este comando.",
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            msg = tr(
                lang,
                f"Missing argument: `{error.param.name}`.",
                f"Falta el argumento: `{error.param.name}`.",
            )
        else:
            msg = tr(
                lang,
                f"Command failed: {error}",
                f"El comando fallo: {error}",
            )

        interaction = getattr(ctx, "interaction", None)
        if interaction is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        await ctx.send(msg)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog(bot))
