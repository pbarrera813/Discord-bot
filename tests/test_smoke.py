from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.database import Database
from services.voice_messages import DiscordVoiceMessageSender, ProcessedVoiceAudio, VoicePermissionError, sanitize_tts_text
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


    async def test_ai_conversation_turn_metadata_and_parent_chain(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_ai_branch_metadata.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        await db.add_ai_conversation_turn(
            123,
            456,
            "user",
            "Pablo",
            "Pablo: draw a forest",
            message_id=100,
            author_user_id=10,
            action_type="GENERATE_IMAGE",
            resolved_request="a forest",
        )
        await db.add_ai_conversation_turn(
            123,
            456,
            "assistant",
            "Nitori",
            "Nitori: Generated image: a forest",
            message_id=101,
            author_user_id=20,
            parent_message_id=100,
            action_type="GENERATE_IMAGE",
            resolved_request="a forest",
        )

        turn = await db.get_ai_turn_by_message_id(123, 456, 101)
        chain = await db.get_ai_parent_chain(123, 456, 101)

        self.assertEqual(turn["author_user_id"], "20")
        self.assertEqual(turn["parent_message_id"], "100")
        self.assertEqual(turn["action_type"], "GENERATE_IMAGE")
        self.assertEqual(turn["resolved_request"], "a forest")
        self.assertEqual([row["message_id"] for row in chain], ["100", "101"])

    async def test_member_events_record_and_count(self) -> None:
        db_path = os.path.join(tempfile.gettempdir(), "discordbot_test_member_events.db")
        if os.path.exists(db_path):
            os.remove(db_path)

        db = Database(db_path)
        await db.init()
        now = datetime.now(timezone.utc)

        self.assertTrue(await db.record_member_join(123, 10, now - timedelta(hours=1)))
        self.assertTrue(await db.record_member_leave(123, 11, now - timedelta(hours=2)))
        self.assertFalse(await db.record_member_join(123, 10, now - timedelta(hours=1)))
        self.assertTrue(await db.record_member_join(999, 10, now - timedelta(hours=1)))

        self.assertEqual(await db.count_member_events(123, "join", now - timedelta(hours=5)), 1)
        self.assertEqual(await db.count_member_events(123, "leave", now - timedelta(hours=5)), 1)
        self.assertEqual(await db.count_member_events(999, "join", now - timedelta(hours=5)), 1)
        self.assertEqual(
            await db.count_member_events_between(123, "join", now - timedelta(minutes=30), now),
            0,
        )


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


    def test_tts_tag_sanitizer_preserves_allowlist_and_neutralizes_unknown(self) -> None:
        output = sanitize_tts_text("hola [sigh] [evil-mode] [laugh]")
        self.assertEqual(output, "hola [sigh] evil mode [laugh]")


class VoiceMessageSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_voice_payload_contains_required_metadata(self) -> None:
        calls: list[dict[str, object]] = []

        class _HTTP:
            async def request(self, route, *, files, form):
                calls.append({"route": route, "files": files, "form": form})
                return {"id": "12345"}

        perms = SimpleNamespace(send_messages=True, attach_files=True, send_voice_messages=True)
        guild = SimpleNamespace(me=SimpleNamespace(id=42))
        channel = SimpleNamespace(id=99, guild=guild, permissions_for=lambda _me: perms)
        sender = DiscordVoiceMessageSender(SimpleNamespace(http=_HTTP()))

        message_id = await sender.send(
            channel,
            ProcessedVoiceAudio(data=b"ogg-data", duration_seconds=2.3456, waveform="AQID"),
        )

        self.assertEqual(message_id, 12345)
        form = calls[0]["form"]
        payload = next(item for item in form if item["name"] == "payload_json")["value"]
        self.assertIn('"flags":8192', payload)
        self.assertIn('"duration_secs":2.346', payload)
        self.assertIn('"waveform":"AQID"', payload)
        file_part = next(item for item in form if item["name"] == "files[0]")
        self.assertEqual(file_part["content_type"], "audio/ogg")

    async def test_native_voice_sender_rejects_missing_voice_permission(self) -> None:
        perms = SimpleNamespace(send_messages=True, attach_files=True, send_voice_messages=False)
        guild = SimpleNamespace(me=SimpleNamespace(id=42))
        channel = SimpleNamespace(id=99, guild=guild, permissions_for=lambda _me: perms)
        sender = DiscordVoiceMessageSender(SimpleNamespace(http=SimpleNamespace()))

        with self.assertRaises(VoicePermissionError):
            await sender.send(
                channel,
                ProcessedVoiceAudio(data=b"ogg-data", duration_seconds=1.0, waveform="AQID"),
            )


if __name__ == "__main__":
    unittest.main()
