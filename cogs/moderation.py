from __future__ import annotations

import logging
import colorsys
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from io import BytesIO

import discord
from discord.ext import commands, tasks

from services.modlog import send_modlog_embed
from utils.discord_helpers import (
    WARNING_ROLE_NAMES,
    ensure_muted_role,
    ensure_warning_roles,
    parse_user_id_from_text,
)
from utils.duration import DurationParseError, parse_duration
from utils.i18n import tr
from utils.permissions import owner_or_has_permissions

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageDraw = None
    ImageFont = None

logger = logging.getLogger(__name__)

COLOR_NUMBER_EMOJIS = ["\u0031\ufe0f\u20e3", "\u0032\ufe0f\u20e3", "\u0033\ufe0f\u20e3", "\u0034\ufe0f\u20e3", "\u0035\ufe0f\u20e3", "\u0036\ufe0f\u20e3", "\u0037\ufe0f\u20e3", "\u0038\ufe0f\u20e3", "\u0039\ufe0f\u20e3", "\U0001F51F"]
MAX_COLOR_ROLES = len(COLOR_NUMBER_EMOJIS)
DEFAULT_COLOR_ROLES: list[tuple[str, str]] = [
    ("Red", "#FF0000"),
    ("Blue", "#0000FF"),
    ("Green", "#00FF7F"),
    ("Yellow", "#F1C40F"),
    ("Orange", "#FA8F02"),
    ("White", "#FFFFFF"),
    ("Black", "#000000"),
    ("Pink", "#FF5DD6"),
    ("Magenta", "#FF00FF"),
]


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.temp_action_worker.start()

    def cog_unload(self) -> None:
        self.temp_action_worker.cancel()

    @tasks.loop(seconds=30)
    async def temp_action_worker(self) -> None:
        now_ts = int(time.time())
        due_actions = await self.bot.db.get_due_temp_actions(now_ts)
        if not due_actions:
            return

        for action in due_actions:
            action_id = int(action["id"])
            guild = self.bot.get_guild(int(action["guild_id"]))
            user_id = int(action["user_id"])
            action_type = str(action["action"])

            try:
                if guild is None:
                    await self.bot.db.delete_temp_action(action_id)
                    continue

                if action_type == "tempmute":
                    await self._expire_tempmute(guild, user_id)
                elif action_type == "tempban":
                    await self._expire_tempban(guild, user_id)

                await self.bot.db.delete_temp_action(action_id)
            except Exception:
                # Keep action in DB if expiration failed unexpectedly.
                logger.exception(
                    "Failed to process temp action id=%s guild_id=%s user_id=%s action=%s",
                    action_id,
                    action.get("guild_id"),
                    action.get("user_id"),
                    action_type,
                )
                continue

    @temp_action_worker.before_loop
    async def before_temp_action_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _maybe_defer(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            return
        if ctx.interaction.response.is_done():
            return
        try:
            await ctx.defer()
        except (discord.NotFound, discord.HTTPException):
            return

    @staticmethod
    def _repair_mojibake_text(text: str) -> str:
        if not text:
            return text
        replacements = {
            "\u00C3\u00A1": "\u00E1",
            "\u00C3\u00A9": "\u00E9",
            "\u00C3\u00AD": "\u00ED",
            "\u00C3\u00B3": "\u00F3",
            "\u00C3\u00BA": "\u00FA",
            "\u00C3\u0081": "\u00C1",
            "\u00C3\u0089": "\u00C9",
            "\u00C3\u008D": "\u00CD",
            "\u00C3\u0093": "\u00D3",
            "\u00C3\u009A": "\u00DA",
            "\u00C3\u00B1": "\u00F1",
            "\u00C3\u0091": "\u00D1",
            "\u00C3\u00BC": "\u00FC",
            "\u00C3\u009C": "\u00DC",
            "\u00C2\u00BF": "\u00BF",
            "\u00C2\u00A1": "\u00A1",
            "\u00E2\u0080\u0099": "\u2019",
            "\u00E2\u0080\u009C": "\u201C",
            "\u00E2\u0080\u009D": "\u201D",
            "\u00E2\u0080\u0093": "\u2013",
            "\u00E2\u0080\u0094": "\u2014",
            "\u00E2\u0080\u00A6": "\u2026",
        }
        fixed = text
        for wrong, correct in replacements.items():
            fixed = fixed.replace(wrong, correct)
        return fixed.replace("\u00C2", "")

    @classmethod
    def _repair_embed_mojibake(cls, embed: discord.Embed) -> discord.Embed:
        if embed.title:
            embed.title = cls._repair_mojibake_text(embed.title)
        if embed.description:
            embed.description = cls._repair_mojibake_text(embed.description)
        for index, field in enumerate(tuple(embed.fields)):
            embed.set_field_at(
                index,
                name=cls._repair_mojibake_text(field.name),
                value=cls._repair_mojibake_text(field.value),
                inline=field.inline,
            )
        return embed

    async def _safe_send(self, ctx: commands.Context, *args, **kwargs):
        args_list = list(args)
        if args_list and isinstance(args_list[0], str):
            args_list[0] = self._repair_mojibake_text(args_list[0])
        if isinstance(kwargs.get("content"), str):
            kwargs["content"] = self._repair_mojibake_text(kwargs["content"])
        if isinstance(kwargs.get("embed"), discord.Embed):
            kwargs["embed"] = self._repair_embed_mojibake(kwargs["embed"])
        embeds = kwargs.get("embeds")
        if isinstance(embeds, list):
            kwargs["embeds"] = [
                self._repair_embed_mojibake(item) if isinstance(item, discord.Embed) else item
                for item in embeds
            ]
        args = tuple(args_list)

        try:
            return await ctx.send(*args, **kwargs)
        except (discord.NotFound, discord.HTTPException):
            channel = getattr(ctx, "channel", None)
            if channel is None:
                return None
            kwargs.pop("ephemeral", None)
            try:
                return await channel.send(*args, **kwargs)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

    async def _send_success(
        self,
        ctx: commands.Context,
        message: str,
        *,
        delete_after: float | None = None,
    ) -> None:
        embed = discord.Embed(
            description=f"\u2705 {message}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await self._safe_send(ctx, embed=embed, delete_after=delete_after)

    async def _expire_tempmute(self, guild: discord.Guild, user_id: int) -> None:
        settings = await self.bot.db.get_guild_settings(guild.id)
        role: discord.Role | None = None

        if settings.muted_role_id:
            role = guild.get_role(settings.muted_role_id)
        if role is None:
            role = discord.utils.get(guild.roles, name="Muted")
        if role is None:
            return

        member = guild.get_member(user_id)
        if member is None:
            return

        if role in member.roles:
            await member.remove_roles(role, reason="Temporary mute expired")

        log_embed = discord.Embed(
            title="Temporary Mute Expired",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(name="User", value=member.mention, inline=True)
        log_embed.add_field(name="Action", value="Auto-unmute", inline=True)
        await send_modlog_embed(guild, self.bot.db, log_embed)

    async def _expire_tempban(self, guild: discord.Guild, user_id: int) -> None:
        try:
            await guild.unban(
                discord.Object(id=user_id),
                reason="Temporary ban expired",
            )
        except discord.NotFound:
            pass

        log_embed = discord.Embed(
            title="Temporary Ban Expired",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(name="User ID", value=str(user_id), inline=True)
        log_embed.add_field(name="Action", value="Auto-unban", inline=True)
        await send_modlog_embed(guild, self.bot.db, log_embed)

    def _can_moderate(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        lang: str,
    ) -> tuple[bool, str]:
        if actor.id == target.id:
            return False, tr(
                lang,
                "You cannot moderate yourself.",
                "No puedes moderarte a ti mismo.",
            )
        if target.id == guild.owner_id:
            return False, tr(
                lang,
                "You cannot moderate the server owner.",
                "No puedes moderar al propietario del servidor.",
            )
        if (
            actor.id != guild.owner_id
            and not self.bot.is_owner_user(actor)
            and actor.top_role <= target.top_role
        ):
            return False, tr(
                lang,
                "Your highest role must be above the target's highest role.",
                "Tu rol más alto debe estar por encima del rol más alto del objetivo.",
            )

        me = guild.me
        if me is None:
            return False, tr(
                lang,
                "Bot member not found in this guild.",
                "No se encontró al bot como miembro en este servidor.",
            )
        if me.top_role <= target.top_role:
            return False, tr(
                lang,
                "My highest role must be above the target's highest role.",
                "Mi rol más alto debe estar por encima del rol más alto del objetivo.",
            )
        return True, ""

    @staticmethod
    def _member_name_variants(member: discord.Member) -> list[str]:
        variants: list[str] = []
        if member.name:
            variants.append(member.name)
        if member.display_name:
            variants.append(member.display_name)
        global_name = getattr(member, "global_name", None)
        if isinstance(global_name, str) and global_name:
            variants.append(global_name)
        discriminator = getattr(member, "discriminator", None)
        if isinstance(discriminator, str) and discriminator not in {"", "0"}:
            variants.append(f"{member.name}#{discriminator}")
        return variants

    async def _resolve_target_member(
        self,
        guild: discord.Guild,
        user_query: str,
        lang: str,
    ) -> tuple[discord.Member | None, str | None]:
        query = user_query.strip()
        if not query:
            return None, tr(
                lang,
                "Provide a user mention, ID, username, or display name.",
                "Proporciona una mención, ID, nombre de usuario o nombre visible.",
            )

        user_id = parse_user_id_from_text(query)
        if user_id is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member, None
            try:
                member = await guild.fetch_member(user_id)
                return member, None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None, tr(
                    lang,
                    "I couldn't find a server member with that ID.",
                    "No pude encontrar un miembro del servidor con esa ID.",
                )

        query_cf = query.casefold()

        def matches_exact(member: discord.Member) -> bool:
            return any(name.casefold() == query_cf for name in self._member_name_variants(member))

        def matches_prefix(member: discord.Member) -> bool:
            return any(name.casefold().startswith(query_cf) for name in self._member_name_variants(member))

        exact_cached = [m for m in guild.members if matches_exact(m)]
        if len(exact_cached) == 1:
            return exact_cached[0], None
        if len(exact_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Más de un miembro coincide con ese nombre. Usa @mención o ID de usuario.",
            )

        prefix_cached = [m for m in guild.members if matches_prefix(m)]
        if len(prefix_cached) == 1:
            return prefix_cached[0], None
        if len(prefix_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Más de un miembro coincide con ese nombre. Usa @mención o ID de usuario.",
            )

        try:
            queried = await guild.query_members(query=query, limit=25)
        except (discord.Forbidden, discord.HTTPException):
            queried = []

        exact_queried = [m for m in queried if matches_exact(m)]
        if len(exact_queried) == 1:
            return exact_queried[0], None
        if len(exact_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Más de un miembro coincide con ese nombre. Usa @mención o ID de usuario.",
            )

        prefix_queried = [m for m in queried if matches_prefix(m)]
        if len(prefix_queried) == 1:
            return prefix_queried[0], None
        if len(prefix_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Más de un miembro coincide con ese nombre. Usa @mención o ID de usuario.",
            )

        return None, tr(
            lang,
            "User not found in this server. Use @mention, user ID, username, or display name.",
            "No encontré al usuario en este servidor. Usa @mención, ID, nombre de usuario o nombre visible.",
        )

    @staticmethod
    def _parse_channel_id_from_text(text: str) -> int | None:
        cleaned = text.strip()
        if not cleaned:
            return None
        mention_match = re.fullmatch(r"<#(\d{15,22})>", cleaned)
        if mention_match:
            return int(mention_match.group(1))
        if cleaned.isdigit():
            return int(cleaned)
        return None

    async def _resolve_target_channel(
        self,
        guild: discord.Guild,
        channel_query: str,
        lang: str,
    ) -> tuple[discord.TextChannel | None, str | None]:
        query = channel_query.strip()
        if not query:
            return None, tr(
                lang,
                "Provide a channel mention, channel ID, or channel name.",
                "Proporciona una mención de canal, ID de canal o nombre de canal.",
            )

        channel_id = self._parse_channel_id_from_text(query)
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel, None
            return None, tr(
                lang,
                "I couldn't find a text channel with that ID.",
                "No pude encontrar un canal de texto con esa ID.",
            )

        normalized_query = query.lstrip("#").strip().casefold()
        if not normalized_query:
            return None, tr(
                lang,
                "Provide a valid channel name.",
                "Proporciona un nombre de canal válido.",
            )

        exact = [ch for ch in guild.text_channels if ch.name.casefold() == normalized_query]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, tr(
                lang,
                "More than one text channel matches that name. Use #mention or channel ID.",
                "Más de un canal de texto coincide con ese nombre. Usa #mención o ID del canal.",
            )

        starts_with = [ch for ch in guild.text_channels if ch.name.casefold().startswith(normalized_query)]
        if len(starts_with) == 1:
            return starts_with[0], None
        if len(starts_with) > 1:
            return None, tr(
                lang,
                "More than one text channel matches that query. Use #mention or channel ID.",
                "Más de un canal de texto coincide con esa búsqueda. Usa #mención o ID del canal.",
            )

        return None, tr(
            lang,
            "Text channel not found. Use #mention, ID, or full channel name.",
            "Canal de texto no encontrado. Usa #mención, ID o el nombre completo del canal.",
        )

    async def _resolve_channel_or_current(
        self,
        ctx: commands.Context,
        channel_query: discord.TextChannel | str | None,
        lang: str,
    ) -> tuple[discord.TextChannel | None, str | None]:
        if isinstance(channel_query, discord.TextChannel):
            return channel_query, None
        if isinstance(channel_query, str) and channel_query.strip():
            return await self._resolve_target_channel(ctx.guild, channel_query, lang)  # type: ignore[arg-type]
        if isinstance(ctx.channel, discord.TextChannel):
            return ctx.channel, None
        return None, tr(
            lang,
            "Use this command in a text channel or provide a target channel.",
            "Usa este comando en un canal de texto o proporciona un canal objetivo.",
        )

    @staticmethod
    def _parse_role_id_from_text(text: str) -> int | None:
        cleaned = text.strip()
        if not cleaned:
            return None
        mention_match = re.fullmatch(r"<@&(\d{15,22})>", cleaned)
        if mention_match:
            return int(mention_match.group(1))
        if cleaned.isdigit():
            return int(cleaned)
        return None

    async def _resolve_target_role(
        self,
        guild: discord.Guild,
        role_query: str,
        lang: str,
    ) -> tuple[discord.Role | None, str | None]:
        query = role_query.strip()
        if not query:
            return None, tr(
                lang,
                "Provide a role mention, role ID, or role name.",
                "Proporciona una mención de rol, ID de rol o nombre de rol.",
            )

        role_id = self._parse_role_id_from_text(query)
        if role_id is not None:
            role = guild.get_role(role_id)
            if role is not None:
                return role, None
            return None, tr(
                lang,
                "I couldn't find a role with that ID.",
                "No pude encontrar un rol con esa ID.",
            )

        normalized = query.lstrip("@").strip().casefold()
        if not normalized:
            return None, tr(
                lang,
                "Provide a valid role name.",
                "Proporciona un nombre de rol válido.",
            )

        exact = [role for role in guild.roles if role.name.casefold() == normalized]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, tr(
                lang,
                "More than one role matches that name. Use @role mention or role ID.",
                "Más de un rol coincide con ese nombre. Usa @rol o ID del rol.",
            )

        starts_with = [role for role in guild.roles if role.name.casefold().startswith(normalized)]
        if len(starts_with) == 1:
            return starts_with[0], None
        if len(starts_with) > 1:
            return None, tr(
                lang,
                "More than one role matches that query. Use @role mention or role ID.",
                "Más de un rol coincide con esa búsqueda. Usa @rol o ID del rol.",
            )

        return None, tr(
            lang,
            "Role not found. Use @role mention, role ID, or full role name.",
            "Rol no encontrado. Usa @rol, ID del rol o el nombre completo del rol.",
        )

    def _can_manage_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
        lang: str,
    ) -> tuple[bool, str]:
        if role.is_default():
            return False, tr(
                lang,
                "You cannot manage the @everyone role.",
                "No puedes gestionar el rol @everyone.",
            )
        if role.managed:
            return False, tr(
                lang,
                "That role is managed by an integration and cannot be edited manually.",
                "Ese rol está gestionado por una integración y no se puede editar manualmente.",
            )

        me = guild.me
        if me is None:
            return False, tr(
                lang,
                "Bot member not found in this guild.",
                "No se encontró al bot como miembro en este servidor.",
            )
        if role >= me.top_role:
            return False, tr(
                lang,
                "I cannot manage that role due to role hierarchy.",
                "No puedo gestionar ese rol por la jerarquía de roles.",
            )
        if (
            actor.id != guild.owner_id
            and not self.bot.is_owner_user(actor)
            and role >= actor.top_role
        ):
            return False, tr(
                lang,
                "You cannot manage a role equal to or higher than your top role.",
                "No puedes gestionar un rol igual o superior a tu rol más alto.",
            )
        return True, ""

    async def _log_action(
        self,
        guild: discord.Guild,
        *,
        title: str,
        moderator: discord.abc.User,
        target: str,
        reason: str,
        details: str | None = None,
    ) -> None:
        ban_like_titles = {"User Banned", "User Temporarily Banned"}
        if title == "Channel Created":
            color = discord.Color.green()
        elif title == "Channel Deleted":
            color = discord.Color.red()
        elif title in ban_like_titles:
            color = discord.Color.red()
        else:
            color = discord.Color.blue()
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        no_reason_titles = {"Channel Created", "Channel Deleted", "User Info Lookup"}
        if title in {"Channel Created", "Channel Deleted"}:
            embed.add_field(name="Channel name", value=target, inline=True)
        elif title == "User Info Lookup":
            embed.add_field(name="Target", value=target, inline=True)
        else:
            embed.add_field(name="Target", value=target, inline=True)
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if details and title not in no_reason_titles:
            embed.add_field(name="Details", value=details[:1024], inline=False)
        await send_modlog_embed(guild, self.bot.db, embed)

    @staticmethod
    def _format_ts(value: datetime | None, lang: str) -> str:
        if value is None:
            return tr(lang, "Unknown", "Desconocido")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return f"<t:{int(value.timestamp())}:F>"

    @staticmethod
    def _normalize_hex_color(value: str) -> str | None:
        cleaned = value.strip()
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", cleaned)
        if not match:
            return None
        return f"#{match.group(1).upper()}"

    @staticmethod
    def _hex_to_rgb(hex_code: str) -> tuple[int, int, int] | None:
        match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", hex_code.strip())
        if not match:
            return None
        raw = match.group(1)
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)

    @staticmethod
    def _boost_preview_text_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
        r, g, b = rgb
        if r < 18 and g < 18 and b < 18:
            return (8, 8, 8)
        if r > 236 and g > 236 and b > 236:
            return (250, 250, 250)

        rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
        h, s, v = colorsys.rgb_to_hsv(rf, gf, bf)

        if s < 0.52:
            s = 0.52
        if v < 0.74:
            v = 0.74

        hue_deg = (h * 360.0) % 360.0
        if 200 <= hue_deg <= 310 and v < 0.88:
            v = 0.88
            s = max(s, 0.62)

        nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
        return int(nr * 255), int(ng * 255), int(nb * 255)

    @classmethod
    def _color_preview_emoji_from_hex(cls, hex_code: str) -> str:
        rgb = cls._hex_to_rgb(hex_code)
        if rgb is None:
            return "\U0001F3A8"

        r, g, b = rgb
        if r < 35 and g < 35 and b < 35:
            return "\u2B1B"
        if r > 235 and g > 235 and b > 235:
            return "\u2B1C"

        mx = max(r, g, b)
        mn = min(r, g, b)
        diff = mx - mn
        if diff < 12:
            return "\u2B1C" if mx >= 128 else "\u2B1B"

        if mx == r:
            hue = (60 * ((g - b) / diff) + 360) % 360
        elif mx == g:
            hue = (60 * ((b - r) / diff) + 120) % 360
        else:
            hue = (60 * ((r - g) / diff) + 240) % 360

        if hue < 15 or hue >= 345:
            return "\U0001F7E5"  # red
        if hue < 45:
            return "\U0001F7E7"  # orange
        if hue < 70:
            return "\U0001F7E8"  # yellow
        if hue < 165:
            return "\U0001F7E9"  # green
        if hue < 255:
            return "\U0001F7E6"  # blue
        return "\U0001F7EA"  # purple/pink/magenta

    async def _collect_valid_color_roles(
        self,
        guild: discord.Guild,
    ) -> list[tuple[discord.Role, dict]]:
        stored_roles = await self.bot.db.list_color_roles(guild.id)
        valid: list[tuple[discord.Role, dict]] = []
        for entry in stored_roles:
            role_id = int(entry["role_id"])
            role = guild.get_role(role_id)
            if role is None:
                await self.bot.db.delete_color_role_by_role_id(guild.id, role_id)
                continue
            valid.append((role, entry))
        return valid

    def _build_color_panel_image_file(
        self,
        role_entries: list[tuple[discord.Role, dict]],
        lang: str,
    ) -> discord.File | None:
        if Image is None or ImageDraw is None or ImageFont is None:
            return None

        entries = role_entries[:MAX_COLOR_ROLES]
        if not entries:
            return None

        columns = 2 if len(entries) > 4 else 1
        rows = (len(entries) + columns - 1) // columns
        card_width = 1080 if columns == 2 else 700
        row_height = 90
        gutter = 30
        pad = 24
        inner_pad = 42
        card_height = (inner_pad * 2) + (rows * row_height) + 4
        outer_w = card_width + (pad * 2)
        outer_h = card_height + (pad * 2)

        background = (30, 32, 36)
        card_bg = (46, 48, 54)
        image = Image.new("RGB", (outer_w, outer_h), background)
        draw = ImageDraw.Draw(image)

        card_box = (pad, pad, pad + card_width, pad + card_height)
        draw.rounded_rectangle(card_box, radius=20, fill=card_bg, outline=(64, 68, 76), width=2)

        row_font = None
        for candidate in (
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "arialbd.ttf",
            "Arial Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
        ):
            try:
                row_font = ImageFont.truetype(candidate, 62)
                break
            except Exception:
                continue
        if row_font is None:
            row_font = ImageFont.load_default()

        col_width = (card_width - (inner_pad * 2) - (gutter * (columns - 1))) // columns
        x0 = pad + inner_pad
        y0 = pad + inner_pad

        for idx, (_role, entry) in enumerate(entries):
            col = idx % columns if columns == 2 else 0
            row = idx // columns if columns == 2 else idx
            x = x0 + (col * (col_width + gutter))
            y = y0 + (row * row_height)

            color_name = str(entry["color_name"])
            hex_code = self._normalize_hex_color(str(entry["hex_code"])) or str(entry["hex_code"]).upper()
            base_rgb = self._hex_to_rgb(hex_code) or (130, 130, 130)
            rgb = self._boost_preview_text_rgb(base_rgb)
            if hex_code == "#000000":
                stroke_color = (236, 238, 244)
                stroke_width = 5
            elif hex_code == "#FFFFFF":
                stroke_color = (12, 13, 16)
                stroke_width = 5
            else:
                stroke_color = (15, 16, 19) if sum(rgb) > 430 else (238, 240, 246)
                stroke_width = 4

            # Soft shadow to make bright and dark colors readable on dark cards.
            draw.text(
                (x + 2, y + 2),
                f"{idx + 1}. {color_name}",
                fill=(8, 9, 11, 180),
                font=row_font,
            )
            draw.text(
                (x, y),
                f"{idx + 1}. {color_name}",
                fill=rgb,
                font=row_font,
                stroke_width=stroke_width,
                stroke_fill=stroke_color,
            )

        buffer = BytesIO()
        try:
            image.save(buffer, format="PNG")
            buffer.seek(0)
            return discord.File(fp=buffer, filename="color_panel_preview.png")
        except Exception:
            logger.exception("Failed to render color panel preview image.")
            return None

    async def _build_color_panel_embed(
        self,
        guild: discord.Guild,
        lang: str,
    ) -> tuple[discord.Embed, discord.File | None] | None:
        role_entries = await self._collect_valid_color_roles(guild)
        if not role_entries:
            return None

        lines: list[str] = []
        for index, (_role, entry) in enumerate(role_entries[:MAX_COLOR_ROLES]):
            color_name = str(entry["color_name"])
            hex_code = self._normalize_hex_color(str(entry["hex_code"])) or str(entry["hex_code"]).upper()
            swatch = self._color_preview_emoji_from_hex(hex_code)
            lines.append(f"{index + 1}. {swatch} **{color_name}** (`{hex_code}`)")

        image_file = self._build_color_panel_image_file(role_entries, lang)
        embed = discord.Embed(
            title=tr(lang, "Choose Your Name Color", "Elige tu color de nombre"),
            description=tr(
                lang,
                "Preview below. React with a number to select one color role.",
                "Vista previa abajo. Reacciona con un numero para elegir un solo color.",
            ),
            color=discord.Color.from_rgb(54, 57, 63),
            timestamp=datetime.now(timezone.utc),
        )
        if image_file is not None:
            embed.set_image(url="attachment://color_panel_preview.png")
        else:
            embed.description = "\n".join(lines)
        embed.set_footer(
            text=tr(
                lang,
                "React with a number. You can only have one color role at a time.",
                "Reacciona con un n\u00famero. Solo puedes tener un rol de color a la vez.",
            )
        )
        return embed, image_file

    async def _publish_color_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        lang: str,
    ) -> discord.Message | None:
        payload = await self._build_color_panel_embed(guild, lang)
        if payload is None:
            return None
        embed, image_file = payload

        panel_state = await self.bot.db.get_color_panel(guild.id)
        previous_message_id = None
        if panel_state is not None:
            previous_message_id = panel_state.get("message_id")
            previous_channel_id = panel_state.get("channel_id")
            if previous_message_id and previous_channel_id:
                old_channel = guild.get_channel(int(previous_channel_id))
                if isinstance(old_channel, discord.TextChannel):
                    try:
                        old_msg = await old_channel.fetch_message(int(previous_message_id))
                        await old_msg.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

        try:
            if image_file is not None:
                panel_message = await channel.send(embed=embed, file=image_file)
            else:
                panel_message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return None

        role_entries = await self._collect_valid_color_roles(guild)
        for index in range(min(len(role_entries), MAX_COLOR_ROLES)):
            try:
                await panel_message.add_reaction(COLOR_NUMBER_EMOJIS[index])
            except (discord.Forbidden, discord.HTTPException):
                break

        await self.bot.db.upsert_color_panel(guild.id, channel.id, panel_message.id)
        return panel_message

    async def _assign_color_role_from_reaction(
        self,
        guild: discord.Guild,
        member: discord.Member,
        emoji: str,
    ) -> bool:
        if member.bot:
            return False
        if emoji not in COLOR_NUMBER_EMOJIS:
            return False

        role_entries = await self._collect_valid_color_roles(guild)
        index = COLOR_NUMBER_EMOJIS.index(emoji)
        if index >= len(role_entries):
            return False

        target_role, _ = role_entries[index]
        me = guild.me
        if me is None or target_role >= me.top_role:
            return False

        color_role_ids = {int(entry["role_id"]) for _, entry in role_entries}
        to_remove = [role for role in member.roles if role.id in color_role_ids and role.id != target_role.id]

        try:
            if to_remove:
                await member.remove_roles(
                    *to_remove,
                    reason="Color role selection update",
                )
            if target_role not in member.roles:
                await member.add_roles(
                    target_role,
                    reason="Color role selected from panel",
                )
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def _prune_old_color_reactions(
        self,
        *,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
        member: discord.Member,
        selected_emoji: str,
    ) -> None:
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(payload.channel_id)
                channel = fetched
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            panel_message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        for reaction in panel_message.reactions:
            emoji_text = str(reaction.emoji)
            if emoji_text == selected_emoji:
                continue
            if emoji_text not in COLOR_NUMBER_EMOJIS:
                continue
            try:
                await reaction.remove(member)
            except (discord.Forbidden, discord.HTTPException):
                continue

    async def _remove_member_color_reaction(
        self,
        *,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
        member: discord.Member,
        emoji: str,
    ) -> None:
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(payload.channel_id)
                channel = fetched
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            panel_message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        for reaction in panel_message.reactions:
            if str(reaction.emoji) != emoji:
                continue
            try:
                await reaction.remove(member)
            except (discord.Forbidden, discord.HTTPException):
                return
            return

    async def _find_last_member_message(
        self,
        guild: discord.Guild,
        member_id: int,
    ) -> discord.Message | None:
        me = guild.me
        if me is None:
            return None

        channels: Iterable[discord.TextChannel] = sorted(
            guild.text_channels,
            key=lambda ch: (ch.position, ch.id),
            reverse=True,
        )
        for channel in channels:
            perms = channel.permissions_for(me)
            if not perms.read_message_history:
                continue
            try:
                async for msg in channel.history(limit=75):
                    if msg.author.id == member_id:
                        return msg
            except (discord.Forbidden, discord.HTTPException):
                continue
        return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        panel_state = await self.bot.db.get_color_panel(payload.guild_id)
        if panel_state is None:
            return
        message_id = panel_state.get("message_id")
        if message_id is None or int(message_id) != payload.message_id:
            return

        emoji = str(payload.emoji)
        if emoji not in COLOR_NUMBER_EMOJIS:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        member = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        assigned = await self._assign_color_role_from_reaction(guild, member, emoji)
        if not assigned:
            await self._remove_member_color_reaction(
                guild=guild,
                payload=payload,
                member=member,
                emoji=emoji,
            )
            return
        await self._prune_old_color_reactions(
            guild=guild,
            payload=payload,
            member=member,
            selected_emoji=emoji,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        lang = await self._lang(message.guild)
        logged_at = datetime.now(timezone.utc)
        channel_mention = (
            message.channel.mention
            if isinstance(message.channel, (discord.TextChannel, discord.Thread))
            else f"`{message.channel.id}`"
        )
        content = (message.content or "").strip()
        first_image = None
        for attachment in message.attachments:
            content_type = (attachment.content_type or "").lower()
            if content_type.startswith("image/") or attachment.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".avif")
            ):
                first_image = attachment
                break

        embed = discord.Embed(
            color=discord.Color.from_rgb(255, 74, 34),
            timestamp=logged_at,
        )
        embed.set_author(
            name=str(message.author),
            icon_url=message.author.display_avatar.url,
        )

        if first_image is not None:
            headline = tr(
                lang,
                f"Image sent by {message.author.mention} Deleted in {channel_mention}",
                f"Imagen enviada por {message.author.mention} Eliminada en {channel_mention}",
            )
        else:
            headline = tr(
                lang,
                f"Message sent by {message.author.mention} Deleted in {channel_mention}",
                f"Mensaje enviado por {message.author.mention} Eliminado en {channel_mention}",
            )

        body_lines: list[str] = [headline]
        if content:
            body_lines.append(content)
        elif first_image is None:
            if message.attachments:
                files_text = ", ".join(att.filename for att in message.attachments[:5])
                body_lines.append(
                    tr(
                        lang,
                        f"[Attachment-only message] {files_text}",
                        f"[Mensaje solo con archivo adjunto] {files_text}",
                    )
                )
            else:
                body_lines.append(tr(lang, "[No text content]", "[Sin contenido de texto]"))

        embed.description = "\n".join(body_lines)[:4096]
        if first_image is not None:
            embed.set_image(url=first_image.proxy_url or first_image.url)
        embed.set_footer(text=f"Author: {message.author.id} | Message ID: {message.id}")
        await send_modlog_embed(message.guild, self.bot.db, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return

        lang = await self._lang(before.guild)
        logged_at = datetime.now(timezone.utc)
        embed = discord.Embed(
            color=discord.Color.from_rgb(128, 128, 128),
            timestamp=logged_at,
        )
        embed.set_author(
            name=str(before.author),
            icon_url=before.author.display_avatar.url,
        )
        embed.description = tr(
            lang,
            f"Sent by {before.author.mention}",
            f"Enviado por {before.author.mention}",
        )
        embed.add_field(
            name=tr(lang, "Original message", "Mensaje original"),
            value=(before.content or tr(lang, "[No text content]", "[Sin contenido de texto]"))[:1024],
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "New message", "Nuevo mensaje"),
            value=(after.content or tr(lang, "[No text content]", "[Sin contenido de texto]"))[:1024],
            inline=False,
        )
        channel_value: str
        if isinstance(before.channel, (discord.TextChannel, discord.Thread)):
            if after.jump_url:
                channel_value = f"[#{before.channel.name}]({after.jump_url})"
            else:
                channel_value = before.channel.mention
        else:
            channel_value = str(before.channel.id)
        embed.add_field(
            name=tr(lang, "Channel", "Canal"),
            value=channel_value,
            inline=True,
        )
        embed.set_footer(text=f"Author: {before.author.id} | Message ID: {before.id}")
        await send_modlog_embed(before.guild, self.bot.db, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.guild is None:
            return
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        if before_role_ids == after_role_ids:
            return

        added_roles = [
            role for role in after.roles if role.id not in before_role_ids and not role.is_default()
        ]
        removed_roles = [
            role for role in before.roles if role.id not in after_role_ids and not role.is_default()
        ]
        if not added_roles and not removed_roles:
            return

        lang = await self._lang(after.guild)
        embed = discord.Embed(
            color=discord.Color.dark_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(after),
            icon_url=after.display_avatar.url,
        )

        lines: list[str] = []
        if added_roles:
            roles_text = ", ".join(role.mention for role in added_roles)
            lines.append(
                tr(
                    lang,
                    f"{after.mention} was given the roles {roles_text}",
                    f"{after.mention} recibió los roles {roles_text}",
                )
            )
        if removed_roles:
            roles_text = ", ".join(role.mention for role in removed_roles)
            lines.append(
                tr(
                    lang,
                    f"{after.mention} had the roles removed {roles_text}",
                    f"A {after.mention} se le quitaron los roles {roles_text}",
                )
            )

        embed.description = "\n".join(lines)[:4096]
        embed.set_footer(text=f"ID: {after.id}")
        await send_modlog_embed(after.guild, self.bot.db, embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        lang = await self._lang(guild)
        logged_at = datetime.now(timezone.utc)
        embed = discord.Embed(
            title=tr(lang, "Server join event", "Evento de ingreso al servidor"),
            color=discord.Color.green(),
            timestamp=logged_at,
        )
        embed.set_author(
            name=str(member),
            icon_url=member.display_avatar.url,
        )
        embed.add_field(
            name=tr(lang, "New user", "Nuevo usuario"),
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Joined server at", "Se unio al servidor en"),
            value=self._format_ts(member.joined_at or logged_at, lang),
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Joined Discord at", "Se unio a Discord en"),
            value=self._format_ts(member.created_at, lang),
            inline=True,
        )
        await send_modlog_embed(guild, self.bot.db, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        lang = await self._lang(guild)
        logged_at = datetime.now(timezone.utc)

        last_message = await self._find_last_member_message(guild, member.id)
        if last_message is None:
            last_message_text = tr(lang, "No recent message found.", "No se encontro un mensaje reciente.")
        else:
            content = (last_message.content or "").strip()
            if not content and last_message.attachments:
                content = tr(lang, "[Attachment-only message]", "[Mensaje solo con archivo adjunto]")
            if not content:
                content = tr(lang, "[No text content]", "[Sin contenido de texto]")
            channel_tag = (
                last_message.channel.mention
                if isinstance(last_message.channel, discord.TextChannel)
                else f"#{last_message.channel.id}"
            )
            last_message_text = (
                f"{channel_tag} | {self._format_ts(last_message.created_at, lang)}\n"
                f"{content[:850]}"
            )

        embed = discord.Embed(
            title=tr(lang, "Server leave event", "Evento de salida del servidor"),
            color=discord.Color.red(),
            timestamp=logged_at,
        )
        embed.set_author(
            name=str(member),
            icon_url=member.display_avatar.url,
        )
        embed.add_field(
            name=tr(lang, "User", "Usuario"),
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Left server at", "Salio del servidor en"),
            value=self._format_ts(logged_at, lang),
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Last message (if any)", "Ultimo mensaje (si existe)"),
            value=last_message_text,
            inline=False,
        )
        await send_modlog_embed(guild, self.bot.db, embed)

    async def _apply_warning_role(
        self, guild: discord.Guild, member: discord.Member, warning_level: int, *, reason: str
    ) -> None:
        roles = await ensure_warning_roles(guild, reason=reason)
        selected_level = min(max(warning_level, 1), 3)
        target_role = roles[selected_level]

        to_remove = [role for lvl, role in roles.items() if lvl != selected_level and role in member.roles]
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
        if target_role not in member.roles:
            await member.add_roles(target_role, reason=reason)

    async def _apply_warning_punishment(
        self,
        ctx: commands.Context,
        member: discord.Member,
        warning_count: int,
        reason_text: str,
        lang: str,
    ) -> str:
        guild = ctx.guild
        if guild is None:
            return tr(lang, "No punishment applied.", "No se aplico castigo.")

        if warning_count in {1, 2, 3}:
            duration_map = {1: ("1d", 86400), 2: ("3d", 259200), 3: ("7d", 604800)}
            duration_pretty, duration_seconds = duration_map[warning_count]
            muted_role = await ensure_muted_role(
                guild, self.bot.db, reason=f"Warning punishment requested by {ctx.author}"
            )
            await member.add_roles(
                muted_role,
                reason=f"Auto-tempmute from warning {warning_count} by {ctx.author} | {reason_text}",
            )
            expires_at = int(time.time()) + duration_seconds
            await self.bot.db.upsert_temp_action(
                guild_id=guild.id,
                user_id=member.id,
                action="tempmute",
                expires_at=expires_at,
                duration_input=duration_pretty,
                reason=f"Warning {warning_count}: {reason_text}",
                moderator_id=ctx.author.id,
            )
            return tr(
                lang,
                f"Automatic punishment applied: tempmute for `{duration_pretty}`.",
                f"Castigo automático aplicado: silencio temporal por `{duration_pretty}`.",
            )

        try:
            await member.ban(
                reason=f"Auto-ban after warning {warning_count} by {ctx.author} | {reason_text}"
            )
            return tr(
                lang,
                "Automatic punishment applied: user banned on 4th warning.",
                "Castigo automático aplicado: usuario baneado en la 4ta advertencia.",
            )
        except discord.Forbidden:
            return tr(
                lang,
                "Warning saved, but auto-ban failed due to missing permissions/role hierarchy.",
                "Advertencia guardada, pero el auto-ban falló por permisos o jerarquía de roles.",
            )
        except discord.HTTPException as exc:
            return tr(
                lang,
                f"Warning saved, but auto-ban failed: {exc}",
                f"Advertencia guardada, pero el auto-ban falló: {exc}",
            )

    async def _sync_warning_roles_after_change(
        self,
        guild: discord.Guild,
        member: discord.Member,
        warning_count: int,
        *,
        reason: str,
    ) -> None:
        warning_roles = [
            discord.utils.get(guild.roles, name=role_name)
            for role_name in WARNING_ROLE_NAMES.values()
        ]
        assigned_warning_roles = [role for role in warning_roles if role and role in member.roles]

        if warning_count <= 0:
            if not assigned_warning_roles:
                return
            try:
                await member.remove_roles(*assigned_warning_roles, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                return
            return

        try:
            await self._apply_warning_role(guild, member, warning_count, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            return

    @commands.hybrid_group(
        name="message",
        description="Message moderation commands.",
    )
    async def message(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await self._safe_send(
            ctx,
            tr(
                lang,
                "Use `/message delete`, `/message clear`, or `/message purgeuser`.",
                "Usa `/message delete`, `/message clear` o `/message purgeuser`.",
            ),
        )

    @commands.hybrid_group(
        name="user",
        description="Member moderation commands.",
    )
    async def user(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await self._safe_send(
            ctx,
            tr(
                lang,
                "Use `/user` subcommands like `info`, `warn`, `mute`, `kick`, or `ban`.",
                "Usa subcomandos de `/user` como `info`, `warn`, `mute`, `kick` o `ban`.",
            ),
        )

    @commands.hybrid_group(
        name="role",
        description="Role moderation commands.",
    )
    async def role(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await self._safe_send(
            ctx,
            tr(
                lang,
                "Use `/role add`, `/role remove`, or `/role create`.",
                "Usa `/role add`, `/role remove` o `/role create`.",
            ),
        )

    @commands.hybrid_group(
        name="color",
        description="Color role panel commands.",
    )
    async def color(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await self._safe_send(
            ctx,
            tr(
                lang,
                "Use `/color setup`, `/color list`, `/color add`, `/color remove`, or `/color reload`.",
                "Usa `/color setup`, `/color list`, `/color add`, `/color remove` o `/color reload`.",
            ),
        )

    @message.command(name="delete", description="Delete the last N messages.")
    @owner_or_has_permissions(manage_messages=True)
    async def delete(self, ctx: commands.Context, amount: int) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in text channels.",
                    "Este comando solo se puede usar en canales de texto.",
                )
            )
            return
        if amount < 1 or amount > 500:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Amount must be between 1 and 500.",
                    "La cantidad debe estar entre 1 y 500.",
                )
            )
            return

        try:
            if ctx.interaction:
                deleted = await ctx.channel.purge(limit=amount)
            else:
                command_deleted = False
                if ctx.message is not None:
                    try:
                        await ctx.message.delete()
                        command_deleted = True
                    except (discord.Forbidden, discord.HTTPException):
                        command_deleted = False

                if command_deleted:
                    deleted = await ctx.channel.purge(limit=amount)
                elif ctx.message is not None:
                    command_id = ctx.message.id
                    deleted = await ctx.channel.purge(
                        limit=amount + 1,
                        check=lambda msg: msg.id != command_id,
                    )
                else:
                    deleted = await ctx.channel.purge(limit=amount)
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to delete messages here.",
                    "No tengo permisos para eliminar mensajes aqui.",
                )
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to delete messages: {exc}",
                    f"No se pudieron eliminar los mensajes: {exc}",
                )
            )
            return

        deleted_count = len(deleted)
        if ctx.interaction is None:
            delete_confirmation = tr(lang, "Messages deleted.", "Mensajes eliminados.")
            await self._send_success(ctx, delete_confirmation, delete_after=2)
        await self._log_action(
            ctx.guild,
            title="Messages Deleted",
            moderator=ctx.author,
            target=f"#{ctx.channel.name}",
            reason="Bulk delete requested",
            details=f"Deleted messages: {deleted_count}",
        )

    async def _channel_add_impl(self, ctx: commands.Context, channel_name: str) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        raw_input = channel_name.strip()
        if not raw_input:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Provide a channel name to create.",
                    "Proporciona un nombre de canal para crear.",
                ),
            )
            return

        normalized_name = raw_input.lstrip("#").strip().lower()
        normalized_name = re.sub(r"\s+", "-", normalized_name)
        normalized_name = re.sub(r"[^a-z0-9\-_]", "", normalized_name)
        normalized_name = normalized_name.strip("-_")
        if not normalized_name:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Invalid channel name. Use letters, numbers, spaces, `-` or `_`.",
                    "Nombre de canal inválido. Usa letras, números, espacios, `-` o `_`.",
                ),
            )
            return
        normalized_name = normalized_name[:100]

        existing_by_name = discord.utils.get(ctx.guild.text_channels, name=normalized_name)
        if existing_by_name is not None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"That channel already exists: {existing_by_name.mention}.",
                    f"Ese canal ya existe: {existing_by_name.mention}.",
                ),
            )
            return

        try:
            created = await ctx.guild.create_text_channel(
                normalized_name,
                reason=f"Channel created by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to create channels.",
                    "No tengo permisos para crear canales.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to create channel: {exc}",
                    f"No se pudo crear el canal: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Created channel {created.mention}.",
                f"Canal creado {created.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Channel Created",
            moderator=ctx.author,
            target=f"#{created.name}",
            reason="",
        )

    async def _channel_delete_impl(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        lang = await self._lang(ctx.guild)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        if ctx.channel and channel.id == ctx.channel.id:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "You cannot delete the current channel where this command is running.",
                    "No puedes eliminar el canal actual donde se está ejecutando este comando.",
                ),
            )
            return

        deleted_name = channel.name
        deleted_id = channel.id
        try:
            await channel.delete(reason=f"Channel deleted by {ctx.author}")
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to delete channels.",
                    "No tengo permisos para eliminar canales.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to delete channel: {exc}",
                    f"No se pudo eliminar el canal: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Deleted channel `#{deleted_name}`.",
                f"Canal eliminado `#{deleted_name}`.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Channel Deleted",
            moderator=ctx.author,
            target=deleted_name,
            reason="",
        )

    @commands.hybrid_group(
        name="channel",
        description="Channel management commands.",
    )
    @owner_or_has_permissions(manage_channels=True)
    async def channel(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await self._safe_send(
            ctx,
                tr(
                    lang,
                    "Use `/channel add`, `/channel delete`, `/channel clear`, `/channel clone`, `/channel lock`, `/channel unlock`, or `/channel slowmode`.",
                    "Usa `/channel add`, `/channel delete`, `/channel clear`, `/channel clone`, `/channel lock`, `/channel unlock` o `/channel slowmode`.",
                ),
            )

    @message.command(
        name="clear",
        description="Clear all messages in a channel without recreating it.",
    )
    @owner_or_has_permissions(manage_channels=True)
    @discord.app_commands.describe(
        channel="Channel to clear. Leave empty to clear the current channel.",
    )
    async def clearchannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        target_channel = channel
        if target_channel is None:
            if not isinstance(ctx.channel, discord.TextChannel):
                await self._safe_send(
                    ctx,
                    tr(
                        lang,
                        "Use this command in a text channel or specify one.",
                        "Usa este comando en un canal de texto o especifica uno.",
                    ),
                )
                return
            target_channel = ctx.channel

        original_name = target_channel.name
        original_id = target_channel.id
        deleted_count = 0
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        recent_batch: list[discord.Message] = []

        async def flush_recent_batch() -> None:
            nonlocal deleted_count
            if not recent_batch:
                return
            if len(recent_batch) == 1:
                await recent_batch[0].delete()
            else:
                await target_channel.delete_messages(recent_batch)
            deleted_count += len(recent_batch)
            recent_batch.clear()

        try:
            async for message in target_channel.history(limit=None, oldest_first=False):
                if message.created_at >= recent_cutoff:
                    recent_batch.append(message)
                    if len(recent_batch) >= 100:
                        await flush_recent_batch()
                    continue
                await flush_recent_batch()
                await message.delete()
                deleted_count += 1
            await flush_recent_batch()
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to clear that channel.",
                    "No tengo permisos para limpiar ese canal.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to clear channel: {exc}",
                    f"No se pudo limpiar el canal: {exc}",
                ),
            )
            return

        success_text = tr(
            lang,
            f"Cleared messages in {target_channel.mention}.",
            f"Mensajes limpiados en {target_channel.mention}.",
        )
        await self._send_success(ctx, success_text)

        await self._log_action(
            ctx.guild,
            title="Channel Cleared",
            moderator=ctx.author,
            target=f"#{original_name} ({original_id})",
            reason="Manual channel clear",
            details=f"Deleted messages: {deleted_count}",
        )

    @channel.command(
        name="clear",
        description="Clear all messages in a channel without recreating it.",
    )
    @owner_or_has_permissions(manage_channels=True)
    @discord.app_commands.describe(
        channel="Channel to clear. Leave empty to clear the current channel.",
    )
    async def channel_clear(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.clearchannel(ctx, channel)

    @channel.command(
        name="clone",
        description="Clone a text channel.",
    )
    @owner_or_has_permissions(manage_channels=True)
    @discord.app_commands.describe(
        channel="Channel to clone. Leave empty to clone the current channel.",
    )
    async def clonechannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        target_channel = channel
        if target_channel is None:
            if not isinstance(ctx.channel, discord.TextChannel):
                await self._safe_send(
                    ctx,
                    tr(
                        lang,
                        "Use this command in a text channel or specify one.",
                        "Usa este comando en un canal de texto o especifica uno.",
                    ),
                )
                return
            target_channel = ctx.channel

        try:
            cloned = await target_channel.clone(
                reason=f"Channel cloned by {ctx.author}",
            )
            await cloned.edit(
                category=target_channel.category,
                position=min(
                    target_channel.position + 1,
                    max(0, len(ctx.guild.channels) - 1),
                ),
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to clone that channel.",
                    "No tengo permisos para clonar ese canal.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to clone channel: {exc}",
                    f"No se pudo clonar el canal: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Cloned channel {target_channel.mention} into {cloned.mention}.",
                f"Canal clonado {target_channel.mention} en {cloned.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Channel Cloned",
            moderator=ctx.author,
            target=f"#{target_channel.name} ({target_channel.id})",
            reason="Manual channel clone",
            details=f"Clone created: #{cloned.name} ({cloned.id})",
        )

    @channel.command(name="add", description="Create a new text channel.")
    @owner_or_has_permissions(manage_channels=True)
    @discord.app_commands.rename(channel_name="name")
    async def channel_add(self, ctx: commands.Context, *, channel_name: str) -> None:
        await self._maybe_defer(ctx)
        await self._channel_add_impl(ctx, channel_name)

    @channel.command(name="delete", description="Delete a text channel.")
    @owner_or_has_permissions(manage_channels=True)
    async def channel_delete(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        await self._maybe_defer(ctx)
        await self._channel_delete_impl(ctx, channel)

    @user.command(
        name="setnick",
        description="Set a user's server nickname.",
    )
    @owner_or_has_permissions(manage_nicknames=True)
    @discord.app_commands.describe(
        user="User whose nickname you want to change.",
        nickname="New nickname (1 to 32 characters).",
    )
    async def setnick(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        nickname: str,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user
        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        normalized_nick = nickname.strip()
        if not normalized_nick:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Provide a nickname to set.",
                    "Proporciona un apodo para establecer.",
                ),
            )
            return
        if len(normalized_nick) > 32:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Nickname must be 32 characters or fewer.",
                    "El apodo debe tener 32 caracteres o menos.",
                ),
            )
            return

        try:
            await member.edit(
                nick=normalized_nick,
                reason=f"Nickname changed by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to change that nickname.",
                    "No tengo permisos para cambiar ese apodo.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to change nickname: {exc}",
                    f"No se pudo cambiar el apodo: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Nickname updated for {member.mention}: `{normalized_nick}`.",
                f"Apodo actualizado para {member.mention}: `{normalized_nick}`.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Nickname Updated",
            moderator=ctx.author,
            target=member.mention,
            reason=f"New nickname: {normalized_nick}",
        )

    @channel.command(name="lock", description="Lock a text channel.")
    @owner_or_has_permissions(manage_channels=True)
    async def lock(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        channel, resolve_error = await self._resolve_channel_or_current(
            ctx,
            channel,
            lang,
        )
        if channel is None:
            await self._safe_send(
                ctx,
                resolve_error or tr(lang, "Text channel not found.", "Canal de texto no encontrado."),
            )
            return

        overwrite = channel.overwrites_for(ctx.guild.default_role)
        if overwrite.send_messages is False:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{channel.mention} is already locked.",
                    f"{channel.mention} ya está bloqueado.",
                ),
            )
            return
        overwrite.send_messages = False

        try:
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason=f"Channel locked by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to lock channels.",
                    "No tengo permisos para bloquear canales.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to lock channel: {exc}",
                    f"No se pudo bloquear el canal: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Locked {channel.mention}.",
                f"Canal bloqueado {channel.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Channel Locked",
            moderator=ctx.author,
            target=channel.mention,
            reason="Manual channel lock",
        )

    @channel.command(name="unlock", description="Unlock a text channel.")
    @owner_or_has_permissions(manage_channels=True)
    async def unlock(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        channel, resolve_error = await self._resolve_channel_or_current(
            ctx,
            channel,
            lang,
        )
        if channel is None:
            await self._safe_send(
                ctx,
                resolve_error or tr(lang, "Text channel not found.", "Canal de texto no encontrado."),
            )
            return

        overwrite = channel.overwrites_for(ctx.guild.default_role)
        if overwrite.send_messages is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{channel.mention} is already unlocked.",
                    f"{channel.mention} ya está desbloqueado.",
                ),
            )
            return
        overwrite.send_messages = None

        try:
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason=f"Channel unlocked by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to unlock channels.",
                    "No tengo permisos para desbloquear canales.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to unlock channel: {exc}",
                    f"No se pudo desbloquear el canal: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Unlocked {channel.mention}.",
                f"Canal desbloqueado {channel.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Channel Unlocked",
            moderator=ctx.author,
            target=channel.mention,
            reason="Manual channel unlock",
        )

    @channel.command(name="slowmode", description="Set/disable slowmode on a channel.")
    @owner_or_has_permissions(manage_channels=True)
    @discord.app_commands.describe(
        channel="Target text channel.",
        limit="Slowmode limit in seconds, or 'disable'.",
    )
    async def slowmode(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        limit: str,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        token = (limit or "").strip().lower()
        if token in {"disable", "off"}:
            seconds = 0
        elif token.isdigit():
            seconds = int(token)
        else:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Usage: `/channel slowmode <#channel> <seconds|disable>`.",
                    "Uso: `/channel slowmode <#canal> <segundos|disable>`.",
                ),
            )
            return

        if seconds < 0 or seconds > 21600:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Slowmode must be between 0 and 21600 seconds.",
                    "El modo lento debe estar entre 0 y 21600 segundos.",
                ),
            )
            return

        try:
            await channel.edit(
                slowmode_delay=seconds,
                reason=f"Slowmode changed by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to change slowmode.",
                    "No tengo permisos para cambiar el modo lento.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to change slowmode: {exc}",
                    f"No se pudo cambiar el modo lento: {exc}",
                ),
            )
            return

        if seconds == 0:
            response = tr(
                lang,
                f"Slowmode disabled in {channel.mention}.",
                f"Modo lento desactivado en {channel.mention}.",
            )
        else:
            response = tr(
                lang,
                f"Slowmode set to `{seconds}s` in {channel.mention}.",
                f"Modo lento establecido en `{seconds}s` en {channel.mention}.",
            )
        await self._send_success(ctx, response)
        await self._log_action(
            ctx.guild,
            title="Slowmode Updated",
            moderator=ctx.author,
            target=channel.mention,
            reason="Manual slowmode update",
            details=f"Slowmode: {seconds}s",
        )

    @message.command(name="purgeuser", description="Delete messages from one user in this channel.")
    @owner_or_has_permissions(manage_messages=True)
    async def purgeuser(
        self,
        ctx: commands.Context,
        user: discord.Member,
        amount: int,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in text channels.",
                    "Este comando solo se puede usar en canales de texto.",
                ),
            )
            return
        if amount < 1 or amount > 500:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Amount must be between 1 and 500.",
                    "La cantidad debe estar entre 1 y 500.",
                ),
            )
            return

        member = user

        command_id = ctx.message.id if ctx.message else None
        if ctx.interaction is None and ctx.message is not None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        candidates: list[discord.Message] = []
        try:
            async for msg in ctx.channel.history(limit=2000):
                if command_id is not None and msg.id == command_id:
                    continue
                if msg.author.id == member.id:
                    candidates.append(msg)
                    if len(candidates) >= amount:
                        break
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to read message history: {exc}",
                    f"No se pudo leer el historial de mensajes: {exc}",
                ),
            )
            return

        if not candidates:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"No recent messages found for {member.mention}.",
                    f"No se encontraron mensajes recientes de {member.mention}.",
                ),
            )
            return

        deleted_count = 0
        for msg in candidates:
            try:
                await msg.delete()
                deleted_count += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

        if deleted_count == 0:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "No messages could be deleted due to permissions or age limits.",
                    "No se pudo eliminar ningún mensaje por permisos o límites de antigüedad.",
                ),
            )
            return

        if ctx.interaction is None:
            await self._send_success(
                ctx,
                tr(lang, "Messages deleted.", "Mensajes eliminados."),
                delete_after=2,
            )
        await self._log_action(
            ctx.guild,
            title="User Messages Purged",
            moderator=ctx.author,
            target=f"{member.mention} in #{ctx.channel.name}",
            reason="Manual user purge",
            details=f"Requested: {amount} | Deleted: {deleted_count}",
        )

    @role.command(name="add", description="Add a role to a user.")
    @owner_or_has_permissions(manage_roles=True)
    async def roleadd(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        role: str,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user
        role_obj, role_error = await self._resolve_target_role(ctx.guild, role, lang)
        if role_obj is None:
            await self._safe_send(ctx, role_error or tr(lang, "Role not found.", "Rol no encontrado."))
            return

        actor = ctx.author if isinstance(ctx.author, discord.Member) else ctx.guild.get_member(ctx.author.id)
        if actor is None:
            await self._safe_send(ctx, tr(lang, "Could not resolve your member profile.", "No pude resolver tu perfil de miembro."))
            return

        can_role, role_msg = self._can_manage_role(ctx.guild, actor, role_obj, lang)
        if not can_role:
            await self._safe_send(ctx, role_msg)
            return
        can_member, member_msg = self._can_moderate(ctx.guild, actor, member, lang)
        if not can_member:
            await self._safe_send(ctx, member_msg)
            return
        if role_obj in member.roles:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{member.mention} already has {role_obj.mention}.",
                    f"{member.mention} ya tiene {role_obj.mention}.",
                ),
            )
            return

        try:
            await member.add_roles(role_obj, reason=f"Role added by {ctx.author}")
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to add that role.",
                    "No tengo permisos para agregar ese rol.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to add role: {exc}",
                    f"No se pudo agregar el rol: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Added {role_obj.mention} to {member.mention}.",
                f"Se agregó {role_obj.mention} a {member.mention}.",
            ),
        )

    @role.command(name="remove", description="Remove a role from a user.")
    @owner_or_has_permissions(manage_roles=True)
    async def roleremove(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        role: str,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user
        role_obj, role_error = await self._resolve_target_role(ctx.guild, role, lang)
        if role_obj is None:
            await self._safe_send(ctx, role_error or tr(lang, "Role not found.", "Rol no encontrado."))
            return

        actor = ctx.author if isinstance(ctx.author, discord.Member) else ctx.guild.get_member(ctx.author.id)
        if actor is None:
            await self._safe_send(ctx, tr(lang, "Could not resolve your member profile.", "No pude resolver tu perfil de miembro."))
            return

        can_role, role_msg = self._can_manage_role(ctx.guild, actor, role_obj, lang)
        if not can_role:
            await self._safe_send(ctx, role_msg)
            return
        can_member, member_msg = self._can_moderate(ctx.guild, actor, member, lang)
        if not can_member:
            await self._safe_send(ctx, member_msg)
            return
        if role_obj not in member.roles:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{member.mention} does not have {role_obj.mention}.",
                    f"{member.mention} no tiene {role_obj.mention}.",
                ),
            )
            return

        try:
            await member.remove_roles(role_obj, reason=f"Role removed by {ctx.author}")
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to remove that role.",
                    "No tengo permisos para quitar ese rol.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to remove role: {exc}",
                    f"No se pudo quitar el rol: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Removed {role_obj.mention} from {member.mention}.",
                f"Se quitó {role_obj.mention} de {member.mention}.",
            ),
        )

    @role.command(name="create", description="Create a role (optional hex color).")
    @owner_or_has_permissions(manage_roles=True)
    @discord.app_commands.rename(role_name="name", color_hex="color")
    async def createrole(
        self,
        ctx: commands.Context,
        role_name: str,
        color_hex: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        role_name = role_name.strip()
        if not role_name:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Provide a role name.",
                    "Proporciona un nombre de rol.",
                ),
            )
            return
        if len(role_name) > 100:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Role name cannot exceed 100 characters.",
                    "El nombre del rol no puede exceder 100 caracteres.",
                ),
            )
            return

        existing = discord.utils.find(
            lambda r: r.name.casefold() == role_name.casefold(),
            ctx.guild.roles,
        )
        if existing is not None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"That role already exists: {existing.mention}.",
                    f"Ese rol ya existe: {existing.mention}.",
                ),
            )
            return

        create_color = discord.Color.default()
        clean_hex_display = "default"
        if color_hex:
            color_input = color_hex.strip()
            match = re.fullmatch(r"#?([0-9a-fA-F]{6})", color_input)
            if not match:
                await self._safe_send(
                    ctx,
                    tr(
                        lang,
                        "Invalid color format. Use a 6-digit hex color like `#1abc9c`.",
                        "Formato de color inválido. Usa un color hexadecimal de 6 dígitos, por ejemplo `#1abc9c`.",
                    ),
                )
                return
            hex_value = match.group(1).lower()
            create_color = discord.Color(int(hex_value, 16))
            clean_hex_display = f"#{hex_value}"

        try:
            created = await ctx.guild.create_role(
                name=role_name,
                color=create_color,
                reason=f"Role created by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to create roles.",
                    "No tengo permisos para crear roles.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to create role: {exc}",
                    f"No se pudo crear el rol: {exc}",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Created role {created.mention} (color: `{clean_hex_display}`).",
                f"Rol creado {created.mention} (color: `{clean_hex_display}`).",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Role Created",
            moderator=ctx.author,
            target=f"{created.name} ({created.id})",
            reason="Manual role creation",
            details=f"Color: {clean_hex_display}",
        )

    @color.command(
        name="setup",
        description="Create the default server color roles list.",
    )
    @owner_or_has_permissions(administrator=True)
    async def colorsetup(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        me = ctx.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I need `Manage Roles` permission to set up color roles.",
                    "Necesito el permiso `Gestionar roles` para configurar los roles de color.",
                ),
            )
            return

        created_or_updated: list[str] = []
        try:
            for order, (color_name, hex_code) in enumerate(DEFAULT_COLOR_ROLES, start=1):
                role = discord.utils.find(
                    lambda r, n=color_name: r.name.casefold() == n.casefold(),
                    ctx.guild.roles,
                )
                color_obj = discord.Color(int(hex_code[1:], 16))
                if role is None:
                    role = await ctx.guild.create_role(
                        name=color_name,
                        color=color_obj,
                        mentionable=False,
                        reason=f"Default color role setup by {ctx.author}",
                    )
                else:
                    if role < me.top_role and role.color.value != color_obj.value:
                        try:
                            await role.edit(
                                color=color_obj,
                                reason=f"Default color role sync by {ctx.author}",
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass

                await self.bot.db.upsert_color_role(
                    ctx.guild.id,
                    role.id,
                    color_name,
                    hex_code,
                    display_order=order,
                )
                created_or_updated.append(f"{color_name} ({hex_code})")
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to create or edit roles.",
                    "No tengo permisos para crear o editar roles.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to set up color roles: {exc}",
                    f"No se pudieron configurar los roles de color: {exc}",
                ),
            )
            return

        panel_payload = await self._build_color_panel_embed(ctx.guild, lang)
        if panel_payload is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Color setup finished, but no valid color roles were found.",
                    "La configuracion de colores termino, pero no se encontraron roles validos.",
                ),
            )
            return
        panel_embed, panel_file = panel_payload
        panel_embed.title = tr(
            lang,
            "\u2705 Color setup completed",
            "\u2705 Configuraci\u00f3n de colores completada",
        )
        panel_embed.description = tr(
            lang,
            "Current configured colors:\n",
            "Colores configurados actualmente:\n",
        ) + (panel_embed.description or "")
        if panel_file is not None:
            await self._safe_send(ctx, embed=panel_embed, file=panel_file)
        else:
            await self._safe_send(ctx, embed=panel_embed)

        await self._log_action(
            ctx.guild,
            title="Color Roles Setup",
            moderator=ctx.author,
            target=f"{ctx.guild.name} ({ctx.guild.id})",
            reason="Default color role setup",
            details=f"Configured colors: {len(created_or_updated)}",
        )

    @color.command(
        name="list",
        description="Show the configured color roles list.",
    )
    @owner_or_has_permissions(administrator=True)
    async def colors(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        panel_payload = await self._build_color_panel_embed(ctx.guild, lang)
        if panel_payload is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "No color roles are configured yet. Use `/color setup` first.",
                    "Aun no hay colores configurados. Usa `/color setup` primero.",
                ),
            )
            return
        panel_embed, panel_file = panel_payload
        panel_embed.title = tr(lang, "Configured colors", "Colores configurados")
        if panel_file is not None:
            await self._safe_send(ctx, embed=panel_embed, file=panel_file)
        else:
            await self._safe_send(ctx, embed=panel_embed)

    @color.command(
        name="channel",
        description="Set channel and publish the numbered color selector panel.",
    )
    @owner_or_has_permissions(administrator=True)
    async def colorchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        if not await self.bot.db.count_color_roles(ctx.guild.id):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "No color roles are configured yet. Use `/color setup` first.",
                    "Aun no hay colores configurados. Usa `/color setup` primero.",
                ),
            )
            return

        panel_message = await self._publish_color_panel(ctx.guild, channel, lang)
        if panel_message is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Failed to publish the color panel. Check my send-message and add-reaction permissions.",
                    "No se pudo publicar el panel de colores. Revisa mis permisos para enviar mensajes y agregar reacciones.",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Color panel published in {channel.mention}.",
                f"Panel de colores publicado en {channel.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Color Panel Published",
            moderator=ctx.author,
            target=f"{channel.mention} ({channel.id})",
            reason="Color panel publish/update",
            details=f"Message ID: {panel_message.id}",
        )

    @color.command(
        name="reload",
        description="Reload and republish the color selector panel in the configured channel.",
    )
    @owner_or_has_permissions(administrator=True)
    async def colorreload(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        panel_state = await self.bot.db.get_color_panel(ctx.guild.id)
        if panel_state is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "No color channel configured yet. Use `/color channel` first.",
                    "Aun no hay canal de colores configurado. Usa `/color channel` primero.",
                ),
            )
            return

        channel = ctx.guild.get_channel(int(panel_state["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "The configured color channel no longer exists. Set it again with `/color channel`.",
                    "El canal de colores configurado ya no existe. Configuralo de nuevo con `/color channel`.",
                ),
            )
            await self.bot.db.clear_color_panel(ctx.guild.id)
            return

        panel_message = await self._publish_color_panel(ctx.guild, channel, lang)
        if panel_message is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Failed to reload the color panel. Check my send-message and add-reaction permissions.",
                    "No se pudo recargar el panel de colores. Revisa mis permisos para enviar mensajes y agregar reacciones.",
                ),
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Color panel reloaded in {channel.mention}.",
                f"Panel de colores recargado en {channel.mention}.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Color Panel Reloaded",
            moderator=ctx.author,
            target=f"{channel.mention} ({channel.id})",
            reason="Color panel reload",
            details=f"Message ID: {panel_message.id}",
        )

    @color.command(
        name="add",
        description="Add a new selectable color role.",
    )
    @owner_or_has_permissions(administrator=True)
    @discord.app_commands.rename(hex_code="color")
    async def coloradd(
        self,
        ctx: commands.Context,
        hex_code: str,
        *,
        name: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        normalized_hex = self._normalize_hex_color(hex_code)
        if normalized_hex is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Invalid color format. Use a 6-digit hex color like `#1abc9c`.",
                    "Formato de color invalido. Usa un color hexadecimal de 6 digitos como `#1abc9c`.",
                ),
            )
            return

        role_name = (name or normalized_hex).strip()
        if len(role_name) > 100:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Role name cannot exceed 100 characters.",
                    "El nombre del rol no puede exceder 100 caracteres.",
                ),
            )
            return

        existing_colors = await self.bot.db.list_color_roles(ctx.guild.id)
        if len(existing_colors) >= MAX_COLOR_ROLES:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"You can only configure up to {MAX_COLOR_ROLES} color roles for the reaction panel.",
                    f"Solo puedes configurar hasta {MAX_COLOR_ROLES} roles de color para el panel por reacciones.",
                ),
            )
            return

        if any(str(item["color_name"]).casefold() == role_name.casefold() for item in existing_colors):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "A color with that name already exists.",
                    "Ya existe un color con ese nombre.",
                ),
            )
            return

        try:
            created_role = await ctx.guild.create_role(
                name=role_name,
                color=discord.Color(int(normalized_hex[1:], 16)),
                mentionable=False,
                reason=f"Color role created by {ctx.author}",
            )
        except discord.Forbidden:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I do not have permission to create roles.",
                    "No tengo permisos para crear roles.",
                ),
            )
            return
        except discord.HTTPException as exc:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Failed to create color role: {exc}",
                    f"No se pudo crear el rol de color: {exc}",
                ),
            )
            return

        await self.bot.db.upsert_color_role(
            ctx.guild.id,
            created_role.id,
            role_name,
            normalized_hex,
        )
        await self._send_success(
            ctx,
            tr(
                lang,
                f"Color role added: {created_role.mention} (`{normalized_hex}`). Run `/color reload` to refresh the public panel.",
                f"Rol de color agregado: {created_role.mention} (`{normalized_hex}`). Usa `/color reload` para refrescar el panel publico.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Color Role Added",
            moderator=ctx.author,
            target=f"{created_role.name} ({created_role.id})",
            reason="Manual color role add",
            details=f"Hex: {normalized_hex}",
        )

    @color.command(
        name="remove",
        description="Remove a selectable color role by configured color name.",
    )
    @owner_or_has_permissions(administrator=True)
    @discord.app_commands.rename(color_name="name")
    async def colorremove(self, ctx: commands.Context, *, color_name: str) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        existing = await self.bot.db.get_color_role_by_name(ctx.guild.id, color_name)
        if existing is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Color name not found.",
                    "No se encontro ese nombre de color.",
                ),
            )
            return

        role_id = int(existing["role_id"])
        role_name = str(existing["color_name"])
        role = ctx.guild.get_role(role_id)
        if role is not None:
            try:
                await role.delete(reason=f"Color role removed by {ctx.author}")
            except discord.Forbidden:
                await self._safe_send(
                    ctx,
                    tr(
                        lang,
                        "I do not have permission to delete that role.",
                        "No tengo permisos para eliminar ese rol.",
                    ),
                )
                return
            except discord.HTTPException as exc:
                await self._safe_send(
                    ctx,
                    tr(
                        lang,
                        f"Failed to remove color role: {exc}",
                        f"No se pudo eliminar el rol de color: {exc}",
                    ),
                )
                return

        await self.bot.db.delete_color_role_by_name(ctx.guild.id, role_name)
        await self._send_success(
            ctx,
            tr(
                lang,
                f"Color role `{role_name}` removed. Run `/color reload` to refresh the public panel.",
                f"Rol de color `{role_name}` eliminado. Usa `/color reload` para refrescar el panel publico.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Color Role Removed",
            moderator=ctx.author,
            target=f"{role_name} ({role_id})",
            reason="Manual color role removal",
        )

    @user.command(name="info", description="Show user info in this server.")
    @owner_or_has_permissions(moderate_members=True)
    async def userinfo(self, ctx: commands.Context, user: discord.Member) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user

        roles = [role.mention for role in sorted(member.roles, key=lambda r: r.position, reverse=True) if not role.is_default()]
        roles_text = ", ".join(roles) if roles else tr(lang, "None", "Ninguno")

        permissions = [
            permission_name.replace("_", " ").title()
            for permission_name, enabled in member.guild_permissions
            if enabled
        ]
        permissions_text = ", ".join(permissions) if permissions else tr(lang, "None", "Ninguno")
        if len(permissions_text) > 1024:
            permissions_text = permissions_text[:1021] + "..."

        joined_server = (
            f"<t:{int(member.joined_at.timestamp())}:F>"
            if member.joined_at is not None
            else tr(lang, "Unknown", "Desconocido")
        )
        joined_discord = f"<t:{int(member.created_at.timestamp())}:F>"
        boosting = tr(lang, "Yes", "Sí") if member.premium_since else tr(lang, "No", "No")

        embed = discord.Embed(
            color=member.top_role.color if member.top_role.color.value else discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name=tr(lang, "Name", "Nombre"), value=member.display_name[:1024], inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name=tr(lang, "Server Booster", "Impulsa el servidor"), value=boosting, inline=True)
        embed.add_field(name=tr(lang, "Roles", "Roles"), value=roles_text[:1024], inline=False)
        embed.add_field(
            name=tr(lang, "Permissions", "Permisos globales"),
            value=permissions_text,
            inline=False,
        )
        embed.add_field(name=tr(lang, "Joined Server", "Se unió al servidor"), value=joined_server, inline=True)
        embed.add_field(name=tr(lang, "Joined Discord", "Se unió a Discord"), value=joined_discord, inline=True)

        await self._safe_send(
            ctx,
            tr(lang, "User information", "Información del usuario"),
            embed=embed,
        )
        await self._log_action(
            ctx.guild,
            title="User Info Lookup",
            moderator=ctx.author,
            target=f"{member.mention} ({member.id})",
            reason="",
        )

    @user.command(name="mute", description="Mute a user by assigning the Muted role.")
    @owner_or_has_permissions(moderate_members=True)
    async def mute(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        if member.guild_permissions.administrator:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "Cannot mute an administrator due to Discord permission rules.",
                    "No se puede silenciar a un administrador por las reglas de permisos de Discord.",
                )
            )
            return

        muted_role = await ensure_muted_role(
            ctx.guild, self.bot.db, reason=f"Requested by {ctx.author}"
        )
        if muted_role in member.roles:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    f"{member.mention} is already muted.",
                    f"{member.mention} ya está silenciado.",
                )
            )
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await member.add_roles(
            muted_role, reason=f"Muted by {ctx.author} | {reason_text}"
        )
        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been muted. Reason: {reason_text}",
                f"{member.mention} ha sido silenciado. Raz\u00f3n: {reason_text}",
            ),
        )


        await self._log_action(
            ctx.guild,
            title="User Muted",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
        )

    @user.command(name="unmute", description="Remove Muted role from a user.")
    @owner_or_has_permissions(moderate_members=True)
    async def unmute(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        member = user

        settings = await self.bot.db.get_guild_settings(ctx.guild.id)
        muted_role = ctx.guild.get_role(settings.muted_role_id or 0)
        if muted_role is None:
            muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
        if muted_role is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "Muted role does not exist yet.",
                    "El rol Muted aún no existe.",
                )
            )
            return
        if muted_role not in member.roles:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    f"{member.mention} is not muted.",
                    f"{member.mention} no está silenciado.",
                )
            )
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await member.remove_roles(
            muted_role, reason=f"Unmuted by {ctx.author} | {reason_text}"
        )
        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been unmuted. Reason: {reason_text}",
                f"Se ha quitado el silencio a {member.mention}. Raz\u00f3n: {reason_text}",
            ),
        )


        await self._log_action(
            ctx.guild,
            title="User Unmuted",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
        )

    @user.command(name="kick", description="Kick a member from the server.")
    @owner_or_has_permissions(kick_members=True)
    async def kick(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await member.kick(reason=f"Kicked by {ctx.author} | {reason_text}")
        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been kicked. Reason: {reason_text}",
                f"{member.mention} ha sido expulsado. Raz\u00f3n: {reason_text}",
            ),
        )


        await self._log_action(
            ctx.guild,
            title="User Kicked",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
        )

    @user.command(name="ban", description="Ban a member from the server.")
    @owner_or_has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await member.ban(reason=f"Banned by {ctx.author} | {reason_text}")
        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been banned. Reason: {reason_text}",
                f"{member.mention} ha sido baneado. Raz\u00f3n: {reason_text}",
            ),
        )


        await self._log_action(
            ctx.guild,
            title="User Banned",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
        )

    @user.command(
        name="unban", description="Unban a user by ID or mention format."
    )
    @owner_or_has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: commands.Context,
        user: str,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        user_id = parse_user_id_from_text(user)
        if user_id is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "Provide a valid user ID or mention.",
                    "Proporciona una ID de usuario o mención válida.",
                )
            )
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        try:
            await ctx.guild.unban(
                discord.Object(id=user_id),
                reason=f"Unbanned by {ctx.author} | {reason_text}",
            )
        except discord.NotFound:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "That user is not currently banned.",
                    "Ese usuario no está baneado actualmente.",
                )
            )
            return

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Unbanned user `{user_id}`.",
                f"Se desbaneó al usuario `{user_id}`.",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="User Unbanned",
            moderator=ctx.author,
            target=str(user_id),
            reason=reason_text,
        )

    @user.command(name="tempmute", description="Temporarily mute a user.")
    @owner_or_has_permissions(moderate_members=True)
    async def tempmute(
        self,
        ctx: commands.Context,
        user: discord.Member,
        duration: str,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return
        if member.guild_permissions.administrator:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "Cannot tempmute an administrator due to Discord permission rules.",
                    "No se puede silenciar temporalmente a un administrador por las reglas de permisos de Discord.",
                )
            )
            return

        try:
            duration_seconds, duration_pretty = parse_duration(duration)
        except DurationParseError:
            await self._safe_send(ctx, 
                tr(
                    lang,
                    "Invalid duration format. Use values like 120s, 2m, 3h, 1d.",
                    "Formato de duración inválido. Usa valores como 120s, 2m, 3h, 1d.",
                )
            )
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        muted_role = await ensure_muted_role(
            ctx.guild, self.bot.db, reason=f"Requested by {ctx.author}"
        )
        await member.add_roles(
            muted_role,
            reason=f"Tempmuted by {ctx.author} | {duration_pretty} | {reason_text}",
        )

        expires_at = int(time.time()) + duration_seconds
        await self.bot.db.upsert_temp_action(
            guild_id=ctx.guild.id,
            user_id=member.id,
            action="tempmute",
            expires_at=expires_at,
            duration_input=duration_pretty,
            reason=reason_text,
            moderator_id=ctx.author.id,
        )

        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been temporarily muted for `{duration_pretty}`. Reason: {reason_text}",
                f"{member.mention} ha sido silenciado temporalmente por `{duration_pretty}`. Raz\u00f3n: {reason_text}",
            ),
        )


        await self._log_action(
            ctx.guild,
            title="User Temporarily Muted",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
            details=f"Duration: {duration_pretty}",
        )

    @user.command(name="tempban", description="Temporarily ban a user.")
    @owner_or_has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx: commands.Context,
        user: discord.Member,
        duration: str,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        try:
            duration_seconds, duration_pretty = parse_duration(duration)
        except DurationParseError:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Invalid duration format. Use values like 120s, 2m, 3h, 1d.",
                    "Formato de duraci\u00f3n inv\u00e1lido. Usa valores como 120s, 2m, 3h, 1d.",
                ),
            )
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await member.ban(
            reason=f"Tempbanned by {ctx.author} | {duration_pretty} | {reason_text}"
        )

        expires_at = int(time.time()) + duration_seconds
        await self.bot.db.upsert_temp_action(
            guild_id=ctx.guild.id,
            user_id=member.id,
            action="tempban",
            expires_at=expires_at,
            duration_input=duration_pretty,
            reason=reason_text,
            moderator_id=ctx.author.id,
        )

        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been temporarily banned for `{duration_pretty}`. Reason: {reason_text}",
                f"{member.mention} ha sido baneado temporalmente por `{duration_pretty}`. Raz\u00f3n: {reason_text}",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="User Temporarily Banned",
            moderator=ctx.author,
            target=f"{member.mention} ({member.id})",
            reason=reason_text,
            details=f"Duration: {duration_pretty}",
        )

    @user.command(name="warn", description="Warn a user.")
    @owner_or_has_permissions(moderate_members=True)
    async def warn(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user

        can, msg = self._can_moderate(ctx.guild, ctx.author, member, lang)
        if not can:
            await self._safe_send(ctx, msg)
            return

        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        await self.bot.db.add_warning(
            guild_id=ctx.guild.id,
            user_id=member.id,
            moderator_id=ctx.author.id,
            reason=reason_text,
        )
        warning_count = len(await self.bot.db.get_warnings(ctx.guild.id, member.id))

        await self._sync_warning_roles_after_change(
            ctx.guild,
            member,
            warning_count,
            reason=f"Warning role update requested by {ctx.author}",
        )

        punishment_info = await self._apply_warning_punishment(
            ctx, member, warning_count, reason_text, lang
        )
        await self._send_success(
            ctx,
            tr(
                lang,
                f"{member.mention} has been warned. Reason: {reason_text}",
                f"{member.mention} ha sido advertido. Raz\u00f3n: {reason_text}",
            ),
        )

        await self._log_action(
            ctx.guild,
            title="User Warned",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
            details=f"Warning count: {warning_count} | {punishment_info}",
        )

    @user.command(
        name="unwarn",
        description="Remove the 1st, 2nd, or 3rd warning from a user.",
    )
    @owner_or_has_permissions(moderate_members=True)
    @discord.app_commands.rename(warning_number="number")
    async def unwarn(
        self,
        ctx: commands.Context,
        user: discord.Member,
        warning_number: int,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        if warning_number not in {1, 2, 3}:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Warning number must be 1, 2, or 3.",
                    "El numero de advertencia debe ser 1, 2 o 3.",
                ),
            )
            return

        member = user

        warnings_for_user = await self.bot.db.get_warnings(ctx.guild.id, member.id)
        if not warnings_for_user:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{member.mention} has no warnings.",
                    f"{member.mention} no tiene advertencias.",
                ),
            )
            return

        warnings_ordered = list(reversed(warnings_for_user))
        target_index = warning_number - 1
        if target_index >= len(warnings_ordered):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{member.mention} only has {len(warnings_ordered)} warning(s).",
                    f"{member.mention} solo tiene {len(warnings_ordered)} advertencia(s).",
                ),
            )
            return

        target_warning = warnings_ordered[target_index]
        warning_id = int(target_warning["id"])
        removed = await self.bot.db.delete_warning_by_id(ctx.guild.id, warning_id)
        if removed is None:
            await self._safe_send(
                ctx,
                tr(lang, "Warning not found.", "No se encontro la advertencia."),
            )
            return

        removed_reason = str(removed["reason"])
        current_count = len(await self.bot.db.get_warnings(ctx.guild.id, member.id))
        await self._sync_warning_roles_after_change(
            ctx.guild,
            member,
            current_count,
            reason=f"Warning removed by {ctx.author}",
        )

        target_display = member.mention
        await self._send_success(
            ctx,
            tr(
                lang,
                f"Warning #{warning_number} was removed from {target_display}. Reason: {removed_reason}",
                f"Se elimin\u00f3 la advertencia #{warning_number} de {target_display}. Raz\u00f3n: {removed_reason}",
            ),
        )
        await self._log_action(
            ctx.guild,
            title="Warning Removed",
            moderator=ctx.author,
            target=target_display,
            reason="Warning removed by moderator",
            details=f"Removed warning ID: {warning_id} | Reason: {removed_reason[:220]}",
        )

    @user.command(name="warnings", description="List warnings for a user.")
    @owner_or_has_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, user: discord.Member) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user

        warnings_list = await self.bot.db.get_warnings(ctx.guild.id, member.id)
        if not warnings_list:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"{member.mention} has no warnings.",
                    f"{member.mention} no tiene advertencias.",
                ),
            )
            return

        lines = []
        for item in warnings_list[:10]:
            lines.append(
                f"ID `{item['id']}` | Mod `{item['moderator_id']}` | "
                f"{item['created_at']} | {item['reason']}"
            )

        await self._safe_send(
            ctx,
            tr(
                lang,
                f"Warnings for {member.mention} ({len(warnings_list)} total):\n",
                f"Advertencias de {member.mention} ({len(warnings_list)} total):\n",
            )
            + "\n".join(lines),
        )

    @user.command(name="clearwarnings", description="Clear all warnings for a user.")
    @owner_or_has_permissions(moderate_members=True)
    async def clearwarnings(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)
        if ctx.guild is None:
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                ),
            )
            return

        member = user

        count = await self.bot.db.clear_warnings(ctx.guild.id, member.id)
        reason_text = reason or tr(lang, "No reason provided.", "Sin raz\u00f3n proporcionada.")
        removed_roles_note = ""
        warning_roles = [
            discord.utils.get(ctx.guild.roles, name=role_name)
            for role_name in WARNING_ROLE_NAMES.values()
        ]
        assigned_warning_roles = [role for role in warning_roles if role and role in member.roles]
        if assigned_warning_roles:
            try:
                await member.remove_roles(
                    *assigned_warning_roles,
                    reason=f"Warnings cleared by {ctx.author} | {reason_text}",
                )
                removed_roles_note = tr(
                    lang,
                    " Warning roles were removed.",
                    " Se eliminaron los roles de advertencia.",
                )
            except (discord.Forbidden, discord.HTTPException):
                removed_roles_note = tr(
                    lang,
                    " Warning roles could not be removed due to permissions/role hierarchy.",
                    " No se pudieron eliminar los roles de advertencia por permisos o jerarqu\u00eda de roles.",
                )

        await self._send_success(
            ctx,
            tr(
                lang,
                f"Cleared {count} warnings for {member.mention}.{removed_roles_note}",
                f"Se limpiaron {count} advertencias de {member.mention}.{removed_roles_note}",
            ),
        )

        await self._log_action(
            ctx.guild,
            title="Warnings Cleared",
            moderator=ctx.author,
            target=member.mention,
            reason=reason_text,
            details=f"Cleared warnings: {count}",
        )

    @message.error
    @user.error
    @role.error
    @color.error
    @delete.error
    @clearchannel.error
    @channel_clear.error
    @clonechannel.error
    @channel.error
    @channel_add.error
    @channel_delete.error
    @setnick.error
    @lock.error
    @unlock.error
    @slowmode.error
    @purgeuser.error
    @roleadd.error
    @roleremove.error
    @createrole.error
    @colorsetup.error
    @colors.error
    @colorchannel.error
    @colorreload.error
    @coloradd.error
    @colorremove.error
    @userinfo.error
    @mute.error
    @unmute.error
    @kick.error
    @ban.error
    @unban.error
    @tempmute.error
    @tempban.error
    @warn.error
    @unwarn.error
    @warnings.error
    @clearwarnings.error
    async def moderation_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        lang = await self._lang(ctx.guild)
        if isinstance(error, commands.MissingPermissions):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "You do not have permission to use this command.",
                    "No tienes permiso para usar este comando.",
                )
            )
            return
        if isinstance(error, commands.BotMissingPermissions):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "I am missing required permissions for this command. Please check role hierarchy and bot permissions.",
                    "Me faltan permisos requeridos para este comando. Revisa la jerarquía de roles y los permisos del bot.",
                )
            )
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    f"Missing argument: `{error.param.name}`.",
                    f"Falta el argumento: `{error.param.name}`.",
                ),
            )
            return
        if isinstance(error, commands.BadArgument):
            await self._safe_send(
                ctx,
                tr(
                    lang,
                    "Invalid argument provided.",
                    "Se proporcionó un argumento inválido.",
                ),
            )
            return
        await self._safe_send(
            ctx,
            tr(
                lang,
                "Command failed due to an internal error. Please try again.",
                "El comando fallo por un error interno. Intenta de nuevo.",
            )
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))

