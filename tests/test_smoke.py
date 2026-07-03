from __future__ import annotations

import os
import tempfile
import unittest

from services import database as db_module
from services.database import Database
from utils.duration import DurationParseError, parse_duration
from utils.i18n import normalize_language, tr


class DatabaseSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_core_flow(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_smoke.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()

        settings = await db.get_or_create_guild_settings(1)
        self.assertEqual(settings.prefix, "!")
        self.assertEqual(settings.language_code, "en")

        await db.set_prefix(1, "?")
        await db.set_language(1, "es")
        updated = await db.get_guild_settings(1)
        self.assertEqual(updated.prefix, "?")
        self.assertEqual(updated.language_code, "es")

        warn_id = await db.add_warning(1, 10, 20, "reason")
        self.assertGreater(warn_id, 0)
        warnings = await db.get_warnings(1, 10)
        self.assertEqual(len(warnings), 1)

        await db.upsert_temp_action(1, 10, "tempmute", 1, "1s", "reason", 20)
        due = await db.get_due_temp_actions(9999999999)
        self.assertEqual(len(due), 1)
        await db.delete_temp_action(int(due[0]["id"]))
        due_after = await db.get_due_temp_actions(9999999999)
        self.assertEqual(len(due_after), 0)

    async def test_reset_ai_server_context_clears_memory_state(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_ai_reset.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        await db.get_or_create_guild_settings(123)
        await db.set_server_context(123, "Tone: stale")
        await db.upsert_server_context_entry(
            guild_id=123,
            channel_id=456,
            channel_name="general",
            summary="Tone: stale",
        )
        await db.add_ai_conversation_turn(123, 456, "user", "Pablo", "Pablo: hola")

        await db.reset_ai_server_context(123)

        settings = await db.get_guild_settings(123)
        entries = await db.get_server_context_entries(123)
        turns = await db.get_ai_conversation_history(123, 456)
        self.assertEqual(settings.server_context, "")
        self.assertEqual(entries, [])
        self.assertEqual(turns, [])

    async def test_interaction_context_does_not_count_against_real_channels(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_ai_context_entries.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        await db.get_or_create_guild_settings(123)
        sentinel = db_module.AI_INTERACTIONS_CONTEXT_CHANNEL_ID

        final_entries, _ = await db.upsert_server_context_entry(
            guild_id=123,
            channel_id=sentinel,
            channel_name="ai-interactions",
            summary="Tone: learned from chats",
        )
        self.assertEqual([int(entry["channel_id"]) for entry in final_entries], [sentinel])

        await db.upsert_server_context_entry(
            guild_id=123,
            channel_id=456,
            channel_name="general",
            summary="Tone: general",
        )
        final_entries, _ = await db.upsert_server_context_entry(
            guild_id=123,
            channel_id=789,
            channel_name="memes",
            summary="Tone: memes",
        )

        channel_ids = [int(entry["channel_id"]) for entry in final_entries]
        settings = await db.get_guild_settings(123)
        self.assertEqual(set(channel_ids), {456, 789})
        self.assertNotIn("ai-interactions", settings.server_context)

    async def test_recent_ai_conversation_turns_filters_by_guild_and_date(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_ai_recent_turns.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        await db.add_ai_conversation_turn(123, 456, "user", "Pablo", "Pablo: hola")
        await db.add_ai_conversation_turn(999, 456, "user", "Other", "Other: nope")

        rows = await db.get_recent_ai_conversation_turns(123, "2000-01-01T00:00:00+00:00")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["speaker"], "Pablo")

    async def test_ai_conversation_turn_message_id_lookup(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_ai_message_id.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        await db.add_ai_conversation_turn(
            123,
            456,
            "assistant",
            "Nitori",
            "Nitori: hola",
            message_id=999,
        )
        await db.add_ai_conversation_turn(
            123,
            456,
            "user",
            "Pablo",
            "Pablo: hola",
            message_id=1000,
        )

        self.assertTrue(await db.is_ai_assistant_message(123, 456, 999))
        self.assertFalse(await db.is_ai_assistant_message(123, 456, 1000))
        self.assertFalse(await db.is_ai_assistant_message(123, 999, 999))


class UtilityTests(unittest.TestCase):
    def test_duration_parse(self) -> None:
        self.assertEqual(parse_duration("120s"), (120, "120s"))
        self.assertEqual(parse_duration("2m"), (120, "2m"))
        self.assertEqual(parse_duration("1h"), (3600, "1h"))
        self.assertEqual(parse_duration("1d"), (86400, "1d"))
        with self.assertRaises(DurationParseError):
            parse_duration("abc")

    def test_i18n(self) -> None:
        self.assertEqual(normalize_language("es"), "es")
        self.assertEqual(normalize_language("xx"), "en")
        self.assertEqual(tr("en", "hello", "hola"), "hello")
        self.assertEqual(tr("es", "hello", "hola"), "hola")


if __name__ == "__main__":
    unittest.main()
