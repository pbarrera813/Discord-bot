from __future__ import annotations

import discord

from services.database import Database


_CARD_TEXT_LIMIT = 4000
_CARD_COMPONENT_LIMIT = 40


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "... [truncated]"
    return value[:max(0, limit - len(suffix))] + suffix[:limit]


def _card_text(embed: discord.Embed, body: str | None) -> tuple[str, str, str, str, str | None, str]:
    author_name = str(getattr(embed.author, "name", "") or "")
    title = embed.title or author_name or "Log"
    if title == author_name:
        author_name = ""
    title = _truncate(title, 256)
    author_name = _truncate(author_name, 256)
    header = embed.description or ""
    content = body or ""
    if body is None:
        for field in embed.fields:
            content += ("\n\n" if content else "") + f"**{field.name}**\n{field.value}"
    footer = str(getattr(embed.footer, "text", "") or "")
    footer = _truncate(footer, 2048)
    metadata = f"-# {footer}" if footer else ""
    date = ""
    if embed.timestamp is not None:
        date = f"-# <t:{int(embed.timestamp.timestamp())}:f>"
    if len(title) > 256:
        title = title[:244] + "… [truncated]"
    available = max(0, _CARD_TEXT_LIMIT - len(title) - len(author_name) - len(metadata) - len(date) - 32)
    if len(header) > available:
        suffix = "… [truncated]"
        header = header[:max(0, available - len(suffix))] + suffix[:available]
    available -= min(len(header), available)
    if len(content) > available:
        suffix = "… [truncated]"
        content = content[:max(0, available - len(suffix))] + suffix[:available]
    if date:
        metadata += ("\n" if metadata else "") + date
    return title, header, content, metadata, str(getattr(embed.author, "icon_url", "") or "") or None, author_name


async def send_modlog_embed(
    guild: discord.Guild,
    db: Database,
    embed: discord.Embed,
    *,
    view: discord.ui.View | None = None,
    files: list[discord.File] | None = None,
    image_urls: list[str] | None = None,
    body: str | None = None,
) -> discord.Message | None:
    settings = await db.get_guild_settings(guild.id)
    channel_id = settings.modlog_channel_id
    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await guild.fetch_channel(channel_id)
            channel = fetched if isinstance(fetched, discord.abc.Messageable) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    if channel is None:
        return None

    heading, header, content, metadata, author_icon, author_name = _card_text(embed, body)
    card = discord.ui.Container(accent_color=embed.color)
    heading_text = f"**{heading}**" + (f"\n{author_name}" if author_name else "")
    thumb = str(getattr(embed.thumbnail, "url", "") or "")
    accessory_url = author_icon or thumb
    if accessory_url:
        card.add_item(discord.ui.Section(discord.ui.TextDisplay(heading_text), accessory=discord.ui.Thumbnail(accessory_url)))
    else:
        card.add_item(discord.ui.TextDisplay(heading_text))
    if header:
        card.add_item(discord.ui.TextDisplay(header))
    card.add_item(discord.ui.Separator())
    if content:
        card.add_item(discord.ui.TextDisplay(content))

    urls = list(image_urls or [])
    urls.extend(url for url in (getattr(embed.image, "url", None),) if url)
    if thumb and author_icon and thumb != author_icon:
        urls.append(thumb)
    urls = urls[:10]
    if urls:
        uploaded = {f"attachment://{file.filename}": file for file in (files or [])}
        card.add_item(discord.ui.MediaGallery(*[
            discord.MediaGalleryItem(url, spoiler=bool(getattr(uploaded.get(url), "spoiler", False)))
            for url in urls
        ]))
    if metadata:
        card.add_item(discord.ui.TextDisplay(metadata))

    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(card)
    if view is not None:
        if view.children:
            layout.add_item(discord.ui.ActionRow(*view.children))
    if layout.total_children_count > _CARD_COMPONENT_LIMIT:
        return None

    try:
        return await channel.send(
            view=layout,
            files=files or [],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        return None
