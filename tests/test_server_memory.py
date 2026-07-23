from __future__ import annotations

import os
import tempfile
import unittest

from services.database import Database
from services.server_memory import ServerMemoryInput, ServerMemoryService
from services.server_memory_context import ServerMemoryContextBuilder


class ServerMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db_path = os.path.join(tempfile.gettempdir(), f"discordbot_test_server_memory_{self._testMethodName}.db")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)
        await self.db.init()
        self.service = ServerMemoryService(self.db)

    async def test_create_update_archive_and_no_cross_guild_leakage(self) -> None:
        row = await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="USER_NICKNAME",
                subject_user_id=10,
                key="preferred_nickname",
                value="juancho",
                created_by_user_id=99,
            )
        )
        self.assertEqual(row["value"], "juancho")

        updated = await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="USER_NICKNAME",
                subject_user_id=10,
                key="preferred_nickname",
                value="presidente",
                created_by_user_id=99,
            )
        )
        rows = await self.service.list_user_memories(1, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(updated["id"], rows[0]["id"])
        self.assertEqual(rows[0]["value"], "presidente")
        self.assertEqual(await self.service.list_user_memories(2, 10), [])

        self.assertTrue(await self.service.archive_memory(1, int(row["id"])))
        self.assertEqual(await self.service.list_user_memories(1, 10), [])

    async def test_pending_approval_and_expiration(self) -> None:
        pending = await self.service.create_pending_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="USER_NICKNAME",
                subject_user_id=10,
                key="preferred_nickname",
                value="questionable",
                created_by_user_id=99,
                expires_at="2000-01-01T00:00:00+00:00",
            ),
            ttl_minutes=15,
        )
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(await self.service.approve_memory(1, int(pending["id"]), 10))
        active = await self.service.get_memory(1, int(pending["id"]))
        self.assertIsNotNone(active)
        self.assertEqual(active["status"], "active")

        expired = await self.service.create_pending_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="SERVER_FACT",
                key="old",
                value="old fact",
                created_by_user_id=99,
                expires_at="2000-01-01T00:00:00+00:00",
            ),
            ttl_minutes=-1,
        )
        count = await self.service.expire_pending_memories(1)
        self.assertGreaterEqual(count, 1)
        row = await self.service.get_memory(1, int(expired["id"]))
        self.assertEqual(row["status"], "expired")

    async def test_context_injects_relevant_memory_only(self) -> None:
        await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="USER_NICKNAME",
                subject_user_id=10,
                key="preferred_nickname",
                value="juancho",
                created_by_user_id=99,
            )
        )
        await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="USER_NICKNAME",
                subject_user_id=20,
                key="preferred_nickname",
                value="unrelated",
                created_by_user_id=99,
            )
        )
        await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="FOOTBALL_PREFERENCE",
                key="league",
                value="Liga MX",
                created_by_user_id=99,
            )
        )
        builder = ServerMemoryContextBuilder(self.service)

        chat_context = await builder.build_context(
            guild_id=1,
            channel_id=100,
            author_user_id=99,
            mentioned_user_ids=[10],
            route_action="CHAT",
            current_text="hola",
        )
        self.assertIn("juancho", chat_context)
        self.assertNotIn("unrelated", chat_context)
        self.assertNotIn("Liga MX", chat_context)

        football_context = await builder.build_context(
            guild_id=1,
            channel_id=100,
            author_user_id=99,
            mentioned_user_ids=[],
            route_action="FOOTBALL_WATCH_TODAY",
            current_text="que partido hay",
        )
        self.assertIn("Liga MX", football_context)

    async def test_bot_behavior_rule_is_injected_for_future_chat_context(self) -> None:
        await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="BOT_BEHAVIOR_RULE",
                key="style.opening_phrase",
                value="Do not start replies with Orale wey.",
                created_by_user_id=99,
                source_type="trusted_admin_instruction",
                approved_by_user_id=99,
            )
        )
        builder = ServerMemoryContextBuilder(self.service)

        context = await builder.build_context(
            guild_id=1,
            channel_id=100,
            author_user_id=99,
            mentioned_user_ids=[],
            route_action="CHAT",
            current_text="hola",
        )

        self.assertIn("[TRUSTED_SERVER_MEMORY]", context)
        self.assertIn("Bot Behavior Rule style.opening phrase", context)
        self.assertIn("Do not start replies with Orale wey.", context)

    def test_approval_phrase_classifier(self) -> None:
        self.assertEqual(ServerMemoryService.classify_approval_text("va, ta bien"), "approve")
        self.assertEqual(ServerMemoryService.classify_approval_text("nel"), "reject")
        self.assertIsNone(ServerMemoryService.classify_approval_text("tal vez"))


if __name__ == "__main__":
    unittest.main()
