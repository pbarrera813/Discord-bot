from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from services.server_memory import ServerMemoryService

LORE_TYPES = frozenset({"SERVER_FACT", "INSIDE_JOKE"})
LORE_COOLDOWN_SECONDS = 1800
GLOBAL_TYPES = ("SERVER_RULE", "BOT_BEHAVIOR_RULE")


class ServerMemoryContextBuilder:
    def __init__(self, service: ServerMemoryService) -> None:
        self.service = service
        # ponytail: per process, so a restart allows one extra callback; persist it only if that ever matters.
        self._lore_last_used: dict[tuple[int, int | None, int], float] = {}

    async def build_context(
        self,
        *,
        guild_id: int,
        channel_id: int | None = None,
        author_user_id: int | None = None,
        mentioned_user_ids: list[int] | None = None,
        replied_user_id: int | None = None,
        participant_user_ids: list[int] | None = None,
        user_label: Callable[[int], str | None] | None = None,
        route_action: str = "CHAT",
        current_text: str = "",
        limit: int = 12,
    ) -> str:
        effective_limit = min(max(int(limit), 0), 12)
        memories, query_capped = await self.find_relevant_memories(
            guild_id=guild_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            mentioned_user_ids=mentioned_user_ids or [],
            replied_user_id=replied_user_id,
            participant_user_ids=participant_user_ids or [],
            limit=effective_limit + 1,
        )
        now = time.monotonic()
        memories = [
            row for row in memories
            if not self._cooling_down(guild_id, channel_id, row, now)
        ]
        omitted = len(memories) > effective_limit
        memories = memories[:effective_limit]
        for row in memories:
            if self._is_lore(row):
                self._lore_last_used[self._lore_key(guild_id, channel_id, row)] = now
        return self.format_memory_context(
            memories,
            omitted=omitted,
            query_capped=query_capped,
            user_label=user_label,
        )

    def _cooling_down(self, guild_id: int, channel_id: int | None, row: dict[str, Any], now: float) -> bool:
        last_used = self._lore_last_used.get(self._lore_key(guild_id, channel_id, row))
        return self._is_lore(row) and last_used is not None and now - last_used < LORE_COOLDOWN_SECONDS

    @staticmethod
    def _is_lore(row: dict[str, Any]) -> bool:
        return row.get("memory_type") in LORE_TYPES and row.get("subject_user_id") is not None

    @staticmethod
    def _lore_key(guild_id: int, channel_id: int | None, row: dict[str, Any]) -> tuple[int, int | None, int]:
        return guild_id, channel_id, int(row.get("id", 0))

    async def find_relevant_memories(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        author_user_id: int | None,
        mentioned_user_ids: list[int],
        replied_user_id: int | None,
        participant_user_ids: list[int] | None = None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        relevant: list[dict[str, Any]] = []
        seen: set[int] = set()
        query_capped = False
        user_ids = {user_id for user_id in [*mentioned_user_ids, *(participant_user_ids or [])] if user_id}
        if replied_user_id:
            user_ids.add(replied_user_id)
        allowed_user_ids = set(user_ids)
        if author_user_id:
            allowed_user_ids.add(author_user_id)

        def in_scope(row: dict[str, Any]) -> bool:
            try:
                if int(row.get("guild_id", 0)) != guild_id:
                    return False
            except (TypeError, ValueError):
                return False
            if str(row.get("status", "")).casefold() != "active":
                return False
            user_id = row.get("subject_user_id")
            if user_id is not None:
                try:
                    if int(user_id) not in allowed_user_ids:
                        return False
                except (TypeError, ValueError):
                    return False
            scoped_channel_id = row.get("subject_channel_id")
            if scoped_channel_id is not None:
                try:
                    if channel_id is None or int(scoped_channel_id) != channel_id:
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        async def add(rows: list[dict[str, Any]], *, global_only: bool = False) -> None:
            nonlocal query_capped
            query_capped = query_capped or len(rows) >= 500
            for row in rows:
                memory_id = int(row.get("id", 0))
                if (
                    memory_id
                    and memory_id not in seen
                    and in_scope(row)
                    and (not global_only or (row.get("subject_user_id") is None and row.get("subject_channel_id") is None))
                ):
                    seen.add(memory_id)
                    relevant.append(row)

        if author_user_id:
            await add(
                await self.service.list_memories(
                    guild_id,
                    status="active",
                    subject_user_id=author_user_id,
                    limit=500,
                )
            )
        for user_id in user_ids:
            await add(
                await self.service.list_memories(
                    guild_id,
                    status="active",
                    subject_user_id=user_id,
                    limit=500,
                )
            )

        if channel_id is not None:
            await add(
                await self.service.list_memories(
                    guild_id,
                    status="active",
                    subject_channel_id=channel_id,
                    limit=500,
                )
            )

        for memory_type in GLOBAL_TYPES:
            await add(
                await self.service.list_memories(
                    guild_id,
                    memory_type=memory_type,
                    status="active",
                    limit=500,
                ),
                global_only=True,
            )

        return relevant[:limit], query_capped

    @staticmethod
    def format_memory_context(
        memories: list[dict[str, Any]],
        *,
        omitted: bool = False,
        query_capped: bool = False,
        user_label: Callable[[int], str | None] | None = None,
    ) -> str:
        lines: list[str] = []
        for memory in memories:
            memory_type = str(memory.get("memory_type", "SERVER_FACT"))
            value = " ".join(str(memory.get("value", "")).split())[:300]
            if not value:
                continue
            user_id = memory.get("subject_user_id")
            channel_id = memory.get("subject_channel_id")
            key = str(memory.get("key", "")).replace("_", " ")
            if user_id:
                label = user_label(int(user_id)) if user_label else None
                lines.append(f"About {label} (<@{user_id}>) {key}: {value}" if label else f"User {user_id} {key}: {value}")
            elif channel_id:
                lines.append(f"Channel {channel_id} {key}: {value}")
            else:
                lines.append(f"{memory_type.replace('_', ' ').title()} {key}: {value}")
        if not lines and not omitted and not query_capped:
            return ""
        lines = lines[:12]
        if omitted:
            lines.append("Additional eligible memories omitted by the context limit.")
        if query_capped:
            lines.append("Eligible memories may be omitted by the bounded source query.")
        return "[TRUSTED_SERVER_MEMORY]\n" + "\n".join(lines) + "\n[/TRUSTED_SERVER_MEMORY]"
