from __future__ import annotations

import discord

from services.database import Database

WARNING_ROLE_NAMES = {
    1: "Warning 1",
    2: "Warning 2",
    3: "Warning 3",
}


async def ensure_muted_role(
    guild: discord.Guild,
    db: Database,
    *,
    reason: str = "Ensure muted role exists",
) -> discord.Role:
    settings = await db.get_guild_settings(guild.id)
    role: discord.Role | None = None

    if settings.muted_role_id is not None:
        role = guild.get_role(settings.muted_role_id)

    if role is None:
        role = discord.utils.get(guild.roles, name="Muted")
        if role is None:
            role = await guild.create_role(
                name="Muted",
                permissions=discord.Permissions.none(),
                reason=reason,
            )
        await db.set_muted_role(guild.id, role.id)

    me = guild.me
    if me is not None and me.top_role.position > 1:
        target_position = me.top_role.position - 1
        if role.position != target_position:
            try:
                await role.edit(position=target_position, reason=reason)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

    await _apply_muted_overwrites(guild, role, reason=reason)
    return role


async def ensure_warning_roles(
    guild: discord.Guild, *, reason: str = "Ensure warning roles exist"
) -> dict[int, discord.Role]:
    roles: dict[int, discord.Role] = {}
    for level, role_name in WARNING_ROLE_NAMES.items():
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(name=role_name, reason=reason)
        roles[level] = role
    return roles


async def _apply_muted_overwrites(
    guild: discord.Guild, role: discord.Role, *, reason: str
) -> None:
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        overwrite.add_reactions = False
        overwrite.speak = False
        overwrite.connect = False

        try:
            await channel.set_permissions(role, overwrite=overwrite, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            continue


def parse_user_id_from_text(text: str) -> int | None:
    cleaned = text.strip()
    if cleaned.startswith("<@") and cleaned.endswith(">"):
        cleaned = cleaned[2:-1]
        if cleaned.startswith("!"):
            cleaned = cleaned[1:]
    return int(cleaned) if cleaned.isdigit() else None
