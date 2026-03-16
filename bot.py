from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from config import Settings, load_settings
from services.api_ninjas import ApiNinjasClient
from services.api_football import ApiFootballClient
from services.database import Database
from services.glot import GlotClient
from services.modlog import send_modlog_embed
from services.xai_client import XAIClient
from services.memegen import MemeGenClient
from services.mcsrvstat import McSrvStatClient
from utils.i18n import tr


async def dynamic_prefix(bot: "DiscordModBot", message: discord.Message) -> Any:
    default_prefix = bot.settings.default_prefix
    if message.guild is None:
        return commands.when_mentioned_or(default_prefix)(bot, message)

    prefix = default_prefix
    try:
        settings = await bot.db.get_guild_settings(message.guild.id)
        prefix = settings.prefix
    except Exception:
        pass
    return commands.when_mentioned_or(prefix)(bot, message)


class DiscordModBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.moderation = True

        super().__init__(
            command_prefix=dynamic_prefix,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.db = Database(db_path=settings.db_path, default_prefix=settings.default_prefix)
        self.llm_client = XAIClient(settings.xai_api_key, settings.xai_model)
        self.mc_client = McSrvStatClient()
        self.ninjas_client = (
            ApiNinjasClient(settings.api_ninjas_key)
            if settings.api_ninjas_key
            else None
        )
        self.glot_client = (
            GlotClient(
                settings.glot_api_token,
                settings.glot_base_url,
                mode=settings.glot_api_mode,
            )
            if settings.glot_api_token
            else None
        )
        self.api_football_client = (
            ApiFootballClient(
                api_key=settings.api_football_key,
                base_url=settings.api_football_base_url,
            )
            if settings.api_football_key
            else None
        )
        self.memegen_client = MemeGenClient()

    @staticmethod
    def _is_in_global_command_audit_scope(
        *,
        command_name: str,
        cog_name: str | None = None,
        module_name: str | None = None,
    ) -> bool:
        allowed_cogs = {"NinjasCog", "FootballCog", "CodeRunnerCog"}
        allowed_modules = ("cogs.ninjas", "cogs.football", "cogs.code_runner")
        if cog_name in allowed_cogs:
            return True
        if isinstance(module_name, str) and module_name.startswith(allowed_modules):
            return True

        # Fallback by known command families when command metadata is partial.
        normalized = (command_name or "").strip().casefold()
        allowed_prefixes = (
            "joke",
            "dadjoke",
            "advice",
            "whois",
            "convert",
            "football",
            "ligamx",
            "run",
        )
        return normalized.startswith(allowed_prefixes)

    async def _guild_lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _format_app_command_options(data: dict[str, Any] | None) -> str:
        if not isinstance(data, dict):
            return ""
        raw_options = data.get("options")
        if not isinstance(raw_options, list):
            return ""

        parts: list[str] = []

        def walk(options: list[dict[str, Any]], prefix: str = "") -> None:
            for option in options:
                if not isinstance(option, dict):
                    continue
                name = option.get("name")
                if not isinstance(name, str) or not name:
                    continue
                scoped_name = f"{prefix}{name}"
                children = option.get("options")
                if isinstance(children, list) and children:
                    walk(children, f"{scoped_name}.")
                    continue
                value = option.get("value")
                if value is None:
                    continue
                rendered = str(value).strip()
                if not rendered:
                    continue
                parts.append(f"{scoped_name}={rendered}")

        walk(raw_options)
        rendered = " | ".join(parts)
        if len(rendered) > 1000:
            return f"{rendered[:997]}..."
        return rendered

    async def _send_command_audit_log(
        self,
        *,
        guild: discord.Guild | None,
        actor: discord.abc.User | discord.Member | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
        command_name: str,
        command_input: str | None = None,
        command_source: str = "prefix",
    ) -> None:
        if guild is None or actor is None:
            return

        lang = await self._guild_lang(guild)
        embed = discord.Embed(
            title=tr(lang, "Command used", "Comando usado"),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name=tr(lang, "User", "Usuario"),
            value=f"{actor.mention} (`{actor.id}`)",
            inline=True,
        )
        channel_text = (
            channel.mention
            if isinstance(channel, (discord.TextChannel, discord.Thread))
            else tr(lang, "Unknown", "Desconocido")
        )
        embed.add_field(
            name=tr(lang, "Channel", "Canal"),
            value=channel_text,
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Command", "Comando"),
            value=f"`{command_name}`",
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Type", "Tipo"),
            value=command_source,
            inline=True,
        )
        if command_input:
            embed.add_field(
                name=tr(lang, "Input", "Entrada"),
                value=command_input[:1024],
                inline=False,
            )
        await send_modlog_embed(guild, self.db, embed)

    async def setup_hook(self) -> None:
        await self.db.init()
        for extension in (
            "cogs.admin",
            "cogs.announcements",
            "cogs.moderation",
            "cogs.minecraft",
            "cogs.filters",
            "cogs.ai_chat",
            "cogs.fun",
            "cogs.ninjas",
            "cogs.code_runner",
            "cogs.football",
            "cogs.memes",
        ):
            await self.load_extension(extension)

        try:
            await self.tree.sync()
        except Exception as exc:
            logging.warning("Failed to sync app commands: %s", exc)

    async def close(self) -> None:
        await self.llm_client.close()
        await self.mc_client.close()
        if self.ninjas_client:
            await self.ninjas_client.close()
        if self.glot_client:
            await self.glot_client.close()
        if self.api_football_client:
            await self.api_football_client.close()
        await self.memegen_client.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user:
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name="Laufey",
            )
            await self.change_presence(
                status=discord.Status.online,
                activity=activity,
            )
            logging.info("Logged in as %s (%s)", self.user.name, self.user.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.get_or_create_guild_settings(guild.id)

    async def on_command_completion(self, ctx: commands.Context) -> None:
        command = getattr(ctx, "command", None)
        if command is None:
            return
        if ctx.interaction is not None:
            # Slash/hybrid slash usage is handled in on_app_command_completion.
            return

        command_name = command.qualified_name
        cog_name = None
        cog = getattr(command, "cog", None)
        if cog is not None:
            cog_name = type(cog).__name__
        module_name = getattr(command, "module", None)
        if not self._is_in_global_command_audit_scope(
            command_name=command_name,
            cog_name=cog_name,
            module_name=module_name if isinstance(module_name, str) else None,
        ):
            return
        command_input = None
        if ctx.message and ctx.message.content:
            command_input = ctx.message.content.strip()
        await self._send_command_audit_log(
            guild=ctx.guild,
            actor=ctx.author,
            channel=ctx.channel if isinstance(ctx.channel, (discord.abc.GuildChannel, discord.Thread)) else None,
            command_name=command_name,
            command_input=command_input,
            command_source="prefix",
        )

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: discord.app_commands.Command[Any, Any, Any]
        | discord.app_commands.ContextMenu,
    ) -> None:
        command_name = getattr(command, "qualified_name", None) or getattr(command, "name", "unknown")
        binding = getattr(command, "binding", None)
        cog_name = type(binding).__name__ if binding is not None else None
        module_name = getattr(command, "module", None)
        if not self._is_in_global_command_audit_scope(
            command_name=str(command_name),
            cog_name=cog_name,
            module_name=module_name if isinstance(module_name, str) else None,
        ):
            return
        options_text = self._format_app_command_options(interaction.data if isinstance(interaction.data, dict) else None)
        await self._send_command_audit_log(
            guild=interaction.guild,
            actor=interaction.user,
            channel=interaction.channel if isinstance(interaction.channel, (discord.abc.GuildChannel, discord.Thread)) else None,
            command_name=str(command_name),
            command_input=options_text or None,
            command_source="slash",
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        async def safe_send(message: str) -> None:
            interaction = getattr(ctx, "interaction", None)
            if interaction is not None:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(message, ephemeral=True)
                    else:
                        await interaction.followup.send(message, ephemeral=True)
                    return
                except (discord.NotFound, discord.HTTPException):
                    pass

            channel = getattr(ctx, "channel", None)
            if channel is None:
                return
            try:
                await channel.send(message)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await safe_send(f"Missing argument: `{error.param.name}`.")
            return
        if isinstance(error, commands.BadArgument):
            await safe_send("Invalid argument provided.")
            return
        await safe_send(f"Command error: {error}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    settings = load_settings()
    bot = DiscordModBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
