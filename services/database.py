from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

AI_CHANNEL_ALL_MARKER = 0
AI_CHANNEL_NONE_MARKER = -1
AI_INTERACTIONS_CONTEXT_CHANNEL_ID = -2
BIRTHDAY_EVENT_TYPES = {"birthday", "member_anniversary", "server_anniversary"}


@dataclass
class GuildSettings:
    guild_id: int
    prefix: str
    modlog_channel_id: int | None
    server_context: str
    muted_role_id: int | None
    language_code: str


@dataclass
class AnnouncementSettings:
    guild_id: int
    kind: str
    enabled: bool
    channel_id: int | None
    mode: str
    message_text: str
    image_url: str
    color_hex: str


class Database:
    def __init__(self, db_path: str, default_prefix: str = "!") -> None:
        self.db_path = db_path
        self.default_prefix = default_prefix

    async def init(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode = WAL;")
            await conn.execute("PRAGMA foreign_keys = ON;")

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    prefix TEXT NOT NULL DEFAULT '!',
                    modlog_channel_id INTEGER,
                    server_context TEXT NOT NULL DEFAULT '',
                    muted_role_id INTEGER,
                    language_code TEXT NOT NULL DEFAULT 'en',
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_guild_settings_columns(conn)

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS temp_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    duration_input TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, user_id, action)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_filters (
                    guild_id INTEGER PRIMARY KEY,
                    anti_spam_enabled INTEGER NOT NULL DEFAULT 1,
                    anti_link_enabled INTEGER NOT NULL DEFAULT 0,
                    spam_threshold INTEGER NOT NULL DEFAULT 5,
                    spam_window_seconds INTEGER NOT NULL DEFAULT 8,
                    created_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_channels (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, channel_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_ai_conversation_columns(conn)

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS server_context_entries (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    channel_name TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, channel_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    remind_at INTEGER NOT NULL,
                    mention_in_channel INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reminder_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    mention_in_channel INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(reminder_id, user_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS color_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    color_name TEXT NOT NULL,
                    hex_code TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, color_name)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS color_panels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS announcement_settings (
                    guild_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER,
                    mode TEXT NOT NULL DEFAULT 'text',
                    message_text TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    color_hex TEXT NOT NULL DEFAULT '#00FFFF',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, kind)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    channel_id INTEGER,
                    role_id INTEGER,
                    server_timezone TEXT NOT NULL DEFAULT 'UTC',
                    birthday_timezone_mode TEXT NOT NULL DEFAULT 'user',
                    disable_ages INTEGER NOT NULL DEFAULT 0,
                    trusted_role_id INTEGER,
                    trusted_prevent_message INTEGER NOT NULL DEFAULT 0,
                    trusted_prevent_role INTEGER NOT NULL DEFAULT 0,
                    trusted_prevent_list INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_user_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    birth_year INTEGER,
                    timezone TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, user_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_event_settings (
                    guild_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    message_hour INTEGER NOT NULL DEFAULT 0,
                    ping_setting TEXT NOT NULL DEFAULT 'none',
                    image_format TEXT NOT NULL DEFAULT 'none',
                    message_mode TEXT NOT NULL DEFAULT 'embed',
                    embed_title TEXT NOT NULL DEFAULT '',
                    embed_color TEXT NOT NULL DEFAULT '',
                    embed_image_url TEXT NOT NULL DEFAULT '',
                    birthday_message_no_year TEXT NOT NULL DEFAULT '',
                    birthday_message_with_age TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, event_type)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    template_text TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_dispatch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    user_id INTEGER,
                    event_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, event_type, user_id, event_date)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_blacklist_users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, user_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_blacklist_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, role_id)
                )
                """
            )

            # Backward-compatible migration for existing databases.
            for alter_sql in (
                "ALTER TABLE birthday_event_settings ADD COLUMN message_mode TEXT NOT NULL DEFAULT 'embed'",
                "ALTER TABLE birthday_event_settings ADD COLUMN embed_title TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE birthday_event_settings ADD COLUMN embed_color TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE birthday_event_settings ADD COLUMN embed_image_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE birthday_event_settings ADD COLUMN birthday_message_no_year TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE birthday_event_settings ADD COLUMN birthday_message_with_age TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    await conn.execute(alter_sql)
                except aiosqlite.OperationalError:
                    pass

            await conn.commit()

    @asynccontextmanager
    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            await conn.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_settings(row: aiosqlite.Row) -> GuildSettings:
        return GuildSettings(
            guild_id=int(row["guild_id"]),
            prefix=str(row["prefix"]),
            modlog_channel_id=int(row["modlog_channel_id"])
            if row["modlog_channel_id"] is not None
            else None,
            server_context=str(row["server_context"] or ""),
            muted_role_id=int(row["muted_role_id"])
            if row["muted_role_id"] is not None
            else None,
            language_code=str(row["language_code"] or "en"),
        )

    async def get_or_create_guild_settings(self, guild_id: int) -> GuildSettings:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO guild_settings
                (guild_id, prefix, modlog_channel_id, server_context, muted_role_id, language_code, created_at)
                VALUES (?, ?, NULL, '', NULL, 'en', ?)
                """,
                (guild_id, self.default_prefix, self._now_iso()),
            )
            await conn.commit()

            cursor = await conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_settings(row)

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        return await self.get_or_create_guild_settings(guild_id)

    @staticmethod
    def _validate_birthday_event_type(event_type: str) -> str:
        normalized = event_type.strip().lower()
        if normalized not in BIRTHDAY_EVENT_TYPES:
            raise ValueError(f"Unsupported birthday event type: {event_type}")
        return normalized

    async def get_or_create_birthday_guild_settings(self, guild_id: int) -> dict[str, Any]:
        now = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO birthday_guild_settings
                (guild_id, enabled, channel_id, role_id, server_timezone, birthday_timezone_mode,
                 disable_ages, trusted_role_id, trusted_prevent_message, trusted_prevent_role,
                 trusted_prevent_list, created_at, updated_at)
                VALUES (?, 1, NULL, NULL, 'UTC', 'user', 0, NULL, 0, 0, 0, ?, ?)
                """,
                (guild_id, now, now),
            )
            await conn.commit()
            cursor = await conn.execute(
                """
                SELECT guild_id, enabled, channel_id, role_id, server_timezone, birthday_timezone_mode,
                       disable_ages, trusted_role_id, trusted_prevent_message, trusted_prevent_role,
                       trusted_prevent_list
                FROM birthday_guild_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row is not None else {}

    async def update_birthday_guild_settings(
        self,
        guild_id: int,
        *,
        enabled: bool | None = None,
        channel_id: int | None | object = Ellipsis,
        role_id: int | None | object = Ellipsis,
        server_timezone: str | None = None,
        birthday_timezone_mode: str | None = None,
        disable_ages: bool | None = None,
        trusted_role_id: int | None | object = Ellipsis,
        trusted_prevent_message: bool | None = None,
        trusted_prevent_role: bool | None = None,
        trusted_prevent_list: bool | None = None,
    ) -> dict[str, Any]:
        await self.get_or_create_birthday_guild_settings(guild_id)
        updates: list[str] = []
        values: list[Any] = []

        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if channel_id is not Ellipsis:
            updates.append("channel_id = ?")
            values.append(channel_id)
        if role_id is not Ellipsis:
            updates.append("role_id = ?")
            values.append(role_id)
        if server_timezone is not None:
            updates.append("server_timezone = ?")
            values.append(server_timezone.strip())
        if birthday_timezone_mode is not None:
            updates.append("birthday_timezone_mode = ?")
            values.append(birthday_timezone_mode.strip().lower())
        if disable_ages is not None:
            updates.append("disable_ages = ?")
            values.append(1 if disable_ages else 0)
        if trusted_role_id is not Ellipsis:
            updates.append("trusted_role_id = ?")
            values.append(trusted_role_id)
        if trusted_prevent_message is not None:
            updates.append("trusted_prevent_message = ?")
            values.append(1 if trusted_prevent_message else 0)
        if trusted_prevent_role is not None:
            updates.append("trusted_prevent_role = ?")
            values.append(1 if trusted_prevent_role else 0)
        if trusted_prevent_list is not None:
            updates.append("trusted_prevent_list = ?")
            values.append(1 if trusted_prevent_list else 0)

        if updates:
            updates.append("updated_at = ?")
            values.append(self._now_iso())
            values.append(guild_id)
            async with self._connect() as conn:
                await conn.execute(
                    f"UPDATE birthday_guild_settings SET {', '.join(updates)} WHERE guild_id = ?",
                    tuple(values),
                )
                await conn.commit()
        return await self.get_or_create_birthday_guild_settings(guild_id)

    async def upsert_birthday_profile(
        self,
        *,
        guild_id: int,
        user_id: int,
        month: int,
        day: int,
        birth_year: int | None,
        timezone_name: str | None,
    ) -> None:
        now = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO birthday_user_profiles
                (guild_id, user_id, month, day, birth_year, timezone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET
                    month = excluded.month,
                    day = excluded.day,
                    birth_year = excluded.birth_year,
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    user_id,
                    month,
                    day,
                    birth_year,
                    timezone_name.strip() if isinstance(timezone_name, str) and timezone_name.strip() else None,
                    now,
                    now,
                ),
            )
            await conn.commit()

    async def get_birthday_profile(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, user_id, month, day, birth_year, timezone, created_at, updated_at
                FROM birthday_user_profiles
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def delete_birthday_profile(self, guild_id: int, user_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM birthday_user_profiles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.commit()
            return bool(cursor.rowcount)

    async def list_birthday_profiles(self, guild_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, user_id, month, day, birth_year, timezone, created_at, updated_at
                FROM birthday_user_profiles
                WHERE guild_id = ?
                ORDER BY month ASC, day ASC, user_id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_or_create_birthday_event_settings(
        self,
        guild_id: int,
        event_type: str,
    ) -> dict[str, Any]:
        normalized = self._validate_birthday_event_type(event_type)
        now = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO birthday_event_settings
                (guild_id, event_type, enabled, message_hour, ping_setting, image_format, message_mode,
                 embed_title, embed_color, embed_image_url, birthday_message_no_year,
                 birthday_message_with_age, created_at, updated_at)
                VALUES (?, ?, 1, 0, 'none', 'none', 'embed', '', '', '', '', '', ?, ?)
                """,
                (guild_id, normalized, now, now),
            )
            await conn.commit()
            cursor = await conn.execute(
                """
                SELECT guild_id, event_type, enabled, message_hour, ping_setting, image_format,
                       message_mode, embed_title, embed_color, embed_image_url,
                       birthday_message_no_year, birthday_message_with_age
                FROM birthday_event_settings
                WHERE guild_id = ? AND event_type = ?
                """,
                (guild_id, normalized),
            )
            row = await cursor.fetchone()
            return dict(row) if row is not None else {}

    async def update_birthday_event_settings(
        self,
        guild_id: int,
        event_type: str,
        *,
        enabled: bool | None = None,
        message_hour: int | None = None,
        ping_setting: str | None = None,
        image_format: str | None = None,
        message_mode: str | None = None,
        embed_title: str | None = None,
        embed_color: str | None = None,
        embed_image_url: str | None = None,
        birthday_message_no_year: str | None = None,
        birthday_message_with_age: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._validate_birthday_event_type(event_type)
        await self.get_or_create_birthday_event_settings(guild_id, normalized)
        updates: list[str] = []
        values: list[Any] = []
        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if message_hour is not None:
            updates.append("message_hour = ?")
            values.append(int(message_hour))
        if ping_setting is not None:
            updates.append("ping_setting = ?")
            values.append(ping_setting.strip())
        if image_format is not None:
            updates.append("image_format = ?")
            values.append(image_format.strip().lower())
        if message_mode is not None:
            updates.append("message_mode = ?")
            values.append(message_mode.strip().lower())
        if embed_title is not None:
            updates.append("embed_title = ?")
            values.append(embed_title.strip())
        if embed_color is not None:
            updates.append("embed_color = ?")
            values.append(embed_color.strip())
        if embed_image_url is not None:
            updates.append("embed_image_url = ?")
            values.append(embed_image_url.strip())
        if birthday_message_no_year is not None:
            updates.append("birthday_message_no_year = ?")
            values.append(birthday_message_no_year.strip())
        if birthday_message_with_age is not None:
            updates.append("birthday_message_with_age = ?")
            values.append(birthday_message_with_age.strip())

        if updates:
            updates.append("updated_at = ?")
            values.append(self._now_iso())
            values.extend([guild_id, normalized])
            async with self._connect() as conn:
                await conn.execute(
                    f"UPDATE birthday_event_settings SET {', '.join(updates)} WHERE guild_id = ? AND event_type = ?",
                    tuple(values),
                )
                await conn.commit()
        return await self.get_or_create_birthday_event_settings(guild_id, normalized)

    async def list_birthday_event_settings(self, guild_id: int) -> list[dict[str, Any]]:
        for event_type in sorted(BIRTHDAY_EVENT_TYPES):
            await self.get_or_create_birthday_event_settings(guild_id, event_type)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, event_type, enabled, message_hour, ping_setting, image_format,
                       message_mode, embed_title, embed_color, embed_image_url,
                       birthday_message_no_year, birthday_message_with_age
                FROM birthday_event_settings
                WHERE guild_id = ?
                ORDER BY event_type ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def count_birthday_templates(self, guild_id: int, event_type: str) -> int:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM birthday_message_templates
                WHERE guild_id = ? AND event_type = ?
                """,
                (guild_id, normalized),
            )
            row = await cursor.fetchone()
            return int(row["total"]) if row else 0

    async def add_birthday_template(
        self,
        guild_id: int,
        event_type: str,
        template_text: str,
    ) -> int:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO birthday_message_templates
                (guild_id, event_type, template_text, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (guild_id, normalized, template_text, self._now_iso(), self._now_iso()),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def list_birthday_templates(self, guild_id: int, event_type: str) -> list[dict[str, Any]]:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, event_type, template_text, enabled, created_at, updated_at
                FROM birthday_message_templates
                WHERE guild_id = ? AND event_type = ?
                ORDER BY id ASC
                """,
                (guild_id, normalized),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_birthday_template(self, guild_id: int, event_type: str, template_id: int) -> bool:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM birthday_message_templates
                WHERE guild_id = ? AND event_type = ? AND id = ?
                """,
                (guild_id, normalized, template_id),
            )
            await conn.commit()
            return bool(cursor.rowcount)

    async def was_birthday_event_dispatched(
        self,
        guild_id: int,
        event_type: str,
        user_id: int | None,
        event_date: str,
    ) -> bool:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            if user_id is None:
                cursor = await conn.execute(
                    """
                    SELECT 1 FROM birthday_dispatch_log
                    WHERE guild_id = ? AND event_type = ? AND user_id IS NULL AND event_date = ?
                    LIMIT 1
                    """,
                    (guild_id, normalized, event_date),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT 1 FROM birthday_dispatch_log
                    WHERE guild_id = ? AND event_type = ? AND user_id = ? AND event_date = ?
                    LIMIT 1
                    """,
                    (guild_id, normalized, user_id, event_date),
                )
            row = await cursor.fetchone()
            return row is not None

    async def mark_birthday_event_dispatched(
        self,
        guild_id: int,
        event_type: str,
        user_id: int | None,
        event_date: str,
    ) -> None:
        normalized = self._validate_birthday_event_type(event_type)
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO birthday_dispatch_log
                (guild_id, event_type, user_id, event_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, normalized, user_id, event_date, self._now_iso()),
            )
            await conn.commit()

    async def add_birthday_blacklist_user(self, guild_id: int, user_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO birthday_blacklist_users (guild_id, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, user_id, self._now_iso()),
            )
            await conn.commit()

    async def remove_birthday_blacklist_user(self, guild_id: int, user_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM birthday_blacklist_users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.commit()
            return bool(cursor.rowcount)

    async def list_birthday_blacklist_users(self, guild_id: int) -> list[int]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT user_id FROM birthday_blacklist_users
                WHERE guild_id = ?
                ORDER BY user_id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [int(row["user_id"]) for row in rows]

    async def add_birthday_blacklist_role(self, guild_id: int, role_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO birthday_blacklist_roles (guild_id, role_id, created_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, role_id, self._now_iso()),
            )
            await conn.commit()

    async def remove_birthday_blacklist_role(self, guild_id: int, role_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM birthday_blacklist_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )
            await conn.commit()
            return bool(cursor.rowcount)

    async def list_birthday_blacklist_roles(self, guild_id: int) -> list[int]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT role_id FROM birthday_blacklist_roles
                WHERE guild_id = ?
                ORDER BY role_id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [int(row["role_id"]) for row in rows]

    @staticmethod
    def _validate_announcement_kind(kind: str) -> str:
        value = kind.strip().lower()
        if value not in {"welcome", "goodbye"}:
            raise ValueError(f"Unsupported announcement kind: {kind}")
        return value

    @staticmethod
    def _row_to_announcement_settings(row: aiosqlite.Row) -> AnnouncementSettings:
        return AnnouncementSettings(
            guild_id=int(row["guild_id"]),
            kind=str(row["kind"]),
            enabled=bool(int(row["enabled"] or 0)),
            channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
            mode=str(row["mode"] or "text"),
            message_text=str(row["message_text"] or ""),
            image_url=str(row["image_url"] or ""),
            color_hex=str(row["color_hex"] or "#00FFFF"),
        )

    async def get_or_create_announcement_settings(
        self, guild_id: int, kind: str
    ) -> AnnouncementSettings:
        normalized_kind = self._validate_announcement_kind(kind)
        now = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO announcement_settings
                (guild_id, kind, enabled, channel_id, mode, message_text, image_url, color_hex, created_at, updated_at)
                VALUES (?, ?, 0, NULL, 'text', '', '', '#00FFFF', ?, ?)
                """,
                (guild_id, normalized_kind, now, now),
            )
            await conn.commit()
            cursor = await conn.execute(
                """
                SELECT guild_id, kind, enabled, channel_id, mode, message_text, image_url, color_hex
                FROM announcement_settings
                WHERE guild_id = ? AND kind = ?
                """,
                (guild_id, normalized_kind),
            )
            row = await cursor.fetchone()
            return self._row_to_announcement_settings(row)

    async def get_announcement_settings(
        self, guild_id: int, kind: str
    ) -> AnnouncementSettings:
        return await self.get_or_create_announcement_settings(guild_id, kind)

    async def update_announcement_settings(
        self,
        guild_id: int,
        kind: str,
        *,
        enabled: bool | None = None,
        channel_id: int | None | object = Ellipsis,
        mode: str | None = None,
        message_text: str | None = None,
        image_url: str | None = None,
        color_hex: str | None = None,
    ) -> AnnouncementSettings:
        normalized_kind = self._validate_announcement_kind(kind)
        await self.get_or_create_announcement_settings(guild_id, normalized_kind)

        updates: list[str] = []
        values: list[Any] = []

        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if channel_id is not Ellipsis:
            updates.append("channel_id = ?")
            values.append(channel_id)
        if mode is not None:
            updates.append("mode = ?")
            values.append(mode)
        if message_text is not None:
            updates.append("message_text = ?")
            values.append(message_text)
        if image_url is not None:
            updates.append("image_url = ?")
            values.append(image_url)
        if color_hex is not None:
            updates.append("color_hex = ?")
            values.append(color_hex)

        if updates:
            updates.append("updated_at = ?")
            values.append(self._now_iso())
            values.extend([guild_id, normalized_kind])
            async with self._connect() as conn:
                await conn.execute(
                    f"UPDATE announcement_settings SET {', '.join(updates)} WHERE guild_id = ? AND kind = ?",
                    tuple(values),
                )
                await conn.commit()

        return await self.get_or_create_announcement_settings(guild_id, normalized_kind)

    async def set_prefix(self, guild_id: int, prefix: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET prefix = ? WHERE guild_id = ?",
                (prefix, guild_id),
            )
            await conn.commit()

    async def set_modlog_channel(self, guild_id: int, channel_id: int | None) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET modlog_channel_id = ? WHERE guild_id = ?",
                (channel_id, guild_id),
            )
            await conn.commit()

    async def set_server_context(self, guild_id: int, context: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET server_context = ? WHERE guild_id = ?",
                (context, guild_id),
            )
            await conn.commit()

    async def reset_ai_server_context(self, guild_id: int) -> None:
        await self.get_or_create_guild_settings(guild_id)
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET server_context = '' WHERE guild_id = ?",
                (guild_id,),
            )
            await conn.execute(
                "DELETE FROM server_context_entries WHERE guild_id = ?",
                (guild_id,),
            )
            await conn.execute(
                "DELETE FROM ai_conversation_turns WHERE guild_id = ?",
                (guild_id,),
            )
            await conn.commit()

    async def get_server_context_entries(self, guild_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, channel_id, channel_name, summary, created_at, updated_at
                FROM server_context_entries
                WHERE guild_id = ?
                ORDER BY updated_at DESC, channel_id DESC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _compose_server_context(entries: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for entry in entries:
            channel_name = str(entry.get("channel_name", "")).strip() or "unknown"
            channel_id = int(entry.get("channel_id", 0))
            summary = str(entry.get("summary", "")).strip()
            if not summary:
                continue
            blocks.append(
                f"Channel #{channel_name} ({channel_id})\n{summary}"
            )
        return "\n\n---\n\n".join(blocks).strip()

    async def upsert_server_context_entry(
        self,
        guild_id: int,
        channel_id: int,
        channel_name: str,
        summary: str,
        *,
        max_entries: int = 2,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now_iso = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO server_context_entries
                (guild_id, channel_id, channel_name, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id)
                DO UPDATE SET
                    channel_name = excluded.channel_name,
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    channel_id,
                    channel_name.strip() or "unknown",
                    summary.strip(),
                    now_iso,
                    now_iso,
                ),
            )

            if channel_id > 0:
                await conn.execute(
                    """
                    DELETE FROM server_context_entries
                    WHERE guild_id = ? AND channel_id = ?
                    """,
                    (guild_id, AI_INTERACTIONS_CONTEXT_CHANNEL_ID),
                )

            cursor = await conn.execute(
                """
                SELECT guild_id, channel_id, channel_name, summary, created_at, updated_at
                FROM server_context_entries
                WHERE guild_id = ?
                ORDER BY updated_at DESC, channel_id DESC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            all_entries = [dict(row) for row in rows]
            positive_entries = [
                entry for entry in all_entries if int(entry["channel_id"]) > 0
            ]
            has_real_context = bool(positive_entries)

            if channel_id == AI_INTERACTIONS_CONTEXT_CHANNEL_ID and has_real_context:
                await conn.execute(
                    """
                    DELETE FROM server_context_entries
                    WHERE guild_id = ? AND channel_id = ?
                    """,
                    (guild_id, AI_INTERACTIONS_CONTEXT_CHANNEL_ID),
                )
                all_entries = positive_entries

            if has_real_context:
                removed = (
                    positive_entries[max_entries:]
                    if len(positive_entries) > max_entries
                    else []
                )
                final_entries = positive_entries[:max_entries]
            else:
                removed = []
                final_entries = all_entries[:1]

            if removed:
                placeholders = ",".join("?" for _ in removed)
                values: list[Any] = [guild_id]
                values.extend(int(item["channel_id"]) for item in removed)
                await conn.execute(
                    f"""
                    DELETE FROM server_context_entries
                    WHERE guild_id = ?
                      AND channel_id IN ({placeholders})
                    """,
                    tuple(values),
                )

            combined_context = self._compose_server_context(final_entries)
            await conn.execute(
                "UPDATE guild_settings SET server_context = ? WHERE guild_id = ?",
                (combined_context, guild_id),
            )
            await conn.commit()
            return final_entries, removed

    async def set_muted_role(self, guild_id: int, role_id: int | None) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET muted_role_id = ? WHERE guild_id = ?",
                (role_id, guild_id),
            )
            await conn.commit()

    async def set_language(self, guild_id: int, language_code: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE guild_settings SET language_code = ? WHERE guild_id = ?",
                (language_code, guild_id),
            )
            await conn.commit()

    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, reason, self._now_iso()),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def get_warnings(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, user_id, moderator_id, reason, created_at
                FROM warnings
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC
                """,
                (guild_id, user_id),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.commit()
            return int(cursor.rowcount or 0)

    async def delete_warning_by_id(
        self,
        guild_id: int,
        warning_id: int,
    ) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, user_id, moderator_id, reason, created_at
                FROM warnings
                WHERE guild_id = ? AND id = ?
                LIMIT 1
                """,
                (guild_id, warning_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            row_dict = dict(row)
            await conn.execute("DELETE FROM warnings WHERE id = ?", (warning_id,))
            await conn.commit()
            return row_dict

    async def delete_warning_by_reason(
        self,
        guild_id: int,
        reason_query: str,
    ) -> dict[str, Any] | None:
        normalized = reason_query.strip()
        if not normalized:
            return None
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, user_id, moderator_id, reason, created_at
                FROM warnings
                WHERE guild_id = ? AND LOWER(reason) LIKE '%' || LOWER(?) || '%'
                ORDER BY id DESC
                LIMIT 1
                """,
                (guild_id, normalized),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            row_dict = dict(row)
            await conn.execute("DELETE FROM warnings WHERE id = ?", (int(row_dict["id"]),))
            await conn.commit()
            return row_dict

    async def upsert_temp_action(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        expires_at: int,
        duration_input: str,
        reason: str,
        moderator_id: int,
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO temp_actions
                (guild_id, user_id, action, expires_at, duration_input, reason, moderator_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, action)
                DO UPDATE SET
                    expires_at = excluded.expires_at,
                    duration_input = excluded.duration_input,
                    reason = excluded.reason,
                    moderator_id = excluded.moderator_id,
                    created_at = excluded.created_at
                """,
                (
                    guild_id,
                    user_id,
                    action,
                    expires_at,
                    duration_input,
                    reason,
                    moderator_id,
                    self._now_iso(),
                ),
            )
            await conn.commit()

    async def get_due_temp_actions(self, now_ts: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, user_id, action, expires_at, duration_input, reason, moderator_id, created_at
                FROM temp_actions
                WHERE expires_at <= ?
                ORDER BY expires_at ASC
                """,
                (now_ts,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_temp_action(self, action_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute("DELETE FROM temp_actions WHERE id = ?", (action_id,))
            await conn.commit()

    async def create_reminder(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        message: str,
        remind_at: int,
    ) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO reminders
                (guild_id, user_id, channel_id, message, remind_at, mention_in_channel, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (guild_id, user_id, channel_id, message, remind_at, self._now_iso()),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def set_reminder_mention(
        self,
        reminder_id: int,
        user_id: int,
        mention_in_channel: bool,
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                UPDATE reminders
                SET mention_in_channel = ?
                WHERE id = ? AND user_id = ?
                """,
                (1 if mention_in_channel else 0, reminder_id, user_id),
            )
            await conn.commit()

    async def get_due_reminders(self, now_ts: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, user_id, channel_id, message, remind_at, mention_in_channel, created_at
                FROM reminders
                WHERE remind_at <= ?
                ORDER BY remind_at ASC, id ASC
                """,
                (now_ts,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminder(self, reminder_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "DELETE FROM reminder_subscribers WHERE reminder_id = ?",
                (reminder_id,),
            )
            await conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            await conn.commit()

    async def add_reminder_subscriber(
        self,
        reminder_id: int,
        user_id: int,
        mention_in_channel: bool = True,
    ) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO reminder_subscribers
                (reminder_id, user_id, mention_in_channel, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    user_id,
                    1 if mention_in_channel else 0,
                    self._now_iso(),
                ),
            )
            await conn.commit()
            return bool(cursor.rowcount)

    async def get_reminder_subscribers(self, reminder_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, reminder_id, user_id, mention_in_channel, created_at
                FROM reminder_subscribers
                WHERE reminder_id = ?
                ORDER BY id ASC
                """,
                (reminder_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def reminder_exists(self, reminder_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM reminders WHERE id = ?",
                (reminder_id,),
            )
            row = await cursor.fetchone()
            return row is not None

    async def list_active_reminders_for_user(
        self,
        guild_id: int,
        user_id: int,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    r.id AS reminder_id,
                    r.message AS message,
                    r.remind_at AS remind_at,
                    1 AS is_owner
                FROM reminders r
                WHERE r.guild_id = ? AND r.user_id = ?

                UNION ALL

                SELECT
                    r.id AS reminder_id,
                    r.message AS message,
                    r.remind_at AS remind_at,
                    0 AS is_owner
                FROM reminders r
                INNER JOIN reminder_subscribers s ON s.reminder_id = r.id
                WHERE r.guild_id = ? AND s.user_id = ? AND r.user_id != ?

                ORDER BY remind_at ASC, reminder_id ASC
                LIMIT ?
                """,
                (guild_id, user_id, guild_id, user_id, user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_reminder_for_user(
        self,
        guild_id: int,
        user_id: int,
        reminder_id: int,
    ) -> str | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, user_id
                FROM reminders
                WHERE id = ? AND guild_id = ?
                """,
                (reminder_id, guild_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            owner_id = int(row["user_id"])
            if owner_id == user_id:
                await conn.execute(
                    "DELETE FROM reminder_subscribers WHERE reminder_id = ?",
                    (reminder_id,),
                )
                await conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                await conn.commit()
                return "owner_deleted"

            delete_cursor = await conn.execute(
                """
                DELETE FROM reminder_subscribers
                WHERE reminder_id = ? AND user_id = ?
                """,
                (reminder_id, user_id),
            )
            await conn.commit()
            if delete_cursor.rowcount:
                return "subscriber_removed"
            return None

    async def list_color_roles(self, guild_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, role_id, color_name, hex_code, display_order, created_at
                FROM color_roles
                WHERE guild_id = ?
                ORDER BY display_order ASC, id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def count_color_roles(self, guild_id: int) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS total FROM color_roles WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            return int(row["total"]) if row else 0

    async def get_color_role_by_name(
        self,
        guild_id: int,
        color_name: str,
    ) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, role_id, color_name, hex_code, display_order, created_at
                FROM color_roles
                WHERE guild_id = ? AND LOWER(color_name) = LOWER(?)
                ORDER BY display_order ASC, id ASC
                LIMIT 1
                """,
                (guild_id, color_name.strip()),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def upsert_color_role(
        self,
        guild_id: int,
        role_id: int,
        color_name: str,
        hex_code: str,
        *,
        display_order: int | None = None,
    ) -> None:
        clean_name = color_name.strip()
        if not clean_name:
            return
        async with self._connect() as conn:
            order_value = display_order
            if order_value is None:
                cursor = await conn.execute(
                    """
                    SELECT display_order
                    FROM color_roles
                    WHERE guild_id = ? AND LOWER(color_name) = LOWER(?)
                    LIMIT 1
                    """,
                    (guild_id, clean_name),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    order_value = int(existing["display_order"])
                else:
                    cursor = await conn.execute(
                        """
                        SELECT COALESCE(MAX(display_order), 0) + 1 AS next_order
                        FROM color_roles
                        WHERE guild_id = ?
                        """,
                        (guild_id,),
                    )
                    next_row = await cursor.fetchone()
                    order_value = int(next_row["next_order"]) if next_row else 1

            await conn.execute(
                """
                INSERT INTO color_roles
                (guild_id, role_id, color_name, hex_code, display_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, color_name)
                DO UPDATE SET
                    role_id = excluded.role_id,
                    hex_code = excluded.hex_code,
                    display_order = excluded.display_order
                """,
                (
                    guild_id,
                    role_id,
                    clean_name,
                    hex_code.strip().upper(),
                    int(order_value),
                    self._now_iso(),
                ),
            )
            await conn.commit()

    async def delete_color_role_by_name(
        self,
        guild_id: int,
        color_name: str,
    ) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, guild_id, role_id, color_name, hex_code, display_order, created_at
                FROM color_roles
                WHERE guild_id = ? AND LOWER(color_name) = LOWER(?)
                ORDER BY display_order ASC, id ASC
                LIMIT 1
                """,
                (guild_id, color_name.strip()),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            row_dict = dict(row)
            await conn.execute("DELETE FROM color_roles WHERE id = ?", (row_dict["id"],))
            await self._normalize_color_role_order(conn, guild_id)
            await conn.commit()
            return row_dict

    async def delete_color_role_by_role_id(self, guild_id: int, role_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM color_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )
            await self._normalize_color_role_order(conn, guild_id)
            await conn.commit()
            return bool(cursor.rowcount)

    async def clear_color_roles(self, guild_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute("DELETE FROM color_roles WHERE guild_id = ?", (guild_id,))
            await conn.commit()

    async def get_color_panel(self, guild_id: int) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, channel_id, message_id, created_at, updated_at
                FROM color_panels
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def upsert_color_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int | None,
    ) -> None:
        now_iso = self._now_iso()
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO color_panels (guild_id, channel_id, message_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id)
                DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, channel_id, message_id, now_iso, now_iso),
            )
            await conn.commit()

    async def clear_color_panel(self, guild_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute("DELETE FROM color_panels WHERE guild_id = ?", (guild_id,))
            await conn.commit()

    async def _normalize_color_role_order(
        self,
        conn: aiosqlite.Connection,
        guild_id: int,
    ) -> None:
        cursor = await conn.execute(
            """
            SELECT id
            FROM color_roles
            WHERE guild_id = ?
            ORDER BY display_order ASC, id ASC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        for index, row in enumerate(rows, start=1):
            await conn.execute(
                "UPDATE color_roles SET display_order = ? WHERE id = ?",
                (index, int(row["id"])),
            )

    async def get_or_create_filter_settings(self, guild_id: int) -> dict[str, Any]:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO message_filters
                (guild_id, anti_spam_enabled, anti_link_enabled, spam_threshold, spam_window_seconds, created_at)
                VALUES (?, 1, 0, 5, 8, ?)
                """,
                (guild_id, self._now_iso()),
            )
            await conn.commit()

            cursor = await conn.execute(
                """
                SELECT guild_id, anti_spam_enabled, anti_link_enabled, spam_threshold, spam_window_seconds
                FROM message_filters
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {
                    "guild_id": guild_id,
                    "anti_spam_enabled": 1,
                    "anti_link_enabled": 0,
                    "spam_threshold": 5,
                    "spam_window_seconds": 8,
                }
            return dict(row)

    async def set_anti_spam(self, guild_id: int, enabled: bool) -> None:
        await self.get_or_create_filter_settings(guild_id)
        async with self._connect() as conn:
            await conn.execute(
                """
                UPDATE message_filters
                SET anti_spam_enabled = ?
                WHERE guild_id = ?
                """,
                (1 if enabled else 0, guild_id),
            )
            await conn.commit()

    async def set_anti_link(self, guild_id: int, enabled: bool) -> None:
        await self.get_or_create_filter_settings(guild_id)
        async with self._connect() as conn:
            await conn.execute(
                """
                UPDATE message_filters
                SET anti_link_enabled = ?
                WHERE guild_id = ?
                """,
                (1 if enabled else 0, guild_id),
            )
            await conn.commit()

    async def add_ai_channel(self, guild_id: int, channel_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "DELETE FROM ai_channels WHERE guild_id = ? AND channel_id IN (?, ?)",
                (guild_id, AI_CHANNEL_ALL_MARKER, AI_CHANNEL_NONE_MARKER),
            )
            await conn.execute(
                """
                INSERT OR IGNORE INTO ai_channels (guild_id, channel_id, created_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, channel_id, self._now_iso()),
            )
            await conn.commit()

    async def remove_ai_channel(self, guild_id: int, channel_id: int) -> None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT channel_id FROM ai_channels WHERE guild_id = ?",
                (guild_id,),
            )
            rows = await cursor.fetchall()
            existing_ids = {int(row["channel_id"]) for row in rows}
            if not existing_ids or AI_CHANNEL_ALL_MARKER in existing_ids:
                # Unrestricted mode: keep unrestricted semantics.
                return

            await conn.execute(
                "DELETE FROM ai_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            cursor = await conn.execute(
                "SELECT channel_id FROM ai_channels WHERE guild_id = ? AND channel_id > 0",
                (guild_id,),
            )
            remaining = await cursor.fetchall()
            if not remaining:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_channels (guild_id, channel_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (guild_id, AI_CHANNEL_NONE_MARKER, self._now_iso()),
                )
            await conn.commit()

    async def clear_ai_channels(self, guild_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute("DELETE FROM ai_channels WHERE guild_id = ?", (guild_id,))
            await conn.execute(
                """
                INSERT OR IGNORE INTO ai_channels (guild_id, channel_id, created_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, AI_CHANNEL_ALL_MARKER, self._now_iso()),
            )
            await conn.commit()

    async def list_ai_channels(self, guild_id: int) -> list[int]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT channel_id
                FROM ai_channels
                WHERE guild_id = ? AND channel_id > 0
                ORDER BY channel_id
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [int(row["channel_id"]) for row in rows]

    async def get_ai_channel_scope(self, guild_id: int) -> tuple[str, list[int]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT channel_id FROM ai_channels WHERE guild_id = ?",
                (guild_id,),
            )
            rows = await cursor.fetchall()
            ids = [int(row["channel_id"]) for row in rows]

        if not ids or AI_CHANNEL_ALL_MARKER in ids:
            return "all", []
        allowed = sorted(channel_id for channel_id in ids if channel_id > 0)
        if not allowed:
            return "none", []
        return "restricted", allowed

    async def is_ai_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        scope, channel_ids = await self.get_ai_channel_scope(guild_id)
        if scope == "all":
            return True
        if scope == "none":
            return False
        return channel_id in channel_ids

    async def add_ai_conversation_turn(
        self,
        guild_id: int,
        channel_id: int,
        role: str,
        speaker: str,
        content: str,
        message_id: int | None = None,
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO ai_conversation_turns
                (guild_id, channel_id, role, speaker, content, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, channel_id, role, speaker, content, message_id, self._now_iso()),
            )
            # Keep a rolling window per channel to avoid unbounded growth.
            await conn.execute(
                """
                DELETE FROM ai_conversation_turns
                WHERE guild_id = ? AND channel_id = ?
                  AND id NOT IN (
                    SELECT id FROM ai_conversation_turns
                    WHERE guild_id = ? AND channel_id = ?
                    ORDER BY id DESC
                    LIMIT 220
                  )
                """,
                (guild_id, channel_id, guild_id, channel_id),
            )
            await conn.commit()

    async def is_ai_assistant_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT 1
                FROM ai_conversation_turns
                WHERE guild_id = ?
                  AND channel_id = ?
                  AND message_id = ?
                  AND role = 'assistant'
                LIMIT 1
                """,
                (guild_id, channel_id, message_id),
            )
            return await cursor.fetchone() is not None

    async def get_ai_conversation_history(
        self,
        guild_id: int,
        channel_id: int,
        *,
        limit: int = 120,
    ) -> list[dict[str, str]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT role, speaker, content
                FROM ai_conversation_turns
                WHERE guild_id = ? AND channel_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, channel_id, limit),
            )
            rows = await cursor.fetchall()
            rows = list(reversed(rows))
            return [
                {
                    "role": str(row["role"]),
                    "speaker": str(row["speaker"]),
                    "content": str(row["content"]),
                }
                for row in rows
            ]

    async def get_recent_ai_conversation_turns(
        self,
        guild_id: int,
        since_iso: str,
        *,
        limit: int = 400,
    ) -> list[dict[str, str]]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT guild_id, channel_id, role, speaker, content, created_at
                FROM ai_conversation_turns
                WHERE guild_id = ? AND created_at >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, since_iso, limit),
            )
            rows = await cursor.fetchall()
            rows = list(reversed(rows))
            return [
                {
                    "guild_id": str(row["guild_id"]),
                    "channel_id": str(row["channel_id"]),
                    "role": str(row["role"]),
                    "speaker": str(row["speaker"]),
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    async def _ensure_guild_settings_columns(self, conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(guild_settings)")
        rows = await cursor.fetchall()
        column_names = {str(row[1]) for row in rows}
        if "language_code" not in column_names:
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN language_code TEXT NOT NULL DEFAULT 'en'"
            )

    async def _ensure_ai_conversation_columns(self, conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(ai_conversation_turns)")
        rows = await cursor.fetchall()
        column_names = {str(row[1]) for row in rows}
        if "message_id" not in column_names:
            await conn.execute(
                "ALTER TABLE ai_conversation_turns ADD COLUMN message_id INTEGER"
            )

