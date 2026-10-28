from __future__ import annotations

import asyncio
import logging
import time

import discord

from services.database import Database


_CARD_TEXT_LIMIT = 4000
_CARD_COMPONENT_LIMIT = 40

_SPANISH_LABELS = {
    "Moderator": "Responsable", "Target": "Destino", "Reason": "Motivo",
    "Details": "Detalles", "User": "Usuario", "User ID": "ID de usuario",
    "Action": "Acción", "Channel name": "Canal",
}
_SPANISH_TITLES = {
    "Channel Created": "Canal creado", "Channel Deleted": "Canal eliminado",
    "Channel Cloned": "Canal clonado", "Channel Cleared": "Canal vaciado",
    "Channel Locked": "Canal bloqueado", "Channel Unlocked": "Canal desbloqueado",
    "Messages Deleted": "Mensajes eliminados", "User Messages Purged": "Mensajes de usuario eliminados",
    "Role Created": "Rol creado", "Role Deleted": "Rol eliminado",
    "Role Added": "Rol asignado", "Role Removed": "Rol retirado",
    "User Kicked": "Usuario expulsado", "User Banned": "Usuario baneado",
    "User Temporarily Banned": "Usuario baneado temporalmente", "User Unbanned": "Baneo retirado",
    "User Muted": "Usuario silenciado", "User Temporarily Muted": "Usuario silenciado temporalmente",
    "User Timed Out": "Usuario aislado temporalmente", "User Unmuted": "Silencio retirado",
    "User Warned": "Advertencia registrada", "Warning Removed": "Advertencia retirada",
    "Warnings Cleared": "Advertencias borradas", "User Info Lookup": "Consulta de usuario",
    "Slowmode Updated": "Modo lento actualizado", "Nickname Updated": "Apodo actualizado",
    "Temporary Mute Expired": "Silencio temporal finalizado", "Temporary Ban Expired": "Baneo temporal finalizado",
    "Color Roles Setup": "Roles de color configurados", "Color Panel Published": "Panel de colores publicado",
    "Color Panel Reloaded": "Panel de colores actualizado", "Color Role Added": "Color asignado",
    "Color Role Removed": "Color retirado",
}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "... [truncated]"
    return value[:max(0, limit - len(suffix))] + suffix[:limit]


def _card_text(embed: discord.Embed, body: str | None, lang: str = "en") -> tuple[str, str, str, str, str | None, str]:
    author_name = str(getattr(embed.author, "name", "") or "")
    title = embed.title or author_name or "Log"
    if title == author_name:
        author_name = ""
    if lang == "es":
        title = _SPANISH_TITLES.get(title, title)
    title = _truncate(title, 256)
    author_name = _truncate(author_name, 256)
    header = embed.description or ""
    content = body or ""
    if body is None:
        for field in embed.fields:
            name = _SPANISH_LABELS.get(field.name, field.name) if lang == "es" else field.name
            compact = field.inline and "\n" not in field.value
            content += ("\n" if compact else "\n\n") if content else ""
            content += f"**{name}:** {field.value}" if compact else f"**{name}**\n{field.value}"
    footer = str(getattr(embed.footer, "text", "") or "")
    footer = _truncate(footer, 2048)
    metadata = "\n".join(f"-# {line}" for line in footer.replace(" | ", "\n").splitlines())
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
    event_key: tuple[str, int] | None = None,
    event_enriched: bool = False,
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

    heading, header, content, metadata, author_icon, author_name = _card_text(embed, body, getattr(settings, "language_code", "en"))
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
    if content:
        card.add_item(discord.ui.Separator())
        card.add_item(discord.ui.TextDisplay(content))

    urls = list(image_urls or [])
    urls.extend(url for url in (getattr(embed.image, "url", None),) if url)
    if thumb and author_icon and thumb != author_icon:
        urls.append(thumb)
    urls = urls[:10]
    if urls:
        if not content:
            card.add_item(discord.ui.Separator())
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

    async def deliver(message: discord.Message | None = None) -> discord.Message | None:
        try:
            if message is not None:
                return await message.edit(view=layout, allowed_mentions=discord.AllowedMentions.none())
            return await channel.send(view=layout, files=files or [], allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            logging.warning("Modlog delivery failed guild_id=%s event_key=%s editing=%s", guild.id, event_key, message is not None)
            return None

    if event_key is None:
        return await deliver()

    # REST completion and Gateway events can arrive in either order. Only the
    # same resource/event may share a card; an action report enriches that card.
    if not hasattr(db, "_modlog_event_lock"):
        db._modlog_event_lock = asyncio.Lock()
        db._modlog_events = {}
    async with db._modlog_event_lock:
        now = time.monotonic()
        for key, (expires, _, _) in list(db._modlog_events.items()):
            if expires <= now:
                del db._modlog_events[key]
        key = (guild.id, *event_key)
        previous = db._modlog_events.get(key)
        if previous is not None and (not event_enriched or previous[2]):
            return previous[1]
        sent = await deliver(previous[1] if previous else None)
        if sent is not None:
            db._modlog_events[key] = (now + 300, sent, event_enriched)
            while len(db._modlog_events) > 512:
                del db._modlog_events[next(iter(db._modlog_events))]
        return sent
