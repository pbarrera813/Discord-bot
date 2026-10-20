from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import discord

from cogs.moderation import ModerationLogView
from services.modlog import send_modlog_embed


def flatten(components):
    for component in components:
        yield component
        yield from flatten(component.get("components", []))
        if "accessory" in component:
            yield component["accessory"]


class ModlogCardTests(IsolatedAsyncioTestCase):
    async def deliver(self, embed, **kwargs):
        sent = SimpleNamespace(id=900)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent))
        guild = SimpleNamespace(id=1, get_channel=lambda _: channel)
        db = SimpleNamespace(get_guild_settings=AsyncMock(return_value=SimpleNamespace(modlog_channel_id=20)))
        result = await send_modlog_embed(guild, db, embed, **kwargs)
        return result, sent, channel.send.await_args.kwargs

    async def test_card_metadata_and_date_follow_image_with_buttons_outside(self):
        date = datetime(2026, 9, 28, 16, 10, tzinfo=timezone.utc)
        embed = discord.Embed(title="Message Edited", description="From: <@123>    In: <#20>", timestamp=date, color=discord.Color.blue())
        embed.add_field(name="Before", value="old", inline=False)
        embed.add_field(name="After", value="new", inline=False)
        embed.set_image(url="https://example.invalid/image.png")
        embed.set_footer(text="Author: 123 | Message ID: 456")
        buttons = ModerationLogView(jump_url="https://discord.com/channels/1/20/456")
        result, sent, payload = await self.deliver(embed, view=buttons)
        self.assertIs(result, sent)
        self.assertNotIn("embed", payload)
        self.assertIsInstance(payload["view"], discord.ui.LayoutView)
        components = payload["view"].to_components()
        self.assertEqual(components[0]["type"], 17)
        card = components[0]
        self.assertEqual(card["accent_color"], discord.Color.blue().value)
        self.assertTrue(any(c["type"] == 14 for c in card["components"]))
        separator_index = next(i for i, c in enumerate(card["components"]) if c["type"] == 14)
        self.assertIn("From: <@123>", repr(card["components"][:separator_index]))
        self.assertIn("Before", repr(card["components"][separator_index + 1:]))
        gallery_index = next(i for i, c in enumerate(card["components"]) if c["type"] == 12)
        metadata = card["components"][-1]
        self.assertEqual(metadata["type"], 10)
        self.assertGreater(len(card["components"]) - 1, gallery_index)
        self.assertIn("-# Author: 123 | Message ID: 456", metadata["content"])
        self.assertIn(f"\n-# <t:{int(date.timestamp())}:f>", metadata["content"])
        self.assertEqual(components[-1]["type"], 1)
        self.assertEqual(components[-1]["components"][1]["url"], buttons.children[1].url)
        self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})

    async def test_long_content_reserves_metadata_and_timestamp(self):
        embed = discord.Embed(title="Audit", description="a" * 4096, timestamp=datetime.now(timezone.utc))
        for index in range(25):
            embed.add_field(name=f"Field {index}", value="b" * 1024)
        embed.set_footer(text="Author: 123 | Message ID: 456")
        _, _, payload = await self.deliver(embed)
        components = list(flatten(payload["view"].to_components()))
        text = "".join(c["content"] for c in components if c["type"] == 10)
        self.assertLessEqual(len(text), 4000)
        self.assertLessEqual(len(components), 40)
        self.assertIn("Author: 123 | Message ID: 456", text)
        self.assertIn(":f>", text)
        self.assertTrue("truncat" in text.lower() or "omitt" in text.lower() or "…" in text or "..." in text)

    async def test_new_card_user_id_button_ignores_forged_body(self):
        embed = discord.Embed(title="Message Deleted", description="Author: 999 | Message ID: 1")
        embed.set_footer(text="Author: 123 | Message ID: 456")
        _, _, payload = await self.deliver(embed, view=ModerationLogView())
        components = [discord.components._component_factory(c) for c in payload["view"].to_components()]
        interaction = SimpleNamespace(message=SimpleNamespace(embeds=[], components=components), response=SimpleNamespace(send_message=AsyncMock()))
        await ModerationLogView().children[0].callback(interaction)
        interaction.response.send_message.assert_awaited_once_with("123", ephemeral=True)

    async def test_missing_destination_returns_none_without_delivery(self):
        guild = SimpleNamespace(id=1, get_channel=lambda _: None)
        db = SimpleNamespace(get_guild_settings=AsyncMock(return_value=SimpleNamespace(modlog_channel_id=None)))
        self.assertIsNone(await send_modlog_embed(guild, db, discord.Embed(title="Audit")))


    async def test_uploaded_gallery_preserves_spoilers_and_metadata(self):
        files = [discord.File(BytesIO(b"png"), filename="first.png"), discord.File(BytesIO(b"gif"), filename="second.gif", spoiler=True)]
        embed = discord.Embed(title="Message Deleted", description="From: <@123> In: <#20>", timestamp=datetime.now(timezone.utc))
        embed.set_footer(text="Author: 123 | Message ID: 456")
        _, _, payload = await self.deliver(embed, files=files, image_urls=[f"attachment://{f.filename}" for f in files], body="")
        components = list(flatten(payload["view"].to_components()))
        gallery = next(c for c in components if c["type"] == 12)
        self.assertEqual([item["media"]["url"] for item in gallery["items"]], [f"attachment://{f.filename}" for f in files])
        self.assertFalse(gallery["items"][0]["spoiler"])
        self.assertTrue(gallery["items"][1]["spoiler"])
        self.assertEqual(payload["files"], files)
