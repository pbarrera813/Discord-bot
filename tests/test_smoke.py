from __future__ import annotations

import os
import tempfile
import unittest

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
