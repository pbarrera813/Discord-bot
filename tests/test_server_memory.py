from __future__ import annotations

import os
import tempfile
import unittest

from services.database import Database
from services.server_memory import MEMORY_TYPES, SELF_DECIDABLE_MEMORY_TYPES, ServerMemoryInput, ServerMemoryService
from services.server_memory_context import ServerMemoryContextBuilder


class ServerMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db_path = os.path.join(tempfile.gettempdir(), f"discordbot_test_server_memory_{self._testMethodName}.db")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)
        await self.db.init()
        self.service = ServerMemoryService(self.db)

    async def test_create_update_delete_and_no_cross_guild_leakage(self) -> None:
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

        self.assertTrue(await self.service.delete_memory(1, int(row["id"])))
        self.assertEqual(await self.service.list_user_memories(1, 10), [])
        self.assertIsNone(await self.service.get_memory(1, int(row["id"])))
        self.assertFalse(await self.service.delete_memory(1, int(row["id"])))

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


    async def test_context_keeps_only_active_authorized_scope_rows(self) -> None:
        for guild_id, key, value, subject_user_id, subject_channel_id in (
            (1, "global", "active guild global", None, None),
            (2, "other-guild", "other guild", None, None),
            (1, "author-scoped", "author scoped", 99, None),
            (1, "allowed-user-channel", "allowed user in this channel", 10, 100),
            (1, "replied-user", "replied user scope", 30, None),
            (1, "same-user-other-channel", "same user other channel", 10, 101),
            (1, "other-user-same-channel", "other user same channel", 20, 100),
        ):
            await self.service.create_memory(
                ServerMemoryInput(
                    guild_id=guild_id,
                    memory_type="SERVER_FACT",
                    subject_user_id=subject_user_id,
                    subject_channel_id=subject_channel_id,
                    key=key,
                    value=value,
                    created_by_user_id=99,
                )
            )
        pending = await self.service.create_pending_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="SERVER_FACT",
                key="pending-global",
                value="pending global",
                created_by_user_id=99,
            )
        )
        archived = await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1,
                memory_type="SERVER_FACT",
                key="archived-global",
                value="archived global",
                created_by_user_id=99,
            )
        )
        self.assertTrue(await self.service.delete_memory(1, int(archived["id"])))

        context = await ServerMemoryContextBuilder(self.service).build_context(
            guild_id=1,
            channel_id=100,
            author_user_id=99,
            mentioned_user_ids=[10],
            replied_user_id=30,
            route_action="CHAT",
            current_text="words matching none of these memories",
        )

        self.assertNotIn("active guild global", context)
        self.assertIn("author scoped", context)
        self.assertIn("allowed user in this channel", context)
        self.assertIn("replied user scope", context)
        for excluded in ("other guild", "same user other channel", "other user same channel", "pending global", "archived global"):
            self.assertNotIn(excluded, context)
        pending_row = await self.service.get_memory(1, int(pending["id"]))
        self.assertEqual(pending_row["status"], "pending")


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

    async def test_clear_memories_deletes_every_row_of_that_guild_only(self) -> None:
        for guild_id, status in ((1, "active"), (1, "pending"), (2, "active")):
            await self.db.add_server_memory(
                guild_id=guild_id, memory_type="SERVER_FACT", key=f"k{status}", value="v", source_type="test",
                created_by_user_id=99, status=status,
            )

        await self.service.clear_memories(1)

        self.assertEqual(await self.service.list_memories(1, status=None), [])
        self.assertEqual(len(await self.service.list_memories(2, status=None)), 1)

    async def test_memories_archived_by_older_versions_are_deleted_on_startup(self) -> None:
        await self.db.add_server_memory(
            guild_id=1, memory_type="SERVER_FACT", key="old", value="v", source_type="test", created_by_user_id=99,
        )
        await self.db.add_server_memory(
            guild_id=1, memory_type="SERVER_FACT", key="gone", value="v", source_type="test", created_by_user_id=99,
        )
        await self.db.update_server_memory_status(2, guild_id=1, status="archived")

        await Database(self.db_path).init()

        self.assertEqual([row["key"] for row in await self.service.list_memories(1, status=None)], ["old"])

    async def _joke(self, user_id, value, memory_type="SERVER_FACT", key="joke"):
        await self.service.create_memory(
            ServerMemoryInput(
                guild_id=1, memory_type=memory_type, subject_user_id=user_id, key=key, value=value, created_by_user_id=99,
            )
        )

    async def test_unlinked_lore_is_not_ambient_but_rules_are(self) -> None:
        await self._joke(None, "unlinked fact")
        await self._joke(None, "unlinked joke", memory_type="INSIDE_JOKE")
        await self._joke(None, "be polite", memory_type="SERVER_RULE", key="rule")

        context = await ServerMemoryContextBuilder(self.service).build_context(guild_id=1, channel_id=100, author_user_id=99)

        self.assertIn("be polite", context)
        self.assertNotIn("unlinked", context)

    async def test_linked_lore_follows_author_mention_reply_and_recent_participants(self) -> None:
        for user_id in (10, 11, 12, 13, 14):
            await self._joke(user_id, f"joke about {user_id}")
        names = {10: "Ana", 11: "Beto", 12: "Cleo", 13: "Dani"}
        builder = ServerMemoryContextBuilder(self.service)

        context = await builder.build_context(
            guild_id=1, channel_id=100, author_user_id=10, mentioned_user_ids=[11], replied_user_id=12,
            participant_user_ids=[13], user_label=names.get,
        )

        for user_id in (10, 11, 12, 13):
            self.assertIn(f"joke about {user_id}", context)
        self.assertNotIn("joke about 14", context)
        self.assertIn(f"About Ana (<@10>)", context)

    async def test_lore_is_offered_once_per_cooldown_but_preferences_always(self) -> None:
        await self._joke(10, "boogers", memory_type="INSIDE_JOKE")
        await self._joke(10, "likes tea", memory_type="USER_PREFERENCE", key="drink")
        builder = ServerMemoryContextBuilder(self.service)

        first = await builder.build_context(guild_id=1, channel_id=100, author_user_id=10)
        second = await builder.build_context(guild_id=1, channel_id=100, author_user_id=10)
        other_channel = await builder.build_context(guild_id=1, channel_id=101, author_user_id=10)

        self.assertIn("boogers", first)
        self.assertNotIn("boogers", second)
        self.assertIn("likes tea", second)
        self.assertIn("boogers", other_channel)

    def test_a_member_needs_approval_for_anything_that_is_not_about_themselves(self) -> None:
        for memory_type in sorted(MEMORY_TYPES):
            with self.subTest(about_someone_else=memory_type):
                self.assertTrue(
                    ServerMemoryService.should_require_approval(
                        memory_type, author_can_manage=False, subject_is_author=False,
                    )
                )

    def test_a_member_may_save_a_personal_memory_about_themselves_without_approval(self) -> None:
        for memory_type in SELF_DECIDABLE_MEMORY_TYPES:
            with self.subTest(about_themselves=memory_type):
                self.assertFalse(
                    ServerMemoryService.should_require_approval(
                        memory_type, author_can_manage=False, subject_is_author=True,
                    )
                )
        for memory_type in ("SERVER_RULE", "CHANNEL_CONTEXT"):
            with self.subTest(not_personal=memory_type):
                self.assertTrue(
                    ServerMemoryService.should_require_approval(
                        memory_type, author_can_manage=False, subject_is_author=True,
                    )
                )

    def test_an_administrator_never_needs_approval(self) -> None:
        for memory_type in sorted(MEMORY_TYPES):
            with self.subTest(administrator=memory_type):
                self.assertFalse(
                    ServerMemoryService.should_require_approval(
                        memory_type, author_can_manage=True, subject_is_author=False,
                    )
                )


if __name__ == "__main__":
    unittest.main()
