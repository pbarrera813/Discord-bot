from __future__ import annotations

from discord.ext import commands


def is_bot_owner_id(bot: commands.Bot, user_id: int) -> bool:
    owner_ids = getattr(getattr(bot, "settings", None), "bot_owner_ids", ())
    return user_id in owner_ids


def owner_or_has_permissions(**perms: bool):
    base_predicate = commands.has_permissions(**perms).predicate

    async def predicate(ctx: commands.Context) -> bool:
        author = getattr(ctx, "author", None)
        if author is not None and is_bot_owner_id(ctx.bot, int(author.id)):
            return True
        return await base_predicate(ctx)

    return commands.check(predicate)
