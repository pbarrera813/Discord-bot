from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.database import BIRTHDAY_EVENT_TYPES
from utils.i18n import tr


EVENT_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="birthday", value="birthday"),
    app_commands.Choice(name="join", value="member_anniversary"),
    app_commands.Choice(name="server", value="server_anniversary"),
]

EVENT_CONFIG_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="default", value="birthday_default"),
    app_commands.Choice(name="year", value="birthday_year"),
    app_commands.Choice(name="join", value="member_anniversary"),
    app_commands.Choice(name="server", value="server_anniversary"),
    app_commands.Choice(name="disable", value="disable"),
]

EVENT_PREVIEW_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="default", value="default"),
    app_commands.Choice(name="year", value="year"),
    app_commands.Choice(name="server", value="server"),
    app_commands.Choice(name="user", value="user"),
]

DATE_PATTERN = re.compile(r"^\s*(\d{1,4})\s*[-/]\s*(\d{1,2})(?:\s*[-/]\s*(\d{1,4}))?\s*$")
TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")

DEFAULT_TEMPLATES: dict[str, str] = {
    "birthday": "Happy birthday {user}! Enjoy your day in {server}.",
    "member_anniversary": "Happy anniversary {user}! You have been here for {year} year(s) in {server}.",
    "server_anniversary": "{server} turns {year} year(s) today!",
}

DEFAULT_BIRTHDAY_MESSAGE_NO_YEAR = "Happy birthday {user}!"
DEFAULT_BIRTHDAY_MESSAGE_WITH_AGE = "Happy birthday {user} that today is celebrating their {age} birthday."


class BirthdaysCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.birthday_worker.start()

    def cog_unload(self) -> None:
        self.birthday_worker.cancel()

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _send_user_only(
        self,
        ctx: commands.Context,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        if ctx.interaction is not None:
            if not ctx.interaction.response.is_done():
                await ctx.interaction.response.send_message(
                    content=content,
                    embed=embed,
                    ephemeral=True,
                )
            else:
                await ctx.interaction.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=True,
                )
            return
        await ctx.send(content=content, embed=embed)

    async def _is_module_enabled(self, guild: discord.Guild | None) -> bool:
        if guild is None:
            return False
        settings = await self.bot.db.get_or_create_birthday_guild_settings(guild.id)
        return int(settings.get("enabled", 0)) == 1

    @staticmethod
    def _event_label(event_type: str, lang: str) -> str:
        labels = {
            "birthday": tr(lang, "Birthday", "Cumpleaños"),
            "member_anniversary": tr(lang, "Member Anniversary", "Aniversario de miembro"),
            "server_anniversary": tr(lang, "Server Anniversary", "Aniversario del servidor"),
        }
        return labels.get(event_type, event_type)

    @staticmethod
    def _validate_timezone(raw: str | None) -> str | None:
        if raw is None:
            return None
        value = raw.strip()
        if not value:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return None
        return value

    @staticmethod
    def _normalize_message_mode(raw: str | None) -> str | None:
        if raw is None:
            return None
        value = raw.strip().lower()
        if value in {"", "clear", "reset", "none", "-"}:
            return "embed"
        if value in {"text", "embed", "both"}:
            return value
        return None

    @staticmethod
    def _normalize_hex_color(raw: str | None) -> str | None:
        if raw is None:
            return None
        value = raw.strip()
        if value.lower() in {"", "clear", "reset", "none", "-"}:
            return ""
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
            return None
        if not value.startswith("#"):
            value = f"#{value}"
        return value.lower()

    @staticmethod
    def _normalize_optional_text(raw: str | None) -> str | None:
        if raw is None:
            return None
        value = raw.strip()
        if value.lower() in {"", "clear", "reset", "none", "-"}:
            return ""
        return value

    @staticmethod
    def _parse_date_input(raw: str) -> tuple[int, int] | None:
        match = DATE_PATTERN.match(raw)
        if not match:
            return None
        a = int(match.group(1))
        b = int(match.group(2))
        c_raw = match.group(3)
        if c_raw is not None:
            c = int(c_raw)
            if a > 31:
                month = b
                day = c
            elif c > 31:
                month = a
                day = b
            else:
                month = a
                day = b
        else:
            if a > 12 and b <= 12:
                day = a
                month = b
            else:
                month = a
                day = b
        try:
            datetime(2000, month, day)
        except ValueError:
            return None
        return month, day

    @staticmethod
    def _next_occurrence_key(month: int, day: int, now_local: datetime) -> tuple[int, int]:
        this_year = now_local.year
        try:
            this_date = datetime(this_year, month, day, tzinfo=now_local.tzinfo)
        except ValueError:
            this_date = datetime(this_year, 3, 1, tzinfo=now_local.tzinfo)
        if this_date.date() < now_local.date():
            year = this_year + 1
        else:
            year = this_year
        return year, int(datetime(year, month, day).strftime("%j")) if not (month == 2 and day == 29) else 60

    @staticmethod
    def _resolve_ping(
        *,
        guild: discord.Guild,
        member: discord.Member | None,
        ping_setting: str,
    ) -> str:
        token = (ping_setting or "none").strip()
        low = token.casefold()
        if low in {"", "none", "off"}:
            return ""
        if low == "everyone":
            return "@everyone"
        if low == "here":
            return "@here"
        if "{user}" in low and member is not None:
            return member.mention
        role_mention_match = re.fullmatch(r"<@&(\d+)>", token)
        if role_mention_match:
            role = guild.get_role(int(role_mention_match.group(1)))
            if role is not None:
                return role.mention
        if token.isdigit():
            role = guild.get_role(int(token))
            if role is not None:
                return role.mention
        role = discord.utils.find(lambda r: r.name.casefold() == token.casefold(), guild.roles)
        if role is not None:
            return role.mention
        return ""

    @staticmethod
    def _render_template(
        template: str,
        *,
        guild: discord.Guild,
        member: discord.Member | None,
        age: int | None,
        year_value: int | None,
    ) -> str:
        def match_named_role(token: str) -> discord.Role | None:
            probe = token.strip().lstrip("@")
            if not probe:
                return None
            if probe.isdigit():
                by_id = guild.get_role(int(probe))
                if by_id is not None:
                    return by_id
            lowered = probe.casefold()
            roles = [role for role in guild.roles if role.name != "@everyone"]
            exact = next((role for role in roles if role.name.casefold() == lowered), None)
            if exact is not None:
                return exact
            starts_with = next((role for role in roles if role.name.casefold().startswith(lowered)), None)
            if starts_with is not None:
                return starts_with
            return next((role for role in roles if lowered in role.name.casefold()), None)

        def replace(match: re.Match[str]) -> str:
            raw_token = match.group(1).strip()
            token = raw_token.casefold()
            if token == "user":
                return member.mention if member is not None else ""
            if token == "username":
                if member is None:
                    return ""
                return member.display_name
            if token == "server":
                return guild.name
            if token == "age":
                return str(age) if isinstance(age, int) and age > 0 else ""
            if token == "year":
                return str(year_value) if isinstance(year_value, int) and year_value > 0 else ""
            if token.startswith("role:"):
                query = raw_token.split(":", 1)[1].strip()
                if query:
                    role = match_named_role(query)
                    if role is not None:
                        return role.mention
                return match.group(0)
            role = match_named_role(raw_token)
            if role is not None:
                return role.mention
            return match.group(0)

        rendered = TOKEN_PATTERN.sub(replace, template).strip()
        return rendered or template

    async def _send_event_announcement(
        self,
        *,
        guild: discord.Guild,
        event_type: str,
        channel: discord.TextChannel,
        event_settings: dict,
        member: discord.Member | None,
        age: int | None,
        year_value: int | None,
        lang: str,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> None:
        templates = await self.bot.db.list_birthday_templates(guild.id, event_type)
        template = DEFAULT_TEMPLATES[event_type]

        if event_type == "birthday":
            no_year_cfg = str(event_settings.get("birthday_message_no_year", "") or "").strip()
            with_age_cfg = str(event_settings.get("birthday_message_with_age", "") or "").strip()
            if isinstance(age, int) and age > 0:
                template = with_age_cfg or no_year_cfg or tr(
                    lang,
                    DEFAULT_BIRTHDAY_MESSAGE_WITH_AGE,
                    "¡Feliz cumpleaños {user}! Hoy estás celebrando tus {age}.",
                )
            else:
                template = no_year_cfg or tr(
                    lang,
                    DEFAULT_BIRTHDAY_MESSAGE_NO_YEAR,
                    "¡Feliz cumpleaños {user}!",
                )

        no_year_cfg = str(event_settings.get("birthday_message_no_year", "") or "").strip()
        with_age_cfg = str(event_settings.get("birthday_message_with_age", "") or "").strip()
        if event_type != "birthday" and no_year_cfg:
            template = no_year_cfg
        use_template_pool = not no_year_cfg and (event_type != "birthday" or not with_age_cfg)
        if use_template_pool:
            enabled_templates = [item for item in templates if int(item.get("enabled", 0)) == 1]
            if enabled_templates:
                chosen = random.choice(enabled_templates)
                candidate = str(chosen.get("template_text", "")).strip()
                if candidate:
                    template = candidate

        text = self._render_template(
            template,
            guild=guild,
            member=member,
            age=age,
            year_value=year_value,
        )
        ping_prefix = self._resolve_ping(
            guild=guild,
            member=member,
            ping_setting=str(event_settings.get("ping_setting", "none")),
        )
        message_mode = str(event_settings.get("message_mode", "embed") or "embed").strip().lower()
        if message_mode not in {"text", "embed", "both"}:
            message_mode = "embed"

        custom_title = str(event_settings.get("embed_title", "") or "").strip()
        custom_color = str(event_settings.get("embed_color", "") or "").strip()
        custom_image_url = str(event_settings.get("embed_image_url", "") or "").strip()

        colors = {
            "birthday": discord.Color.fuchsia(),
            "member_anniversary": discord.Color.gold(),
            "server_anniversary": discord.Color.blue(),
        }
        embed_color = colors.get(event_type, discord.Color.blurple())
        if custom_color:
            try:
                embed_color = discord.Color(int(custom_color.lstrip("#"), 16))
            except (ValueError, TypeError):
                embed_color = colors.get(event_type, discord.Color.blurple())

        embed = discord.Embed(
            title=custom_title or self._event_label(event_type, lang),
            description=text,
            color=embed_color,
            timestamp=datetime.now(timezone.utc),
        )
        if member is not None:
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        if custom_image_url:
            embed.set_image(url=custom_image_url)

        content_text = text
        if ping_prefix:
            content_text = f"{ping_prefix}\n{text}" if text else ping_prefix

        mentions = allowed_mentions or discord.AllowedMentions(users=True, roles=True, everyone=True)

        if message_mode == "text":
            await channel.send(
                content=content_text,
                allowed_mentions=mentions,
            )
            return

        if message_mode == "both":
            await channel.send(
                content=content_text,
                embed=embed,
                allowed_mentions=mentions,
            )
            return

        await channel.send(
            content=ping_prefix if ping_prefix else None,
            embed=embed,
            allowed_mentions=mentions,
        )

    async def _member_is_blacklisted(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        blacklisted_users: set[int],
        blacklisted_roles: set[int],
    ) -> bool:
        if member.id in blacklisted_users:
            return True
        member_role_ids = {role.id for role in member.roles}
        return any(role_id in member_role_ids for role_id in blacklisted_roles)

    async def _is_trusted_allowed(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        settings: dict,
        check_kind: str,
    ) -> bool:
        trusted_role_id = settings.get("trusted_role_id")
        if not isinstance(trusted_role_id, int):
            return True
        trusted_role = guild.get_role(trusted_role_id)
        if trusted_role is None:
            return True
        has_trusted = trusted_role in member.roles
        if has_trusted:
            return True
        if check_kind == "message":
            return int(settings.get("trusted_prevent_message", 0)) == 0
        if check_kind == "role":
            return int(settings.get("trusted_prevent_role", 0)) == 0
        if check_kind == "list":
            return int(settings.get("trusted_prevent_list", 0)) == 0
        return True

    async def _run_dispatch_cycle(self) -> None:
        now_utc = datetime.now(timezone.utc)
        bot_now = datetime.now().astimezone()
        bot_tz = bot_now.tzinfo or timezone.utc
        for guild in self.bot.guilds:
            lang = await self._lang(guild)
            settings = await self.bot.db.get_or_create_birthday_guild_settings(guild.id)
            if int(settings.get("enabled", 0)) != 1:
                continue
            channel_id = settings.get("channel_id")
            if not isinstance(channel_id, int):
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            event_settings = {
                event: await self.bot.db.get_or_create_birthday_event_settings(guild.id, event)
                for event in BIRTHDAY_EVENT_TYPES
            }
            blacklisted_users = set(await self.bot.db.list_birthday_blacklist_users(guild.id))
            blacklisted_roles = set(await self.bot.db.list_birthday_blacklist_roles(guild.id))

            birthday_role = None
            role_id = settings.get("role_id")
            if isinstance(role_id, int):
                birthday_role = guild.get_role(role_id)

            active_birthday_members: set[int] = set()
            profiles = await self.bot.db.list_birthday_profiles(guild.id)
            for profile in profiles:
                user_id = int(profile["user_id"])
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    continue
                if await self._member_is_blacklisted(
                    guild=guild,
                    member=member,
                    blacklisted_users=blacklisted_users,
                    blacklisted_roles=blacklisted_roles,
                ):
                    continue

                month = int(profile["month"])
                day = int(profile["day"])
                if bot_now.month == month and bot_now.day == day:
                    active_birthday_members.add(member.id)

                birthday_cfg = event_settings["birthday"]
                if int(birthday_cfg.get("enabled", 0)) != 1:
                    continue
                if bot_now.month != month or bot_now.day != day or bot_now.hour != 0:
                    continue
                event_date = bot_now.date().isoformat()
                if await self.bot.db.was_birthday_event_dispatched(guild.id, "birthday", member.id, event_date):
                    continue
                if not await self._is_trusted_allowed(guild=guild, member=member, settings=settings, check_kind="message"):
                    continue

                birth_year = profile.get("birth_year")
                birth_year_int = int(birth_year) if isinstance(birth_year, int) else None
                age = None
                if birth_year_int and int(settings.get("disable_ages", 0)) != 1:
                    diff = bot_now.year - birth_year_int
                    if diff > 0:
                        age = diff

                await self._send_event_announcement(
                    guild=guild,
                    event_type="birthday",
                    channel=channel,
                    event_settings=birthday_cfg,
                    member=member,
                    age=age,
                    year_value=birth_year_int if int(settings.get("disable_ages", 0)) != 1 else None,
                    lang=lang,
                )
                await self.bot.db.mark_birthday_event_dispatched(guild.id, "birthday", member.id, event_date)

            if birthday_role is not None and guild.me is not None:
                for member in guild.members:
                    if member.bot:
                        continue
                    should_have = member.id in active_birthday_members and await self._is_trusted_allowed(
                        guild=guild,
                        member=member,
                        settings=settings,
                        check_kind="role",
                    )
                    has_role = birthday_role in member.roles
                    if should_have and not has_role:
                        try:
                            await member.add_roles(birthday_role, reason="Birthday role automation")
                        except (discord.Forbidden, discord.HTTPException):
                            continue
                    elif not should_have and has_role:
                        try:
                            await member.remove_roles(birthday_role, reason="Birthday role automation")
                        except (discord.Forbidden, discord.HTTPException):
                            continue

            member_cfg = event_settings["member_anniversary"]
            if int(member_cfg.get("enabled", 0)) == 1 and bot_now.hour == 0:
                for member in guild.members:
                    if member.bot or member.joined_at is None:
                        continue
                    if await self._member_is_blacklisted(
                        guild=guild,
                        member=member,
                        blacklisted_users=blacklisted_users,
                        blacklisted_roles=blacklisted_roles,
                    ):
                        continue
                    if not await self._is_trusted_allowed(guild=guild, member=member, settings=settings, check_kind="message"):
                        continue
                    joined_local = member.joined_at.astimezone(bot_tz)
                    if joined_local.month != bot_now.month or joined_local.day != bot_now.day:
                        continue
                    years = bot_now.year - joined_local.year
                    if years <= 0:
                        continue
                    event_date = bot_now.date().isoformat()
                    if await self.bot.db.was_birthday_event_dispatched(guild.id, "member_anniversary", member.id, event_date):
                        continue
                    await self._send_event_announcement(
                        guild=guild,
                        event_type="member_anniversary",
                        channel=channel,
                        event_settings=member_cfg,
                        member=member,
                        age=None,
                        year_value=years,
                        lang=lang,
                    )
                    await self.bot.db.mark_birthday_event_dispatched(
                        guild.id, "member_anniversary", member.id, event_date
                    )

            server_cfg = event_settings["server_anniversary"]
            if int(server_cfg.get("enabled", 0)) == 1 and bot_now.hour == 0:
                created_local = guild.created_at.astimezone(bot_tz)
                if created_local.month == bot_now.month and created_local.day == bot_now.day:
                    years = bot_now.year - created_local.year
                    if years > 0:
                        event_date = bot_now.date().isoformat()
                        if not await self.bot.db.was_birthday_event_dispatched(
                            guild.id, "server_anniversary", None, event_date
                        ):
                            await self._send_event_announcement(
                                guild=guild,
                                event_type="server_anniversary",
                                channel=channel,
                                event_settings=server_cfg,
                                member=None,
                                age=None,
                                year_value=years,
                                lang=lang,
                            )
                            await self.bot.db.mark_birthday_event_dispatched(
                                guild.id, "server_anniversary", None, event_date
                            )

    @tasks.loop(minutes=5)
    async def birthday_worker(self) -> None:
        try:
            await self._run_dispatch_cycle()
        except Exception:
            return

    @birthday_worker.before_loop
    async def _before_birthday_worker(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(
        name="birthday",
        description="Birthday and anniversary system commands.",
        invoke_without_command=True,
    )
    async def birthday(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        settings = await self.bot.db.get_or_create_birthday_guild_settings(ctx.guild.id)
        channel = ctx.guild.get_channel(int(settings["channel_id"])) if settings.get("channel_id") else None
        role = ctx.guild.get_role(int(settings["role_id"])) if settings.get("role_id") else None
        embed = discord.Embed(
            title=tr(lang, "Birthday Module", "Módulo de cumpleaños"),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name=tr(lang, "Enabled", "Activo"), value=str(bool(int(settings.get("enabled", 0)))), inline=True)
        embed.add_field(name=tr(lang, "Channel", "Canal"), value=channel.mention if isinstance(channel, discord.TextChannel) else tr(lang, "Not set", "Sin configurar"), inline=True)
        embed.add_field(name=tr(lang, "Role", "Rol"), value=role.mention if isinstance(role, discord.Role) else tr(lang, "Not set", "Sin configurar"), inline=True)
        embed.add_field(name=tr(lang, "Server timezone", "Zona horaria del servidor"), value=str(settings.get("server_timezone", "UTC")), inline=True)
        embed.add_field(name=tr(lang, "Birthday mode", "Modo de cumpleaños"), value=str(settings.get("birthday_timezone_mode", "user")), inline=True)
        embed.add_field(name=tr(lang, "Disable ages", "Ocultar edades"), value=str(bool(int(settings.get("disable_ages", 0)))), inline=True)
        await ctx.send(embed=embed)

    @birthday.command(name="setup", description="Configure birthday channel/role quickly.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None
    ) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        settings = await self.bot.db.update_birthday_guild_settings(
            ctx.guild.id,
            enabled=True,
            channel_id=channel.id if channel is not None else Ellipsis,
            role_id=role.id if role is not None else Ellipsis,
        )
        out_channel = ctx.guild.get_channel(int(settings["channel_id"])) if settings.get("channel_id") else None
        out_role = ctx.guild.get_role(int(settings["role_id"])) if settings.get("role_id") else None
        embed = discord.Embed(
            title=tr(lang, "Birthday setup complete", "Configuración de cumpleaños completada"),
            color=discord.Color.green(),
        )
        embed.add_field(name=tr(lang, "Channel", "Canal"), value=out_channel.mention if isinstance(out_channel, discord.TextChannel) else tr(lang, "Not set", "Sin configurar"), inline=True)
        embed.add_field(name=tr(lang, "Role", "Rol"), value=out_role.mention if isinstance(out_role, discord.Role) else tr(lang, "Not set", "Sin configurar"), inline=True)
        embed.add_field(name=tr(lang, "Timezone", "Zona horaria"), value=str(settings.get("server_timezone", "UTC")), inline=True)
        await ctx.send(embed=embed)

    @birthday.command(name="set", description="Set your birthday date (MM-DD or DD/MM).")
    @app_commands.rename(date_text="date", birth_year="year")
    async def birthday_set(
        self,
        ctx: commands.Context,
        date_text: str,
        birth_year: int | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await self._send_user_only(
                ctx,
                content=tr(lang, "Use this command in a server.", "Usa este comando en un servidor."),
            )
            return
        if not await self._is_module_enabled(ctx.guild):
            await self._send_user_only(
                ctx,
                content=tr(
                    lang,
                    "Birthday module is disabled in this server.",
                    "El módulo de cumpleaños está desactivado en este servidor.",
                ),
            )
            return

        parsed = self._parse_date_input(date_text)
        if parsed is None:
            await self._send_user_only(
                ctx,
                content=tr(lang, "Invalid date. Use MM-DD or DD/MM.", "Fecha inválida. Usa MM-DD o DD/MM."),
            )
            return

        month, day = parsed
        if birth_year is not None and (birth_year < 1900 or birth_year > datetime.now(timezone.utc).year):
            await self._send_user_only(
                ctx,
                content=tr(lang, "Invalid birth year.", "Año de nacimiento inválido."),
            )
            return

        await self.bot.db.upsert_birthday_profile(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            month=month,
            day=day,
            birth_year=birth_year,
            timezone_name=None,
        )

        await self._send_user_only(
            ctx,
            content=tr(
                lang,
                f"Birthday saved: `{month:02d}-{day:02d}`",
                f"Cumpleaños guardado: `{month:02d}-{day:02d}`",
            ),
        )

    @birthday.command(name="remove", description="Remove your birthday from this server.")
    async def birthday_remove(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await self._send_user_only(
                ctx,
                content=tr(lang, "Use this command in a server.", "Usa este comando en un servidor."),
            )
            return
        if not await self._is_module_enabled(ctx.guild):
            await self._send_user_only(
                ctx,
                content=tr(
                    lang,
                    "Birthday module is disabled in this server.",
                    "El módulo de cumpleaños está desactivado en este servidor.",
                ),
            )
            return

        removed = await self.bot.db.delete_birthday_profile(ctx.guild.id, ctx.author.id)
        if removed:
            await self._send_user_only(
                ctx,
                content=tr(lang, "Your birthday data was removed.", "Tus datos de cumpleaños fueron eliminados."),
            )
        else:
            await self._send_user_only(
                ctx,
                content=tr(lang, "You had no birthday data stored.", "No ten\u00edas datos de cumpleaños guardados."),
            )

    @birthday.command(name="cleardata", description="Alias of /birthday remove.")
    async def birthday_clear_data(self, ctx: commands.Context) -> None:
        await self.birthday_remove(ctx)

    @birthday.command(name="view", description="View birthday info of a user.")
    async def birthday_view(self, ctx: commands.Context, user: discord.Member | None = None) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        if not await self._is_module_enabled(ctx.guild):
            await ctx.send(
                tr(
                    lang,
                    "Birthday module is disabled in this server.",
                    "El módulo de cumpleaños está desactivado en este servidor.",
                )
            )
            return
        target = user or (ctx.author if isinstance(ctx.author, discord.Member) else None)
        if target is None:
            await ctx.send(tr(lang, "User not found.", "Usuario no encontrado."))
            return
        profile = await self.bot.db.get_birthday_profile(ctx.guild.id, target.id)
        if profile is None:
            await ctx.send(
                tr(
                    lang,
                    f"No birthday set for {target.mention}.",
                    f"No hay cumpleaños configurado para {target.mention}.",
                )
            )
            return
        settings = await self.bot.db.get_or_create_birthday_guild_settings(ctx.guild.id)
        hide_ages = int(settings.get("disable_ages", 0)) == 1
        month = int(profile["month"])
        day = int(profile["day"])
        birth_year = profile.get("birth_year")
        embed = discord.Embed(
            title=tr(lang, "Birthday profile", "Perfil de cumpleaños"),
            color=discord.Color.blurple(),
        )
        embed.set_author(name=str(target), icon_url=target.display_avatar.url)
        embed.add_field(name=tr(lang, "Date", "Fecha"), value=f"`{month:02d}-{day:02d}`", inline=True)
        if not hide_ages and isinstance(birth_year, int):
            embed.add_field(name=tr(lang, "Birth year", "Año de nacimiento"), value=f"`{birth_year}`", inline=True)
        await ctx.send(embed=embed)

    @birthday.command(name="next", description="List next upcoming birthdays in this server.")
    async def birthday_next(self, ctx: commands.Context, count: int = 10) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        if not await self._is_module_enabled(ctx.guild):
            await ctx.send(
                tr(
                    lang,
                    "Birthday module is disabled in this server.",
                    "El módulo de cumpleaños está desactivado en este servidor.",
                )
            )
            return
        count = max(1, min(count, 25))
        settings = await self.bot.db.get_or_create_birthday_guild_settings(ctx.guild.id)
        tz_name = str(settings.get("server_timezone", "UTC") or "UTC")
        try:
            server_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            server_tz = ZoneInfo("UTC")
        now_local = datetime.now(timezone.utc).astimezone(server_tz)
        blacklisted_users = set(await self.bot.db.list_birthday_blacklist_users(ctx.guild.id))
        blacklisted_roles = set(await self.bot.db.list_birthday_blacklist_roles(ctx.guild.id))
        rows: list[tuple[tuple[int, int], str]] = []
        for profile in await self.bot.db.list_birthday_profiles(ctx.guild.id):
            member = ctx.guild.get_member(int(profile["user_id"]))
            if member is None or member.bot:
                continue
            if await self._member_is_blacklisted(
                guild=ctx.guild,
                member=member,
                blacklisted_users=blacklisted_users,
                blacklisted_roles=blacklisted_roles,
            ):
                continue
            if not await self._is_trusted_allowed(guild=ctx.guild, member=member, settings=settings, check_kind="list"):
                continue
            month = int(profile["month"])
            day = int(profile["day"])
            key = self._next_occurrence_key(month, day, now_local)
            rows.append((key, f"`{month:02d}-{day:02d}` - {member.mention}"))
        if not rows:
            await ctx.send(tr(lang, "No birthdays configured yet.", "Aún no hay cumpleaños configurados."))
            return
        rows.sort(key=lambda item: item[0])
        lines = [item[1] for item in rows[:count]]
        embed = discord.Embed(
            title=tr(lang, "Upcoming birthdays", "Próximos cumpleaños"),
            description="\n".join(lines),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.send(embed=embed)

    @birthday.command(name="channel", description="Set or clear the birthday announcement channel.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        await self.bot.db.update_birthday_guild_settings(
            ctx.guild.id,
            channel_id=channel.id if channel is not None else None,
        )
        await ctx.send(
            tr(
                lang,
                f"Birthday channel updated: {channel.mention if channel else 'cleared'}",
                f"Canal de cumpleaños actualizado: {channel.mention if channel else 'limpiado'}",
            )
        )

    @birthday.command(name="role", description="Set or clear the birthday role.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_role(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        await self.bot.db.update_birthday_guild_settings(
            ctx.guild.id,
            role_id=role.id if role is not None else None,
        )
        await ctx.send(
            tr(
                lang,
                f"Birthday role updated: {role.mention if role else 'cleared'}",
                f"Rol de cumpleaños actualizado: {role.mention if role else 'limpiado'}",
            )
        )

    @birthday.command(name="timezone", description="Set server timezone for anniversary/birthday server mode.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(timezone_name="timezone")
    async def birthday_timezone(self, ctx: commands.Context, timezone_name: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        tz = self._validate_timezone(timezone_name)
        if tz is None:
            await ctx.send(tr(lang, "Invalid timezone. Use IANA format.", "Zona horaria inválida. Usa formato IANA."))
            return
        await self.bot.db.update_birthday_guild_settings(ctx.guild.id, server_timezone=tz)
        await ctx.send(tr(lang, f"Server timezone set to `{tz}`.", f"Zona horaria del servidor configurada a `{tz}`."))

    @birthday.command(name="mode", description="Set birthday mode: user or server timezone.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_mode(self, ctx: commands.Context, mode: str) -> None:
        lang = await self._lang(ctx.guild)
        normalized = mode.strip().lower()
        if normalized not in {"user", "server"}:
            await ctx.send(tr(lang, "Mode must be `user` or `server`.", "El modo debe ser `user` o `server`."))
            return
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        await self.bot.db.update_birthday_guild_settings(ctx.guild.id, birthday_timezone_mode=normalized)
        await ctx.send(tr(lang, f"Birthday mode set to `{normalized}`.", f"Modo de cumpleaños configurado a `{normalized}`."))

    @birthday.command(name="ages", description="Enable or disable age visibility in birthday messages.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_ages(self, ctx: commands.Context, enabled: bool) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        await self.bot.db.update_birthday_guild_settings(ctx.guild.id, disable_ages=not enabled)
        await ctx.send(
            tr(
                lang,
                f"Age visibility is now {'enabled' if enabled else 'disabled'}.",
                f"La visibilidad de edad ahora esta {'activada' if enabled else 'desactivada'}.",
            )
        )

    @birthday.command(name="event", description="Configure birthday/anniversary event settings.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(event_type="type")
    @app_commands.choices(event_type=EVENT_CONFIG_CHOICES)
    async def birthday_event(
        self,
        ctx: commands.Context,
        event_type: str,
        color: str | None = None,
        image: str | None = None,
        *,
        message: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return

        normalized_event = (event_type or "").strip().lower()
        target_event_type = ""
        birthday_variant = "default"
        if normalized_event in {"birthday_year", "year"}:
            target_event_type = "birthday"
            birthday_variant = "year"
        elif normalized_event in {"birthday_default", "default", "birthday"}:
            target_event_type = "birthday"
            birthday_variant = "default"
        elif normalized_event in {"member_anniversary", "join"}:
            target_event_type = "member_anniversary"
        elif normalized_event in {"server_anniversary", "server"}:
            target_event_type = "server_anniversary"

        if normalized_event == "disable":
            await self.bot.db.update_birthday_guild_settings(ctx.guild.id, enabled=False)
            await ctx.send(
                tr(
                    lang,
                    "Birthday module disabled.",
                    "Módulo de cumpleaños desactivado.",
                )
            )
            return
        if not target_event_type:
            await ctx.send(tr(lang, "Invalid event type.", "Tipo de evento invalido."))
            return

        normalized_color = self._normalize_hex_color(color)
        if color is not None and normalized_color is None:
            await ctx.send(
                tr(
                    lang,
                    "color must be a valid hex like `#00ffaa`, or `clear`.",
                    "color debe ser un hex válido como `#00ffaa`, o `clear`.",
                )
            )
            return

        normalized_image = self._normalize_optional_text(image)
        normalized_message = self._normalize_optional_text(message)

        try:
            update_kwargs: dict[str, object] = {
                "enabled": True,
                "message_mode": "embed",
                "embed_color": normalized_color,
                "embed_image_url": normalized_image,
            }
            if normalized_message is not None:
                if target_event_type == "birthday":
                    if birthday_variant == "year":
                        update_kwargs["birthday_message_with_age"] = normalized_message
                    else:
                        update_kwargs["birthday_message_no_year"] = normalized_message
                else:
                    update_kwargs["birthday_message_no_year"] = normalized_message

            cfg = await self.bot.db.update_birthday_event_settings(
                ctx.guild.id,
                target_event_type,
                **update_kwargs,
            )
        except ValueError:
            await ctx.send(tr(lang, "Invalid event type.", "Tipo de evento invalido."))
            return

        embed = discord.Embed(
            title=tr(lang, "Event settings updated", "Configuración de evento actualizada"),
            color=discord.Color.green(),
        )
        embed.add_field(name=tr(lang, "Event", "Evento"), value=self._event_label(target_event_type, lang), inline=True)
        if target_event_type == "birthday":
            embed.add_field(name=tr(lang, "Variant", "Variante"), value=f"`{birthday_variant}`", inline=True)
        embed.add_field(name=tr(lang, "Enabled", "Activo"), value=str(bool(int(cfg.get("enabled", 0)))), inline=True)
        embed.add_field(
            name=tr(lang, "Schedule", "Horario"),
            value=tr(
                lang,
                "12:00 AM (bot local time)",
                "12:00 AM (hora local del bot)",
            ),
            inline=True,
        )
        embed.add_field(name=tr(lang, "Ping", "Mencion"), value=f"`{cfg.get('ping_setting', 'none')}`", inline=False)
        embed.add_field(name=tr(lang, "Message mode", "Modo de mensaje"), value=f"`{cfg.get('message_mode', 'embed')}`", inline=True)
        embed.add_field(name=tr(lang, "Embed title", "Titulo del embed"), value=f"`{cfg.get('embed_title', '') or '-'}`", inline=True)
        embed.add_field(name=tr(lang, "Embed color", "Color del embed"), value=f"`{cfg.get('embed_color', '') or '-'}`", inline=True)
        embed.add_field(name=tr(lang, "Embed image", "Imagen del embed"), value=f"`{cfg.get('embed_image_url', '') or '-'}`", inline=False)
        if target_event_type == "birthday":
            no_year_value = str(cfg.get("birthday_message_no_year", "") or "").strip() or "-"
            with_age_value = str(cfg.get("birthday_message_with_age", "") or "").strip() or "-"
            embed.add_field(
                name=tr(lang, "Birthday message (no year)", "Mensaje de cumpleaños (sin año)"),
                value=no_year_value[:1024],
                inline=False,
            )
            embed.add_field(
                name=tr(lang, "Birthday message (with age)", "Mensaje de cumpleaños (con edad)"),
                value=with_age_value[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @birthday.command(name="preview", description="Preview birthday/anniversary event output.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(preview_type="type")
    @app_commands.choices(preview_type=EVENT_PREVIEW_CHOICES)
    async def birthday_preview(self, ctx: commands.Context, preview_type: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return

        target_channel: discord.TextChannel | None = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        if target_channel is None:
            settings = await self.bot.db.get_or_create_birthday_guild_settings(ctx.guild.id)
            config_channel_id = settings.get("channel_id")
            if isinstance(config_channel_id, int):
                maybe_channel = ctx.guild.get_channel(config_channel_id)
                if isinstance(maybe_channel, discord.TextChannel):
                    target_channel = maybe_channel
        if target_channel is None:
            await ctx.send(
                tr(
                    lang,
                    "No valid text channel available for preview.",
                    "No hay un canal de texto valido para la vista previa.",
                )
            )
            return

        normalized = (preview_type or "").strip().lower()
        target_event_type = ""
        preview_age: int | None = None
        preview_year_value: int | None = None
        preview_member = ctx.author if isinstance(ctx.author, discord.Member) else None

        if normalized in {"default", "birthday", "birthday_default"}:
            target_event_type = "birthday"
        elif normalized in {"year", "birthday_year"}:
            target_event_type = "birthday"
            now_year = datetime.now(timezone.utc).year
            preview_age = 24
            preview_year_value = now_year - preview_age
            if preview_member is not None:
                profile = await self.bot.db.get_birthday_profile(ctx.guild.id, preview_member.id)
                if profile is not None:
                    birth_year = profile.get("birth_year")
                    if isinstance(birth_year, int):
                        computed_age = now_year - birth_year
                        if computed_age > 0:
                            preview_age = computed_age
                            preview_year_value = birth_year
        elif normalized in {"server", "server_anniversary"}:
            target_event_type = "server_anniversary"
            created_year = ctx.guild.created_at.year
            preview_year_value = max(1, datetime.now(timezone.utc).year - created_year)
        elif normalized in {"user", "join", "member_anniversary"}:
            target_event_type = "member_anniversary"
            if preview_member is not None and preview_member.joined_at is not None:
                preview_year_value = max(1, datetime.now(timezone.utc).year - preview_member.joined_at.year)
            else:
                preview_year_value = 1

        if not target_event_type:
            await ctx.send(tr(lang, "Invalid preview type.", "Tipo de vista previa invalido."))
            return

        event_settings = await self.bot.db.get_or_create_birthday_event_settings(ctx.guild.id, target_event_type)
        await self._send_event_announcement(
            guild=ctx.guild,
            event_type=target_event_type,
            channel=target_channel,
            event_settings=event_settings,
            member=preview_member,
            age=preview_age,
            year_value=preview_year_value,
            lang=lang,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @birthday.command(name="templateadd", description="Add a custom template for an event type (max 100).")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(event_type="type", template_text="template")
    @app_commands.choices(event_type=EVENT_CHOICES)
    async def birthday_template_add(self, ctx: commands.Context, event_type: str, *, template_text: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        clean = template_text.strip()
        if not clean:
            await ctx.send(tr(lang, "Template text cannot be empty.", "La plantilla no puede estar vacia."))
            return
        try:
            current = await self.bot.db.count_birthday_templates(ctx.guild.id, event_type)
        except ValueError:
            await ctx.send(tr(lang, "Invalid event type.", "Tipo de evento invalido."))
            return
        if current >= 100:
            await ctx.send(tr(lang, "Template limit reached (100).", "Limite de plantillas alcanzado (100)."))
            return
        template_id = await self.bot.db.add_birthday_template(ctx.guild.id, event_type, clean)
        await ctx.send(
            tr(
                lang,
                f"Template added (ID `{template_id}`) for `{event_type}`.",
                f"Plantilla agregada (ID `{template_id}`) para `{event_type}`.",
            )
        )

    @birthday.command(name="templatelist", description="List custom templates for an event type.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(event_type="type")
    @app_commands.choices(event_type=EVENT_CHOICES)
    async def birthday_template_list(self, ctx: commands.Context, event_type: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        try:
            templates = await self.bot.db.list_birthday_templates(ctx.guild.id, event_type)
        except ValueError:
            await ctx.send(tr(lang, "Invalid event type.", "Tipo de evento invalido."))
            return
        if not templates:
            await ctx.send(tr(lang, "No templates configured for this event.", "No hay plantillas configuradas para este evento."))
            return
        lines = []
        for item in templates[:20]:
            template_id = int(item["id"])
            text = str(item["template_text"]).strip().replace("\n", " ")
            lines.append(f"`{template_id}` - {text[:120]}")
        embed = discord.Embed(
            title=tr(lang, f"Templates: {event_type}", f"Plantillas: {event_type}"),
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @birthday.command(name="templateremove", description="Remove template by template ID.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(event_type="type", template_id="id")
    @app_commands.choices(event_type=EVENT_CHOICES)
    async def birthday_template_remove(self, ctx: commands.Context, event_type: str, template_id: int) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        try:
            removed = await self.bot.db.delete_birthday_template(ctx.guild.id, event_type, template_id)
        except ValueError:
            await ctx.send(tr(lang, "Invalid event type.", "Tipo de evento invalido."))
            return
        if removed:
            await ctx.send(tr(lang, "Template removed.", "Plantilla eliminada."))
        else:
            await ctx.send(tr(lang, "Template not found.", "Plantilla no encontrada."))

    @birthday.command(name="blacklistuser", description="Add or remove a user from birthday celebrations.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_blacklist_user(self, ctx: commands.Context, user: discord.Member, blocked: bool) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        if blocked:
            await self.bot.db.add_birthday_blacklist_user(ctx.guild.id, user.id)
            await ctx.send(tr(lang, f"{user.mention} added to birthday blacklist.", f"{user.mention} agregado a la blacklist de cumpleaños."))
        else:
            await self.bot.db.remove_birthday_blacklist_user(ctx.guild.id, user.id)
            await ctx.send(tr(lang, f"{user.mention} removed from birthday blacklist.", f"{user.mention} eliminado de la blacklist de cumpleaños."))

    @birthday.command(name="blacklistrole", description="Add or remove a role from birthday celebrations.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def birthday_blacklist_role(self, ctx: commands.Context, role: discord.Role, blocked: bool) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        if blocked:
            await self.bot.db.add_birthday_blacklist_role(ctx.guild.id, role.id)
            await ctx.send(tr(lang, f"{role.mention} added to birthday blacklist.", f"{role.mention} agregado a la blacklist de cumpleaños."))
        else:
            await self.bot.db.remove_birthday_blacklist_role(ctx.guild.id, role.id)
            await ctx.send(tr(lang, f"{role.mention} removed from birthday blacklist.", f"{role.mention} eliminado de la blacklist de cumpleaños."))

    @birthday.command(name="trusted", description="Set trusted role behavior for birthday system.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @app_commands.rename(
        prevent_message="blockmessage",
        prevent_role="blockrole",
        prevent_list="blocklist",
    )
    async def birthday_trusted(
        self,
        ctx: commands.Context,
        role: discord.Role | None = None,
        prevent_message: bool = False,
        prevent_role: bool = False,
        prevent_list: bool = False,
    ) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await ctx.send(tr(lang, "Use this command in a server.", "Usa este comando en un servidor."))
            return
        updated = await self.bot.db.update_birthday_guild_settings(
            ctx.guild.id,
            trusted_role_id=role.id if role is not None else None,
            trusted_prevent_message=prevent_message,
            trusted_prevent_role=prevent_role,
            trusted_prevent_list=prevent_list,
        )
        trusted_role = ctx.guild.get_role(int(updated["trusted_role_id"])) if updated.get("trusted_role_id") else None
        embed = discord.Embed(
            title=tr(lang, "Trusted role settings", "Configuración de rol confiable"),
            color=discord.Color.blurple(),
        )
        embed.add_field(name=tr(lang, "Role", "Rol"), value=trusted_role.mention if trusted_role else tr(lang, "None", "Ninguno"), inline=False)
        embed.add_field(name=tr(lang, "Prevent message", "Bloquear mensaje"), value=str(bool(int(updated.get("trusted_prevent_message", 0)))), inline=True)
        embed.add_field(name=tr(lang, "Prevent role", "Bloquear rol"), value=str(bool(int(updated.get("trusted_prevent_role", 0)))), inline=True)
        embed.add_field(name=tr(lang, "Prevent list", "Bloquear lista"), value=str(bool(int(updated.get("trusted_prevent_list", 0)))), inline=True)
        await ctx.send(embed=embed)

    @birthday.error
    @birthday_setup.error
    @birthday_set.error
    @birthday_remove.error
    @birthday_view.error
    @birthday_next.error
    @birthday_channel.error
    @birthday_role.error
    @birthday_timezone.error
    @birthday_mode.error
    @birthday_ages.error
    @birthday_event.error
    @birthday_preview.error
    @birthday_template_add.error
    @birthday_template_list.error
    @birthday_template_remove.error
    @birthday_blacklist_user.error
    @birthday_blacklist_role.error
    @birthday_trusted.error
    async def birthday_error_handler(self, ctx: commands.Context, error: commands.CommandError) -> None:
        lang = await self._lang(ctx.guild)
        original = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                tr(
                    lang,
                    "You do not have permission for this birthday command.",
                    "No tienes permisos para este comando de cumpleaños.",
                )
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(tr(lang, "Invalid argument provided.", "Se proporciono un argumento invalido."))
            return
        await ctx.send(
            tr(
                lang,
                f"Birthday command failed: {original}",
                f"El comando de cumpleaños falló: {original}",
            )
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BirthdaysCog(bot))
