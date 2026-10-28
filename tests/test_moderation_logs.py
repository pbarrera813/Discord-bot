from __future__ import annotations

from types import SimpleNamespace
import asyncio
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from services.database import Database

import discord

from cogs.moderation import (
    ModerationCog,
    ModerationLogView,
    _modlog_diff_value,
)


class FakeChannel:
    def __init__(self, *, previous=None, history_error=None):
        self.id = 20
        self.mention = "<#20>"
        self.previous = previous
        self.history_error = history_error

    def history(self, *, limit, before):
        async def iterator():
            if self.history_error is not None:
                raise self.history_error
            if self.previous is not None:
                yield self.previous

        return iterator()


def make_author(author_id=123):
    return SimpleNamespace(
        id=author_id,
        bot=False,
        mention=f"<@{author_id}>",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
    )


def make_cog():
    cog = object.__new__(ModerationCog)
    cog.bot = SimpleNamespace(
        db=SimpleNamespace(
            get_guild_settings=AsyncMock(return_value=SimpleNamespace(language_code="en", modlog_channel_id=99)),
            get_modlog_images=AsyncMock(return_value=None),
            save_modlog_images=AsyncMock(),
            delete_modlog_images=AsyncMock(),
            prune_modlog_images=AsyncMock(),
        )
    )
    cog._modlog_delete_locks = {}
    cog._modlog_deleted_ids = set()
    cog._modlog_capture_tasks = {}
    cog._modlog_deleted_order = []
    cog._modlog_deleting_ids = set()
    return cog


def make_message(*, content="hello", message_id=456, channel=None, attachments=None, jump_url=""):
    return SimpleNamespace(
        id=message_id,
        content=content,
        guild=SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        channel=channel or FakeChannel(),
        author=make_author(),
        attachments=attachments or [],
        jump_url=jump_url,
    )


async def delete_message(cog, message):
    cog.bot.get_guild = lambda _guild_id: message.guild
    await cog.on_raw_message_delete(SimpleNamespace(
        guild_id=message.guild.id, message_id=message.id, cached_message=message, channel_id=message.channel.id,
    ))


class ModerationLogDiffTests(IsolatedAsyncioTestCase):
    def test_changed_single_character_is_underlined(self):
        before = _modlog_diff_value("hello", "hallo", before_side=True, empty_text="empty")
        after = _modlog_diff_value("hello", "hallo", before_side=False, empty_text="empty")
        self.assertEqual(before, "h__e__llo")
        self.assertEqual(after, "h__a__llo")

    def test_sentence_insertion_and_deletion_mark_only_changed_spans(self):
        self.assertEqual(
            _modlog_diff_value("hello world", "hello brave world", before_side=False, empty_text="empty"),
            "hello __brave __world",
        )
        self.assertEqual(
            _modlog_diff_value("hello brave world", "hello world", before_side=True, empty_text="empty"),
            "hello __brave __world",
        )


class ModerationLogListenerTests(IsolatedAsyncioTestCase):
    async def test_edit_embed_and_buttons_use_shared_single_delivery(self):
        cog = make_cog()
        before = make_message(content="This is the original audit message.", message_id=456)
        after = make_message(
            content="This is the revised audit message.",
            message_id=456,
            channel=before.channel,
            jump_url="https://discord.com/channels/1/20/456",
        )

        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await cog.on_message_edit(before, after)

        send.assert_awaited_once()
        embed = send.await_args.args[2]
        view = send.await_args.kwargs["view"]
        self.assertEqual(embed.title, "Message Edited")
        self.assertIn("From: <@123>", embed.description)
        self.assertEqual(embed.fields[0].name, "Before")
        self.assertEqual(embed.fields[1].name, "After")
        self.assertIn("__", embed.fields[0].value)
        self.assertIn("__", embed.fields[1].value)
        self.assertEqual(embed.fields[0].value.replace("__", ""), before.content)
        self.assertEqual(embed.fields[1].value.replace("__", ""), after.content)
        self.assertEqual(embed.footer.text, "Author: 123 | Message ID: 456")
        self.assertEqual(view.children[0].custom_id, ModerationLogView.GET_USER_ID_CUSTOM_ID)
        self.assertEqual(view.children[1].url, after.jump_url)
        self.assertFalse(view.children[1].disabled)

    async def test_delete_embed_preserves_attachment_and_context_button(self):
        cog = make_cog()
        previous = make_message(message_id=455, jump_url="https://discord.com/channels/1/20/455")
        channel = FakeChannel(previous=previous)
        attachment = SimpleNamespace(
            id=777,
            size=3,
            content_type="image/png",
            filename="photo.png",
            proxy_url="https://cdn.example/photo.png",
            url="https://cdn.example/photo-original.png",
            read=AsyncMock(return_value=b"png"),
            is_spoiler=lambda: False,
        )
        message = make_message(content="", channel=channel, attachments=[attachment])

        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await delete_message(cog, message)

        send.assert_awaited_once()
        embed = send.await_args.args[2]
        view = send.await_args.kwargs["view"]
        self.assertEqual(embed.title, "Message Deleted")
        self.assertEqual(embed.footer.text, "Author: 123 | Message ID: 456")
        self.assertEqual(len(embed.fields), 0)
        self.assertNotIn("[Image attachment]", repr(embed.to_dict()))
        self.assertEqual(send.await_args.kwargs["body"], "")
        files = send.await_args.kwargs["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].fp.getvalue(), b"png")
        self.assertEqual(send.await_args.kwargs["image_urls"], [f"attachment://{files[0].filename}"])
        self.assertNotIn(attachment.proxy_url, repr(embed.to_dict()))
        self.assertEqual(view.children[1].label, "Jump to Context")
        self.assertEqual(view.children[1].url, previous.jump_url)


    async def test_get_user_id_button_responds_ephemerally_with_footer_id(self):
        view = ModerationLogView()
        embed = discord.Embed()
        embed.set_footer(text="Author: 987654321 | Message ID: 456")
        interaction = SimpleNamespace(
            message=SimpleNamespace(embeds=[embed]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await view.children[0].callback(interaction)

        interaction.response.send_message.assert_awaited_once_with("987654321", ephemeral=True)


    async def test_bot_messages_and_unchanged_edits_are_ignored(self):
        cog = make_cog()
        bot_message = make_message()
        bot_message.author.bot = True
        same_before = make_message(content="same")
        same_after = make_message(content="same", channel=same_before.channel)

        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await delete_message(cog, bot_message)
            await cog.on_message_edit(same_before, same_after)

        send.assert_not_awaited()


    async def test_failed_image_recovery_does_not_send_broken_url(self):
        cog = make_cog()
        response = SimpleNamespace(status=404, reason="Not Found")
        attachment = SimpleNamespace(id=777, filename="lost.png", size=3, content_type="image/png", read=AsyncMock(side_effect=discord.NotFound(response, "gone")), is_spoiler=lambda: False, url="", proxy_url="")
        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await delete_message(cog, make_message(content="retained text", attachments=[attachment]))
        self.assertFalse(send.await_args.kwargs.get("files"))
        self.assertFalse(send.await_args.kwargs.get("image_urls"))
        body = send.await_args.kwargs["body"]
        self.assertIn("retained text", body)
        self.assertIn("lost.png", body)
        self.assertEqual(attachment.read.await_count, 2)
        self.assertEqual([c.kwargs["use_cached"] for c in attachment.read.await_args_list], [True, False])


    async def test_oversized_deleted_image_is_not_downloaded(self):
        cog = make_cog()
        attachment = SimpleNamespace(id=7, filename="large.png", content_type="image/png", size=11 * 1024 * 1024, read=AsyncMock(return_value=b"png"), is_spoiler=lambda: False, url="", proxy_url="")
        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await delete_message(cog, make_message(attachments=[attachment]))
        attachment.read.assert_not_awaited()
        self.assertFalse(send.await_args.kwargs.get("files"))
        self.assertIn("large.png", send.await_args.kwargs["body"])


class ModerationImageLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.db = Database(str(Path(self.directory.name) / "logs.db"))
        await self.db.init()
        self.db.get_guild_settings = AsyncMock(return_value=SimpleNamespace(language_code="en", modlog_channel_id=99))
        self.channel = FakeChannel()
        self.guild = SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024, get_channel=lambda _: self.channel)
        self.cog = self.new_cog()

    def new_cog(self):
        cog = make_cog()
        cog.bot.db = self.db
        cog.bot.get_guild = lambda _: self.guild
        cog.bot.get_channel = lambda _: self.channel
        return cog

    async def asyncTearDown(self):
        self.directory.cleanup()

    def message(self, *, content="caption", attachment_id=7):
        attachment = SimpleNamespace(id=attachment_id, filename="photo.png", content_type="image/png", size=3, read=AsyncMock(return_value=b"png"), is_spoiler=lambda: False, url="https://example.invalid/original", proxy_url="https://example.invalid/proxy")
        message = make_message(content=content, channel=self.channel, attachments=[attachment])
        message.guild = self.guild
        return message

    def deleted_payload(self, message, *, cached=False):
        return SimpleNamespace(guild_id=1, channel_id=20, message_id=message.id, cached_message=message if cached else None)

    async def capture(self, message):
        await self.cog.on_message(message)
        pending = list(self.cog._modlog_capture_tasks.values())
        if pending:
            await asyncio.gather(*pending)

    async def test_capture_restart_raw_delete_uploads_saved_bytes_and_releases_copy(self):
        message = self.message()
        self.channel.previous = make_message(message_id=455, jump_url="https://discord.com/channels/1/20/455")
        await self.capture(message)
        self.assertIsNotNone(await self.db.get_modlog_images(1, message.id))
        restarted = self.new_cog()
        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock, return_value=SimpleNamespace(id=900)) as send:
            await restarted.on_raw_message_delete(self.deleted_payload(message))
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["files"][0].fp.getvalue(), b"png")
        self.assertIn("caption", send.await_args.kwargs["body"])
        self.assertIn("Author: 123", send.await_args.args[2].footer.text)
        self.assertEqual(send.await_args.kwargs["view"].children[1].url, self.channel.previous.jump_url)
        self.assertIsNone(await self.db.get_modlog_images(1, message.id))


    async def test_text_only_messages_do_not_create_archive_rows(self):
        message = make_message(content="text only", channel=self.channel)
        message.guild = self.guild
        await self.capture(message)
        self.assertIsNone(await self.db.get_modlog_images(1, message.id))

    async def test_disabled_logging_does_not_recover_images_on_raw_edits_or_deletes(self):
        message = self.message()
        await self.capture(message)
        self.db.get_guild_settings.return_value.modlog_channel_id = None
        edit_payload = SimpleNamespace(guild_id=1, channel_id=20, message_id=message.id, cached_message=None, data={
            "content": "changed while disabled", "attachments": [{"id": "8", "filename": "new.png", "content_type": "image/png", "size": 3, "url": "https://example.invalid/new", "proxy_url": "https://example.invalid/proxy"}],
        })
        with patch.object(self.cog, "_download_modlog_image", new_callable=AsyncMock) as recovery, patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock) as send:
            await self.cog.on_raw_message_edit(edit_payload)
            await self.cog.on_raw_message_delete(self.deleted_payload(message))
        recovery.assert_not_awaited()
        send.assert_not_awaited()
        snapshot, images = await self.db.get_modlog_images(1, message.id)
        self.assertNotIn("changed while disabled", snapshot.values())
        self.assertEqual([image["data"] for image in images], [b"png"])

    async def test_delete_waits_for_in_flight_capture_and_avoids_duplicate(self):
        message = self.message()
        started, release = asyncio.Event(), asyncio.Event()

        async def read(**kwargs):
            started.set()
            await release.wait()
            return b"png"

        message.attachments[0].read = AsyncMock(side_effect=read)
        capture = asyncio.create_task(self.cog.on_message(message))
        await asyncio.wait_for(started.wait(), timeout=2)
        with patch("cogs.moderation.send_modlog_embed", new_callable=AsyncMock, return_value=SimpleNamespace(id=900)) as send:
            deletion = asyncio.create_task(self.cog.on_raw_message_delete(self.deleted_payload(message)))
            await asyncio.sleep(0)
            send.assert_not_awaited()
            release.set()
            await asyncio.wait_for(asyncio.gather(capture, deletion), timeout=3)
            await self.cog.on_raw_message_delete(self.deleted_payload(message, cached=True))
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["files"][0].fp.getvalue(), b"png")
