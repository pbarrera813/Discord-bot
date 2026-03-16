from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from utils.i18n import tr
from utils.unit_conversion import (
    collect_conversion_units,
    cleanup_target_unit_input,
    extract_target_conversion,
    normalize_requested_unit,
)


class NinjasCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _client_or_message(self, ctx: commands.Context) -> Any | None:
        lang = await self._lang(ctx.guild)
        client = getattr(self.bot, "ninjas_client", None)
        if client is not None:
            return client
        await ctx.send(
            tr(
                lang,
                "API Ninjas is not configured. Add `API_NINJAS_KEY` in `.env`.",
                "API Ninjas no está configurada. Agrega `API_NINJAS_KEY` en `.env`.",
            )
        )
        return None

    async def _maybe_defer(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            return
        if ctx.interaction.response.is_done():
            return
        try:
            await ctx.defer()
        except (discord.NotFound, discord.HTTPException):
            return

    @commands.hybrid_command(name="joke", description="Get a random joke.")
    async def joke(self, ctx: commands.Context) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)

        try:
            joke_text = await client.get_joke()
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get joke: {exc}",
                    f"No se pudo obtener un chiste: {exc}",
                )
            )
            return

        await ctx.send(joke_text)

    @commands.hybrid_command(name="dadjoke", description="Get a random dad joke.")
    async def dadjoke(self, ctx: commands.Context) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)

        try:
            joke_text = await client.get_dadjoke()
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get dad joke: {exc}",
                    f"No se pudo obtener un chiste de papa: {exc}",
                )
            )
            return

        await ctx.send(joke_text)

    @commands.hybrid_command(name="advice", description="Get random advice.")
    async def advice(self, ctx: commands.Context) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)

        try:
            advice_text = await client.get_advice()
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get advice: {exc}",
                    f"No se pudo obtener un consejo: {exc}",
                )
            )
            return

        await ctx.send(advice_text)

    @commands.hybrid_command(name="whois", description="Get domain WHOIS info.")
    async def whois(self, ctx: commands.Context, domain: str) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)

        domain = domain.strip()
        if not domain:
            await ctx.send(
                tr(
                    lang,
                    "Please provide a domain.",
                    "Por favor proporciona un dominio.",
                )
            )
            return

        try:
            data = await client.whois(domain)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to get whois data: {exc}",
                    f"No se pudo obtener informacion whois: {exc}",
                )
            )
            return

        embed = discord.Embed(
            title=tr(lang, f"WHOIS: {domain}", f"WHOIS: {domain}"),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        registrar = str(data.get("registrar", "N/A"))
        creation_date = str(data.get("creation_date", "N/A"))
        expiration_date = str(data.get("expiration_date", "N/A"))
        country = str(data.get("country", "N/A"))

        embed.add_field(name=tr(lang, "Registrar", "Registrador"), value=registrar[:1024], inline=False)
        embed.add_field(
            name=tr(lang, "Creation Date", "Fecha de creacion"),
            value=creation_date[:1024],
            inline=True,
        )
        embed.add_field(
            name=tr(lang, "Expiration Date", "Fecha de expiracion"),
            value=expiration_date[:1024],
            inline=True,
        )
        embed.add_field(name=tr(lang, "Country", "Pais"), value=country[:1024], inline=True)

        nameservers = data.get("name_servers")
        if isinstance(nameservers, list) and nameservers:
            joined = ", ".join(str(ns) for ns in nameservers[:12])
            embed.add_field(
                name=tr(lang, "Name Servers", "Servidores de nombres"),
                value=joined[:1024],
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="convert",
        description="Convert units. Example: /convert 1000 meter kilometer",
    )
    async def convert(
        self,
        ctx: commands.Context,
        amount: float,
        from_unit: str,
        *,
        to_unit: str,
    ) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)
        await self._maybe_defer(ctx)

        source_unit = normalize_requested_unit(from_unit)
        target_input = cleanup_target_unit_input(to_unit)
        target_unit = normalize_requested_unit(target_input)

        if not source_unit or not target_unit:
            await ctx.send(
                tr(
                    lang,
                    "Please provide valid source and target units.",
                    "Por favor proporciona unidades de origen y destino válidas.",
                )
            )
            return

        try:
            data = await client.unit_conversion(amount, source_unit)
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Conversion failed: {exc}",
                    f"La conversión falló: {exc}",
                )
            )
            return

        converted_value, matched_unit = extract_target_conversion(data, target_unit)
        if converted_value is None:
            available_units = collect_conversion_units(data)
            available_preview = ", ".join(available_units[:12])
            await ctx.send(
                tr(
                    lang,
                    (
                        "I couldn't find that target unit in conversion results. "
                        f"Try another target unit (requested: `{target_input}`)."
                        + (
                            f" Available: `{available_preview}`."
                            if available_preview
                            else ""
                        )
                    ),
                    (
                        "No pude encontrar esa unidad de destino en los resultados de conversión. "
                        f"Intenta otra unidad (solicitada: `{target_input}`)."
                        + (
                            f" Disponibles: `{available_preview}`."
                            if available_preview
                            else ""
                        )
                    ),
                )
            )
            return

        source_label = from_unit.strip()
        target_label = target_input.strip() or (matched_unit or target_unit)
        await ctx.send(
            tr(
                lang,
                f"Conversion result: `{amount:g} {source_label} = {converted_value:g} {target_label}`",
                f"Resultado de conversión: `{amount:g} {source_label} = {converted_value:g} {target_label}`",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NinjasCog(bot))
