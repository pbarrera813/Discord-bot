from __future__ import annotations

import base64
import io
import re
from datetime import datetime, timezone
from urllib.parse import quote

import discord
from discord.ext import commands

from services.modlog import send_modlog_embed
from utils.i18n import tr


class MinecraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _extract_raw_error(data: dict) -> str:
        raw_parts: list[str] = []
        for key in ("ping", "query"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                raw_parts.append(value.strip())

        debug_data = data.get("debug", {})
        if isinstance(debug_data, dict):
            debug_error = debug_data.get("error")
            if isinstance(debug_error, str) and debug_error.strip():
                raw_parts.append(debug_error.strip())

        return " | ".join(raw_parts)

    def _human_readable_error(self, data: dict, lang: str) -> str:
        raw_error = self._extract_raw_error(data)
        lowered = raw_error.lower()

        if any(token in lowered for token in ("connection refused", "failed to connect", "no route to host")):
            return tr(
                lang,
                "Failed to connect to the server (it may be offline).",
                "No se pudo conectar al servidor (puede estar apagado).",
            )
        if any(token in lowered for token in ("timed out", "timeout")):
            return tr(
                lang,
                "Connection timed out while reaching the server.",
                "La conexión expiró al intentar llegar al servidor.",
            )
        if any(
            token in lowered
            for token in ("name or service not known", "getaddrinfo", "no address associated", "nxdomain")
        ):
            return tr(
                lang,
                "The server address looks invalid or DNS could not resolve it.",
                "La dirección del servidor parece inválida o el DNS no pudo resolverla.",
            )
        if raw_error:
            return tr(
                lang,
                "The server appears offline or unreachable right now.",
                "El servidor parece estar apagado o inaccesible en este momento.",
            )
        return tr(
            lang,
            "The server is offline.",
            "El servidor está apagado.",
        )

    @staticmethod
    def _icon_url(address: str) -> str:
        safe_address = quote(address.strip(), safe=":.")
        return f"https://api.mcsrvstat.us/icon/{safe_address}"

    @staticmethod
    def _extract_inline_icon_bytes(data: dict) -> bytes | None:
        for key in ("icon", "favicon"):
            raw = data.get(key)
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value:
                continue
            match = re.match(r"^data:image/[^;]+;base64,(.+)$", value, flags=re.IGNORECASE)
            if not match:
                continue
            encoded = match.group(1).strip()
            try:
                padding = "=" * (-len(encoded) % 4)
                return base64.b64decode(encoded + padding, validate=False)
            except Exception:
                continue
        return None

    @commands.hybrid_command(
        name="srvstatus",
        description="Check Minecraft server status using mcsrvstat API.",
    )
    async def srvstatus(self, ctx: commands.Context, ip: str) -> None:
        lang = await self._lang(ctx.guild)
        ip = ip.strip()
        if not ip:
            await ctx.send(
                tr(
                    lang,
                    "Please provide a valid IP or domain.",
                    "Por favor proporciona una IP o dominio válido.",
                )
            )
            return

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

        try:
            data = await self.bot.mc_client.get_status(ip)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to query server status: {exc}",
                    f"No se pudo consultar el estado del servidor: {exc}",
                )
            )
            return

        online = bool(data.get("online"))
        embed = discord.Embed(
            title=tr(
                lang,
                f"Minecraft Server Status: {ip}",
                f"Estado del servidor de Minecraft: {ip}",
            ),
            color=discord.Color.green() if online else discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name=tr(lang, "Online", "En línea"),
            value=tr(lang, "Yes" if online else "No", "Sí" if online else "No"),
            inline=True,
        )
        embed.add_field(
            name="IP",
            value=f"{data.get('ip', 'N/A')}:{data.get('port', 'N/A')}",
            inline=True,
        )

        if online:
            players = data.get("players", {})
            version = data.get("version", "Unknown")
            motd = ""
            motd_data = data.get("motd", {})
            if isinstance(motd_data, dict):
                clean = motd_data.get("clean")
                if isinstance(clean, list):
                    motd = " | ".join(clean).strip()
            embed.add_field(name=tr(lang, "Version", "Versión"), value=str(version), inline=True)
            embed.add_field(
                name=tr(lang, "Players", "Jugadores"),
                value=f"{players.get('online', 0)}/{players.get('max', 0)}",
                inline=True,
            )
            if motd:
                embed.add_field(name=tr(lang, "MOTD", "MOTD"), value=motd[:1024], inline=False)
        else:
            human_error = self._human_readable_error(data, lang)
            raw_error = self._extract_raw_error(data)
            embed.add_field(
                name=tr(lang, "Status", "Estado"),
                value=human_error[:1024],
                inline=False,
            )
            if raw_error:
                embed.add_field(
                    name=tr(lang, "Technical details", "Detalles técnicos"),
                    value=raw_error[:1024],
                    inline=False,
                )

        if online:
            inline_icon = self._extract_inline_icon_bytes(data)
            if inline_icon:
                icon_file = discord.File(io.BytesIO(inline_icon), filename="server_icon.png")
                embed.set_thumbnail(url="attachment://server_icon.png")
                await ctx.send(embed=embed, file=icon_file)
            else:
                embed.set_thumbnail(url=self._icon_url(ip))
                await ctx.send(embed=embed)
        else:
            await ctx.send(embed=embed)

        if ctx.guild:
            log_embed = discord.Embed(
                title=tr(lang, "Minecraft Status Check", "Revisión de estado de Minecraft"),
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            log_embed.add_field(name=tr(lang, "Moderator", "Moderador"), value=ctx.author.mention, inline=True)
            log_embed.add_field(name=tr(lang, "Query", "Consulta"), value=ip, inline=True)
            log_embed.add_field(
                name=tr(lang, "Result", "Resultado"),
                value=tr(lang, "Online" if online else "Offline", "En línea" if online else "Fuera de línea"),
                inline=True,
            )
            await send_modlog_embed(ctx.guild, self.bot.db, log_embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))
