from __future__ import annotations

import re
from typing import Any

from services.server_memory import ServerMemoryService


class ServerMemoryContextBuilder:
    def __init__(self, service: ServerMemoryService) -> None:
        self.service = service

    async def build_context(
        self,
        *,
        guild_id: int,
        channel_id: int | None = None,
        author_user_id: int | None = None,
        mentioned_user_ids: list[int] | None = None,
        replied_user_id: int | None = None,
        route_action: str = "CHAT",
        current_text: str = "",
        limit: int = 12,
    ) -> str:
        memories = await self.find_relevant_memories(
            guild_id=guild_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            mentioned_user_ids=mentioned_user_ids or [],
            replied_user_id=replied_user_id,
            route_action=route_action,
            current_text=current_text,
            limit=limit,
        )
        return self.format_memory_context(memories)

    async def find_relevant_memories(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        author_user_id: int | None,
        mentioned_user_ids: list[int],
        replied_user_id: int | None,
        route_action: str,
        current_text: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        relevant: list[dict[str, Any]] = []
        seen: set[int] = set()

        async def add(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                memory_id = int(row.get("id", 0))
                if memory_id and memory_id not in seen:
                    seen.add(memory_id)
                    relevant.append(row)

        user_ids = {user_id for user_id in mentioned_user_ids if user_id}
        if replied_user_id:
            user_ids.add(replied_user_id)
        if author_user_id:
            await add(await self.service.list_user_memories(guild_id, author_user_id))
        for user_id in user_ids:
            await add(await self.service.list_user_memories(guild_id, user_id))

        if channel_id is not None:
            await add(
                await self.service.list_memories(
                    guild_id,
                    status="active",
                    subject_channel_id=channel_id,
                    limit=limit,
                )
            )

        global_types = ["SERVER_FACT", "SERVER_RULE", "BOT_BEHAVIOR_RULE", "INSIDE_JOKE"]
        if route_action.startswith("FOOTBALL_"):
            global_types.append("FOOTBALL_PREFERENCE")
        text_tokens = set(self._tokens(current_text))
        for memory_type in global_types:
            rows = await self.service.list_memories(guild_id, memory_type=memory_type, status="active", limit=limit)
            matched = [
                row for row in rows
                if memory_type in {"SERVER_RULE", "BOT_BEHAVIOR_RULE", "FOOTBALL_PREFERENCE"}
                or str(row.get("key", "")).casefold() in text_tokens
                or any(token in text_tokens for token in self._tokens(str(row.get("value", ""))))
            ]
            await add(matched)

        return relevant[:limit]

    @staticmethod
    def format_memory_context(memories: list[dict[str, Any]]) -> str:
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
                lines.append(f"User {user_id} {key}: {value}")
            elif channel_id:
                lines.append(f"Channel {channel_id} {key}: {value}")
            else:
                lines.append(f"{memory_type.replace('_', ' ').title()} {key}: {value}")
        if not lines:
            return ""
        return "[TRUSTED_SERVER_MEMORY]\n" + "\n".join(lines[:12]) + "\n[/TRUSTED_SERVER_MEMORY]"

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return re.findall(r"[a-zA-Z0-9áéíóúñüÁÉÍÓÚÑÜ]{3,}", value.casefold())

