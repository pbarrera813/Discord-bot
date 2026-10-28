import asyncio
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

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
        self.assertIn("-# Author: 123\n-# Message ID: 456", metadata["content"])
        self.assertIn(f"\n-# <t:{int(date.timestamp())}:f>", metadata["content"])
        self.assertEqual(components[-1]["type"], 1)
        self.assertEqual(components[-1]["components"][1]["url"], buttons.children[1].url)
        self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})

        from bot import DiscordModBot
        audit = SimpleNamespace(db=SimpleNamespace(), _guild_lang=AsyncMock(return_value="es"))
        actor = SimpleNamespace(id=123, mention="<@123>")
        source = SimpleNamespace(id=20, mention="<#20>")
        guild = SimpleNamespace(id=1, get_channel=lambda _: SimpleNamespace(send=AsyncMock()))
        options = DiscordModBot._format_app_command_options({"options": [
            {"name": "player", "options": [
                {"name": "player", "value": "Rodrigo Aguirre"}, {"name": "team", "value": "Tigres UANL"},
                {"name": "league", "value": "ligamx"}, {"name": "code", "value": "PRIVATE CODE"},
                {"name": "text", "value": "PRIVATE MESSAGE"},
            ]},
        ]}, "es")
        with patch("bot.send_modlog_embed", new_callable=AsyncMock) as send:
            await DiscordModBot._send_command_audit_log(audit, guild=guild, actor=actor, channel=source,
                command_name="football player", command_input=options, command_source="slash")
        command_card = send.await_args.args[2]
        self.assertEqual(command_card.title, "/football player")
        self.assertIn("<@123> · <#20>", command_card.description)
        self.assertIn("**Jugador:** Rodrigo Aguirre", send.await_args.kwargs["body"])
        self.assertNotIn("PRIVATE", options)
        self.assertEqual(len(command_card.fields), 0)

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
        self.assertIn("-# Author: 123\n-# Message ID: 456", text)
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

    async def test_channel_event_delivery_is_correlated_and_retryable(self):
        guild = SimpleNamespace(id=1, get_channel=lambda _: None)
        db = SimpleNamespace(get_guild_settings=AsyncMock(return_value=SimpleNamespace(modlog_channel_id=None)))
        self.assertIsNone(await send_modlog_embed(guild, db, discord.Embed(title="Audit")))

        from cogs.moderation import ModerationCog
        from services.admin_actions import AdminActionService
        for gateway_first in (True, False):
            with self.subTest(gateway_first=gateway_first):
                message = SimpleNamespace(id=900, edit=AsyncMock())
                message.edit.return_value = message
                transport = SimpleNamespace(send=AsyncMock(return_value=message))
                db = SimpleNamespace(get_guild_settings=AsyncMock(return_value=SimpleNamespace(modlog_channel_id=99, language_code="es")))
                bot = SimpleNamespace(db=db)
                cog = object.__new__(ModerationCog)
                cog.bot = bot
                service = AdminActionService(bot)
                perms = SimpleNamespace(administrator=True)
                actor = SimpleNamespace(id=123, mention="<@123>", guild_permissions=perms)
                guild = SimpleNamespace(id=1, me=SimpleNamespace(guild_permissions=perms), text_channels=[], get_channel=lambda _: transport)
                channel = SimpleNamespace(id=20, name="test", mention="<#20>", guild=guild, permissions_for=lambda _: perms)
                async def create(*_args, **_kwargs):
                    if gateway_first:
                        await cog.on_guild_channel_create(channel)
                    return channel
                async def delete(**_kwargs):
                    if gateway_first:
                        await cog.on_guild_channel_delete(channel)
                guild.create_text_channel = create
                channel.delete = delete
                self.assertTrue((await service.create_text_channel(guild, actor, "test")).success)
                await cog.on_guild_channel_create(channel)
                created = message.edit.await_args if gateway_first else transport.send.await_args
                created_text = [c["content"] for c in flatten(created.kwargs["view"].to_components()) if c["type"] == 10]
                self.assertIn("#test", created_text)
                self.assertNotIn("<#20>", "\n".join(created_text))
                self.assertNotIn("Destino", "\n".join(created_text))
                self.assertTrue((await service.delete_channel(guild, actor, channel, current_channel_id=99)).success)
                await cog.on_guild_channel_delete(channel)
                self.assertEqual(transport.send.await_count, 2)
                self.assertEqual(message.edit.await_count, 2 if gateway_first else 0)
                enriched = message.edit.await_args if gateway_first else transport.send.await_args
                rendered = repr(enriched.kwargs["view"].to_components())
                self.assertIn("Canal eliminado", rendered)
                self.assertIn("Responsable", rendered)
                self.assertIn("<@123>", rendered)
                self.assertIn("#test", rendered)
                self.assertNotIn("Destino", rendered)
                self.assertNotIn("<#20>", rendered)
                self.assertIn("-# Channel ID: 20\n-# User ID: 123\n-# <t:", rendered.replace("\\n", "\n"))

                other = SimpleNamespace(id=21, name="manual", mention="<#21>", guild=guild)
                await asyncio.gather(cog.on_guild_channel_create(other), cog.on_guild_channel_create(other))
                self.assertEqual(transport.send.await_count, 3)
                components = transport.send.await_args.kwargs["view"].to_components()[0]["components"]
                self.assertFalse(any(c["type"] == 14 for c in components))

                response = SimpleNamespace(status=403, reason="Forbidden")
                transport.send.side_effect = [discord.Forbidden(response, "denied"), message]
                other.id = 22
                await cog.on_guild_channel_create(other)
                await cog.on_guild_channel_create(other)
                self.assertEqual(transport.send.await_count, 5)


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
