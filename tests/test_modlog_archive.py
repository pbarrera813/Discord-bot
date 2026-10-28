from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import database as database_module
from services.database import Database


class ModlogArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(prefix="discordbot_modlog_archive_", suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)
        await self.db.init()
        self.now = datetime(2026, 9, 28, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass

    async def test_save_get_sync_and_delete(self) -> None:
        snapshot = {"content": "before", "attachments": [{"id": 11, "filename": "a.png"}]}
        first = {"id": 11, "filename": "a.png", "content_type": "image/png", "spoiler": False,
                 "position": 0, "data": b"first"}
        await self.db.save_modlog_images(1, 2, snapshot, [first], now=self.now)

        edited = {"id": 11, "filename": "a.png", "content_type": "image/png", "spoiler": True,
                  "position": 0, "data": b"updated"}
        await self.db.save_modlog_images(1, 2, {"content": "after"}, [edited], now=self.now + timedelta(hours=1))
        saved_snapshot, images = await self.db.get_modlog_images(1, 2, now=self.now + timedelta(hours=1))
        self.assertEqual(saved_snapshot, {"content": "after"})
        self.assertEqual([image["id"] for image in images], [11])
        self.assertEqual(images[0]["data"], b"updated")
        self.assertTrue(images[0]["spoiler"])
        self.assertEqual(images[0]["captured_at"], self.now.isoformat())

        await self.db.save_modlog_images(1, 2, {"content": "removed"}, [], now=self.now + timedelta(hours=2))
        self.assertEqual(await self.db.get_modlog_images(1, 2, now=self.now + timedelta(hours=2)), ({"content": "removed"}, []))
        await self.db.delete_modlog_images(1, 2)
        self.assertIsNone(await self.db.get_modlog_images(1, 2))


    async def test_capacity_evicts_oldest_image_bytes_globally(self) -> None:
        with patch.object(database_module, "MODLOG_IMAGE_MAX_BYTES", 5):
            await self.db.save_modlog_images(1, 1, {}, [{"id": 1, "filename": "old", "data": b"1234"}], now=self.now)
            await self.db.save_modlog_images(2, 2, {}, [{"id": 2, "filename": "new", "data": b"5678"}], now=self.now + timedelta(seconds=1))
            old_snapshot, old_images = await self.db.get_modlog_images(1, 1, now=self.now + timedelta(seconds=1))
            _, new_images = await self.db.get_modlog_images(2, 2, now=self.now + timedelta(seconds=1))
        self.assertEqual(old_snapshot, {})
        self.assertEqual(old_images, [])
        self.assertEqual([image["data"] for image in new_images], [b"5678"])

    async def test_each_image_expires_24_hours_after_capture(self) -> None:
        await self.db.save_modlog_images(5, 6, {"content": "old snapshot"}, [
            {"id": 1, "filename": "old.png", "data": b"old"},
        ], now=self.now)
        await self.db.save_modlog_images(5, 6, {"content": "edited snapshot"}, [
            {"id": 1, "filename": "old.png", "data": b"old"},
            {"id": 2, "filename": "new.png", "data": b"new"},
        ], now=self.now + timedelta(hours=20))

        snapshot, images = await self.db.get_modlog_images(5, 6, now=self.now + timedelta(hours=25))
        self.assertEqual(snapshot, {"content": "edited snapshot"})
        self.assertEqual([image["id"] for image in images], [2])
        self.assertIsNone(await self.db.get_modlog_images(5, 6, now=self.now + timedelta(hours=45)))

    async def test_archive_survives_database_reopen(self) -> None:
        now = datetime.now(timezone.utc)
        await self.db.save_modlog_images(7, 8, {"content": "persistent"}, [
            {"id": 3, "filename": "saved.png", "data": b"bytes"},
        ], now=now)
        reopened = Database(self.db_path)
        await reopened.init()
        snapshot, images = await reopened.get_modlog_images(7, 8, now=now + timedelta(seconds=1))
        self.assertEqual(snapshot, {"content": "persistent"})
        self.assertEqual(images[0]["data"], b"bytes")


if __name__ == "__main__":
    unittest.main()
