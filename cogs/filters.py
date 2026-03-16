from __future__ import annotations

import re
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands


class FiltersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._history: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._link_regex = re.compile(r"(https?://\S+|discord\.gg/\S+|www\.\S+)", re.IGNORECASE)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        # Staff with manage_messages bypass automatic filters.
        if message.author.guild_permissions.manage_messages:
            return

        filters = await self.bot.db.get_or_create_filter_settings(message.guild.id)

        if int(filters.get("anti_link_enabled", 0)) == 1:
            if self._link_regex.search(message.content or ""):
                await self._handle_anti_link(message)
                return

        if int(filters.get("anti_spam_enabled", 1)) == 1:
            await self._handle_anti_spam(
                message,
                threshold=int(filters.get("spam_threshold", 5)),
                window_seconds=int(filters.get("spam_window_seconds", 8)),
            )

    async def _handle_anti_link(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return

        try:
            await message.channel.send(
                f"{message.author.mention}, links are not allowed here.",
                delete_after=6,
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _handle_anti_spam(
        self, message: discord.Message, *, threshold: int, window_seconds: int
    ) -> None:
        key = (message.guild.id, message.author.id)
        now = time.monotonic()

        queue = self._history[key]
        queue.append(now)

        while queue and (now - queue[0]) > window_seconds:
            queue.popleft()

        if len(queue) < threshold:
            return

        queue.clear()
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return

        try:
            await message.channel.send(
                f"{message.author.mention}, slow down. Anti-spam triggered.",
                delete_after=6,
            )
        except (discord.Forbidden, discord.HTTPException):
            return


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FiltersCog(bot))
