from __future__ import annotations

import re
import discord
from discord import app_commands
from discord.ext import commands

from utils.i18n import tr


ANNOUNCEMENT_MODES = {"text", "embed", "both"}
TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")


async def _slash_only_invocation(ctx: commands.Context) -> bool:
    return ctx.interaction is not None


class AnnouncementCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _default_message(kind: str) -> str:
        if kind == "welcome":
            return "Hello {user}, welcome to {server}!"
        return "{user} left {server}."

    @staticmethod
    def _normalize_mode(raw_mode: str | None) -> str | None:
        if raw_mode is None:
            return None
        normalized = raw_mode.strip().lower()
        if normalized in ANNOUNCEMENT_MODES:
            return normalized
        return None

    @staticmethod
    def _normalize_hex_color(raw: str) -> str | None:
        value = raw.strip()
        if value.lower() in {"none", "clear", "remove"}:
            return "#00FFFF"
        if not value.startswith("#"):
            value = f"#{value}"
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return None
        return value.upper()

    @staticmethod
    def _parse_color(value: str) -> discord.Color:
        normalized = value.strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", normalized):
            return discord.Color.from_rgb(0, 255, 255)
        return discord.Color(int(normalized[1:], 16))

    @staticmethod
    def _normalize_image_url(raw: str) -> str | None:
        value = raw.strip()
        if value.lower() in {"none", "clear", "remove"}:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return None

    @staticmethod
    def _match_named_channel(guild: discord.Guild, token: str) -> discord.abc.GuildChannel | None:
        probe = token.strip().lstrip("#")
        if not probe:
            return None
        if probe.isdigit():
            found = guild.get_channel(int(probe))
            if isinstance(found, discord.abc.GuildChannel):
                return found

        lowered = probe.casefold()
        channels = [channel for channel in guild.channels if isinstance(channel, discord.abc.GuildChannel)]
        exact = next((ch for ch in channels if ch.name.casefold() == lowered), None)
        if exact is not None:
            return exact
        starts_with = next((ch for ch in channels if ch.name.casefold().startswith(lowered)), None)
        if starts_with is not None:
            return starts_with
        return next((ch for ch in channels if lowered in ch.name.casefold()), None)

    @staticmethod
    def _match_named_role(guild: discord.Guild, token: str) -> discord.Role | None:
        probe = token.strip().lstrip("@")
        if not probe:
            return None
        lowered = probe.casefold()
        roles = [role for role in guild.roles if role.name != "@everyone"]
        exact = next((role for role in roles if role.name.casefold() == lowered), None)
        if exact is not None:
            return exact
        starts_with = next((role for role in roles if role.name.casefold().startswith(lowered)), None)
        if starts_with is not None:
            return starts_with
        return next((role for role in roles if lowered in role.name.casefold()), None)

    def _render_template(
        self,
        *,
        template: str,
        guild: discord.Guild,
        member: discord.abc.User | discord.Member,
        announcement_channel: discord.abc.GuildChannel | None,
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_token = match.group(1).strip()
            if not raw_token:
                return match.group(0)

            token = raw_token.casefold()
            if token == "user":
                return member.mention
            if token == "username":
                return getattr(member, "display_name", member.name)
            if token == "avatar":
                avatar = getattr(member, "display_avatar", None)
                return avatar.url if avatar else ""
            if token == "server":
                return guild.name
            if token == "channel":
                return announcement_channel.mention if announcement_channel else ""

            if raw_token.startswith("#") or raw_token.isdigit():
                channel = self._match_named_channel(guild, raw_token)
                if channel is not None:
                    return channel.mention

            named_channel = self._match_named_channel(guild, raw_token)
            if named_channel is not None:
                return named_channel.mention

            role = self._match_named_role(guild, raw_token)
            if role is not None:
                return role.mention

            return match.group(0)

        return TOKEN_PATTERN.sub(replace, template)

    async def _resolve_announcement_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        existing = guild.get_channel(channel_id)
        if isinstance(existing, discord.abc.Messageable):
            return existing
        try:
            fetched = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        if isinstance(fetched, discord.abc.Messageable):
            return fetched
        return None

    def _build_embed(
        self,
        *,
        color_hex: str,
        message_text: str,
        mode: str,
        image_url: str,
    ) -> discord.Embed:
        embed = discord.Embed(color=self._parse_color(color_hex))
        include_description = mode in {"embed", "both"}
        if include_description and message_text.strip():
            embed.description = message_text
        if image_url.strip():
            embed.set_image(url=image_url.strip())
        return embed

    async def _send_announcement(
        self,
        *,
        kind: str,
        guild: discord.Guild,
        member: discord.abc.User | discord.Member,
        force_preview: bool = False,
        preview_channel: discord.abc.Messageable | None = None,
    ) -> tuple[bool, str]:
        config = await self.bot.db.get_announcement_settings(guild.id, kind)
        if not config.enabled and not force_preview:
            return False, "disabled"

        target_channel = preview_channel
        if target_channel is None:
            target_channel = await self._resolve_announcement_channel(guild, config.channel_id)
        if target_channel is None:
            return False, "missing-channel"

        mode = self._normalize_mode(config.mode) or "text"
        template = (config.message_text or "").strip() or self._default_message(kind)
        rendered = self._render_template(
            template=template,
            guild=guild,
            member=member,
            announcement_channel=target_channel if isinstance(target_channel, discord.abc.GuildChannel) else None,
        ).strip()

        allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
        if mode == "text":
            await target_channel.send(rendered, allowed_mentions=allowed_mentions)
            return True, "sent"

        embed = self._build_embed(
            color_hex=config.color_hex,
            message_text=rendered,
            mode=mode,
            image_url=config.image_url,
        )

        has_embed_payload = bool((embed.description or "").strip()) or bool(embed.image.url)
        if mode == "embed":
            if not has_embed_payload:
                embed.description = rendered
            await target_channel.send(embed=embed, allowed_mentions=allowed_mentions)
            return True, "sent"

        # mode == both
        if not (embed.description or "").strip():
            embed.description = rendered
        await target_channel.send(embed=embed, allowed_mentions=allowed_mentions)
        return True, "sent"

    async def _show_config(self, ctx: commands.Context, kind: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(ctx.guild)
        config = await self.bot.db.get_announcement_settings(ctx.guild.id, kind)
        channel = await self._resolve_announcement_channel(ctx.guild, config.channel_id)
        channel_text = (
            channel.mention
            if isinstance(channel, discord.abc.GuildChannel)
            else ("Not set" if lang == "en" else "Sin configurar")
        )

        title = tr(
            lang,
            f"{kind.title()} configuration",
            f"Configuracion de {kind}",
        )
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name=tr(lang, "Enabled", "Activo"), value=str(config.enabled), inline=True)
        embed.add_field(name=tr(lang, "Mode", "Modo"), value=f"`{config.mode}`", inline=True)
        embed.add_field(name=tr(lang, "Channel", "Canal"), value=channel_text, inline=False)
        embed.add_field(
            name=tr(lang, "Message", "Mensaje"),
            value=(config.message_text or self._default_message(kind))[:1024],
            inline=False,
        )
        embed.add_field(
            name=tr(lang, "Image URL", "URL de imagen"),
            value=config.image_url or tr(lang, "Not set", "Sin configurar"),
            inline=False,
        )
        embed.add_field(name=tr(lang, "Color", "Color"), value=f"`{config.color_hex}`", inline=True)
        embed.add_field(
            name=tr(lang, "Variables", "Variables"),
            value="`{user}` `{username}` `{avatar}` `{server}` `{channel}` `{#channel_name}` `{role_name}`",
            inline=False,
        )
        await ctx.send(embed=embed)

    async def _set_common(
        self,
        ctx: commands.Context,
        *,
        kind: str,
        channel: discord.TextChannel | None | object = Ellipsis,
        mode: str | None = None,
        message: str | None = None,
        image: str | None = None,
        color: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(ctx.guild)

        normalized_mode = self._normalize_mode(mode)
        if mode is not None and normalized_mode is None:
            await ctx.send(
                tr(
                    lang,
                    "Invalid mode. Use: text, embed, both.",
                    "Modo invalido. Usa: text, embed, both.",
                )
            )
            return

        normalized_image: str | None = None
        if image is not None:
            normalized_image = self._normalize_image_url(image)
            if normalized_image is None:
                await ctx.send(
                    tr(
                        lang,
                        "Invalid image URL. Use http(s) URL or `clear`.",
                        "URL de imagen invalida. Usa una URL http(s) o `clear`.",
                    )
                )
                return

        normalized_color: str | None = None
        if color is not None:
            normalized_color = self._normalize_hex_color(color)
            if normalized_color is None:
                await ctx.send(
                    tr(
                        lang,
                        "Invalid color. Use hex format like `#00ffaa`.",
                        "Color invalido. Usa formato hex como `#00ffaa`.",
                    )
                )
                return

        channel_update_value: int | None | object = Ellipsis
        if channel is not Ellipsis:
            channel_update_value = channel.id if isinstance(channel, discord.TextChannel) else None

        config = await self.bot.db.update_announcement_settings(
            ctx.guild.id,
            kind,
            channel_id=channel_update_value,
            mode=normalized_mode,
            message_text=message,
            image_url=normalized_image,
            color_hex=normalized_color,
            enabled=enabled,
        )
        await ctx.send(
            tr(
                lang,
                f"{kind.title()} settings updated. Mode: `{config.mode}` | Enabled: `{config.enabled}`",
                f"Configuracion de {kind} actualizada. Modo: `{config.mode}` | Activo: `{config.enabled}`",
            )
        )

    async def _edit_common(
        self,
        ctx: commands.Context,
        *,
        kind: str,
        message: str | None = None,
        color: str | None = None,
        mode: str | None = None,
        image: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        changes = [
            message is not None,
            color is not None,
            mode is not None,
            image is not None,
            channel is not None,
        ]
        selected = sum(1 for value in changes if value)

        if selected == 0:
            await ctx.send(
                tr(
                    lang,
                    "Choose one field to edit: message, color, mode, image, or channel.",
                    "Elige un campo para editar: message, color, mode, image o channel.",
                )
            )
            return

        if selected > 1:
            await ctx.send(
                tr(
                    lang,
                    "Edit one field at a time. Example: `/welcome edit message:<new text>`.",
                    "Edita un solo campo por vez. Ejemplo: `/welcome edit message:<texto nuevo>`.",
                )
            )
            return

        if message is not None:
            await self._set_common(ctx, kind=kind, message=message, enabled=True)
            return
        if color is not None:
            await self._set_common(ctx, kind=kind, color=color, enabled=True)
            return
        if mode is not None:
            await self._set_common(ctx, kind=kind, mode=mode, enabled=True)
            return
        if image is not None:
            await self._set_common(ctx, kind=kind, image=image, enabled=True)
            return

        await self._set_common(
            ctx,
            kind=kind,
            channel=channel if channel is not None else Ellipsis,
            enabled=True,
        )

    @commands.hybrid_group(
        name="welcome",
        description="Configure welcome announcement messages.",
        with_app_command=True,
        fallback="show",
    )
    @commands.check(_slash_only_invocation)
    @commands.has_permissions(manage_guild=True)
    async def welcome_group(self, ctx: commands.Context) -> None:
        await self._show_config(ctx, "welcome")

    @commands.hybrid_group(
        name="goodbye",
        description="Configure goodbye announcement messages.",
        with_app_command=True,
        fallback="show",
    )
    @commands.check(_slash_only_invocation)
    @commands.has_permissions(manage_guild=True)
    async def goodbye_group(self, ctx: commands.Context) -> None:
        await self._show_config(ctx, "goodbye")

    @welcome_group.command(name="set", description="Set welcome settings in one command.")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Announcement channel",
        mode="text, embed, or both",
        message="Custom message template",
        image="Image/GIF URL, or 'clear'",
        color="Hex color (e.g. #00FFAA)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="text", value="text"),
            app_commands.Choice(name="embed", value="embed"),
            app_commands.Choice(name="both", value="both"),
        ]
    )
    async def welcome_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        mode: str | None = None,
        message: str | None = None,
        image: str | None = None,
        color: str | None = None,
    ) -> None:
        await self._set_common(
            ctx,
            kind="welcome",
            channel=channel if channel is not None else Ellipsis,
            mode=mode,
            message=message,
            image=image,
            color=color,
            enabled=True,
        )

    @goodbye_group.command(name="set", description="Set goodbye settings in one command.")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Announcement channel",
        mode="text, embed, or both",
        message="Custom message template",
        image="Image/GIF URL, or 'clear'",
        color="Hex color (e.g. #00FFAA)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="text", value="text"),
            app_commands.Choice(name="embed", value="embed"),
            app_commands.Choice(name="both", value="both"),
        ]
    )
    async def goodbye_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        mode: str | None = None,
        message: str | None = None,
        image: str | None = None,
        color: str | None = None,
    ) -> None:
        await self._set_common(
            ctx,
            kind="goodbye",
            channel=channel if channel is not None else Ellipsis,
            mode=mode,
            message=message,
            image=image,
            color=color,
            enabled=True,
        )

    @welcome_group.command(
        name="edit",
        description="Edit one welcome option directly (message, color, mode, image, channel).",
    )
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message="New welcome message template",
        color="Hex color (e.g. #00FFAA)",
        mode="text, embed, or both",
        image="Image/GIF URL, or `clear`",
        channel="Announcement channel",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="text", value="text"),
            app_commands.Choice(name="embed", value="embed"),
            app_commands.Choice(name="both", value="both"),
        ],
    )
    async def welcome_edit(
        self,
        ctx: commands.Context,
        message: str | None = None,
        color: str | None = None,
        mode: str | None = None,
        image: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._edit_common(
            ctx,
            kind="welcome",
            message=message,
            color=color,
            mode=mode,
            image=image,
            channel=channel,
        )

    @goodbye_group.command(
        name="edit",
        description="Edit one goodbye option directly (message, color, mode, image, channel).",
    )
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message="New goodbye message template",
        color="Hex color (e.g. #00FFAA)",
        mode="text, embed, or both",
        image="Image/GIF URL, or `clear`",
        channel="Announcement channel",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="text", value="text"),
            app_commands.Choice(name="embed", value="embed"),
            app_commands.Choice(name="both", value="both"),
        ],
    )
    async def goodbye_edit(
        self,
        ctx: commands.Context,
        message: str | None = None,
        color: str | None = None,
        mode: str | None = None,
        image: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._edit_common(
            ctx,
            kind="goodbye",
            message=message,
            color=color,
            mode=mode,
            image=image,
            channel=channel,
        )

    @welcome_group.command(name="test", description="Preview the current welcome output.")
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok, status = await self._send_announcement(
            kind="welcome",
            guild=ctx.guild,
            member=ctx.author,
            force_preview=True,
            preview_channel=ctx.channel if isinstance(ctx.channel, discord.abc.Messageable) else None,
        )
        if ok:
            return
        await ctx.send(f"Preview failed: {status}")

    @goodbye_group.command(name="test", description="Preview the current goodbye output.")
    @commands.has_permissions(manage_guild=True)
    async def goodbye_test(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok, status = await self._send_announcement(
            kind="goodbye",
            guild=ctx.guild,
            member=ctx.author,
            force_preview=True,
            preview_channel=ctx.channel if isinstance(ctx.channel, discord.abc.Messageable) else None,
        )
        if ok:
            return
        await ctx.send(f"Preview failed: {status}")

    @welcome_group.command(name="preview", description="Alias for /welcome test.")
    @commands.has_permissions(manage_guild=True)
    async def welcome_preview(self, ctx: commands.Context) -> None:
        await self.welcome_test(ctx)

    @goodbye_group.command(name="preview", description="Alias for /goodbye test.")
    @commands.has_permissions(manage_guild=True)
    async def goodbye_preview(self, ctx: commands.Context) -> None:
        await self.goodbye_test(ctx)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self._send_announcement(kind="welcome", guild=member.guild, member=member)
        except Exception:
            return

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await self._send_announcement(kind="goodbye", guild=member.guild, member=member)
        except Exception:
            return

    @welcome_group.error
    @goodbye_group.error
    @welcome_set.error
    @goodbye_set.error
    @welcome_edit.error
    @goodbye_edit.error
    @welcome_test.error
    @goodbye_test.error
    @welcome_preview.error
    @goodbye_preview.error
    async def announcement_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You do not have permission to use this command.")
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send("This module is slash-only now. Use `/welcome ...` or `/goodbye ...`.")
            return
        await ctx.send(f"Command failed: {error}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AnnouncementCog(bot))
