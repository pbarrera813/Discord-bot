from __future__ import annotations

import discord

from services.database import Database


async def send_modlog_embed(
    guild: discord.Guild,
    db: Database,
    embed: discord.Embed,
    *,
    view: discord.ui.View | None = None,
) -> None:
    settings = await db.get_guild_settings(guild.id)
    channel_id = settings.modlog_channel_id
    if channel_id is None:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await guild.fetch_channel(channel_id)
            channel = fetched if isinstance(fetched, discord.abc.Messageable) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None

    if channel is None:
        return

    try:
        await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        return
