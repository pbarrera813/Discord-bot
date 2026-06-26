from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from cogs.admin import AdminCog
from services.xai_client import XAIClient


def _async_return(value):  # noqa: ANN001
    async def _inner(*_args, **_kwargs):  # noqa: ANN202
        return value

    return _inner


class _DummyMember:
    def __init__(self, *, user_id: int, name: str, display_name: str | None = None) -> None:
        self.id = user_id
        self.name = name
        self.display_name = display_name or name
        self.global_name = None


class _DummyChannel:
    def __init__(self, *, channel_id: int, name: str, position: int = 0) -> None:
        self.id = channel_id
        self.name = name
        self.position = position


class _DummyAttachment:
    def __init__(self, *, url: str, content_type: str | None, filename: str | None) -> None:
        self.url = url
        self.content_type = content_type
        self.filename = filename


class _DummyMessage:
    def __init__(
        self,
        *,
        attachments: list[_DummyAttachment],
        author_name: str = "Pablo",
        content: str = "",
        guild_id: int = 1,
        channel_id: int = 10,
        mentions: list[object] | None = None,
        webhook_id: int | None = None,
    ) -> None:
        self.attachments = attachments
        self.author = SimpleNamespace(display_name=author_name)
        self.content = content
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=channel_id)
        self.mentions = mentions or []
        self.webhook_id = webhook_id


class _DummyGuild:
    def __init__(self) -> None:
        self.members = [
            _DummyMember(user_id=100000000000000001, name="greatooau"),
            _DummyMember(user_id=100000000000000002, name="jonark_xd"),
        ]
        self.text_channels = [
            _DummyChannel(channel_id=200000000000000001, name="pruebas-bots", position=1),
            _DummyChannel(channel_id=200000000000000002, name="minecraft", position=2),
        ]
        self.emojis = []

    def get_channel(self, channel_id: int):  # noqa: ANN001
        for channel in self.text_channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_thread(self, channel_id: int):  # noqa: ANN001
        return None

    def get_member(self, user_id: int):  # noqa: ANN001
        for member in self.members:
            if member.id == user_id:
                return member
        return None

    def get_member_named(self, token: str):
        lowered = token.casefold()
        for member in self.members:
            if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
                return member
        return None

    async def query_members(self, *, query: str, limit: int = 5):
        lowered = query.casefold()
        matches = []
        for member in self.members:
            if lowered in member.name.casefold() or lowered in member.display_name.casefold():
                matches.append(member)
            if len(matches) >= limit:
                break
        return matches


class AIChatTaggingTests(unittest.IsolatedAsyncioTestCase):
    def test_active_chat_window_opens_for_channel(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))
        message = _DummyMessage(attachments=[], guild_id=1, channel_id=10)

        cog._activate_active_chat(message)

        self.assertTrue(cog._is_active_chat(message))

    async def test_reply_to_chat_response_is_direct_trigger(self) -> None:
        bot_user = SimpleNamespace(id=42)
        cog = AIChatCog(SimpleNamespace(user=bot_user))
        cog._remember_chat_response_message(9001)
        message = _DummyMessage(attachments=[], content="yeah exactly")
        replied = SimpleNamespace(id=9001, author=bot_user)

        self.assertTrue(await cog._is_chat_trigger(message, replied))

    async def test_active_followup_triggers_in_same_channel(self) -> None:
        db = SimpleNamespace(
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
        )
        cog = AIChatCog(SimpleNamespace(user=None, db=db))
        active = _DummyMessage(attachments=[], guild_id=1, channel_id=10)
        followup = _DummyMessage(
            attachments=[],
            guild_id=1,
            channel_id=10,
            content="that idea would actually work",
        )
        settings = SimpleNamespace(modlog_channel_id=None)
        cog._activate_active_chat(active)

        self.assertTrue(await cog._is_active_chat_followup(followup, prefix="!", settings=settings))

    async def test_active_followup_does_not_cross_channels_or_expiry(self) -> None:
        db = SimpleNamespace(
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
        )
        cog = AIChatCog(SimpleNamespace(user=None, db=db))
        settings = SimpleNamespace(modlog_channel_id=None)
        cog._active_chats[(1, 10)] = 0.0

        expired = _DummyMessage(attachments=[], guild_id=1, channel_id=10, content="still here")
        other_channel = _DummyMessage(attachments=[], guild_id=1, channel_id=11, content="still here")

        self.assertFalse(await cog._is_active_chat_followup(expired, prefix="!", settings=settings))
        self.assertFalse(await cog._is_active_chat_followup(other_channel, prefix="!", settings=settings))

    async def test_active_followup_ignores_commands_noise_and_webhooks(self) -> None:
        db = SimpleNamespace(
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
        )
        cog = AIChatCog(SimpleNamespace(user=None, db=db))
        settings = SimpleNamespace(modlog_channel_id=None)
        cog._activate_active_chat(_DummyMessage(attachments=[], guild_id=1, channel_id=10))

        command = _DummyMessage(attachments=[], guild_id=1, channel_id=10, content="!help")
        noise = _DummyMessage(attachments=[], guild_id=1, channel_id=10, content="ok")
        webhook = _DummyMessage(
            attachments=[],
            guild_id=1,
            channel_id=10,
            content="hello",
            webhook_id=123,
        )

        self.assertFalse(await cog._is_active_chat_followup(command, prefix="!", settings=settings))
        self.assertFalse(await cog._is_active_chat_followup(noise, prefix="!", settings=settings))
        self.assertFalse(await cog._is_active_chat_followup(webhook, prefix="!", settings=settings))

    async def test_active_followup_ignores_configured_module_channels(self) -> None:
        async def get_birthday(_guild_id: int) -> dict[str, int]:
            return {"channel_id": 11}

        async def get_announcement(_guild_id: int, kind: str) -> SimpleNamespace:
            return SimpleNamespace(channel_id=12 if kind == "welcome" else 13)

        db = SimpleNamespace(
            get_or_create_birthday_guild_settings=get_birthday,
            get_announcement_settings=get_announcement,
        )
        cog = AIChatCog(SimpleNamespace(user=None, db=db))
        settings = SimpleNamespace(modlog_channel_id=10)
        for channel_id in (10, 11, 12, 13):
            cog._activate_active_chat(_DummyMessage(attachments=[], guild_id=1, channel_id=channel_id))
            message = _DummyMessage(
                attachments=[],
                guild_id=1,
                channel_id=channel_id,
                content="this should not become passive chat",
            )
            self.assertFalse(await cog._is_active_chat_followup(message, prefix="!", settings=settings))

    def test_active_followup_uses_active_conversation_mode(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))

        self.assertEqual(cog._conversation_mode_for_trigger(None, is_active_followup=True), "active")

    def test_reaction_cooldown_blocks_repeated_suggestions(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))
        key = (1, 10)

        self.assertTrue(cog._consume_reaction_cooldown(key))
        self.assertFalse(cog._consume_reaction_cooldown(key))

    async def test_normalizes_malformed_user_mention(self) -> None:
        cog = AIChatCog(SimpleNamespace())
        guild = _DummyGuild()

        output = await cog._normalize_discord_references("Claro <@greatooau>", guild)
        self.assertIn("<@100000000000000001>", output)
        self.assertNotIn("<@greatooau>", output)

    async def test_dedupes_duplicate_channel_mentions(self) -> None:
        cog = AIChatCog(SimpleNamespace())
        guild = _DummyGuild()

        output = await cog._normalize_discord_references(
            "Estamos en #pruebas-bots! <#200000000000000001>",
            guild,
        )
        self.assertEqual(output.count("<#200000000000000001>"), 1)

    async def test_split_for_discord_chunks_without_data_loss(self) -> None:
        source = ("hola " * 1200).strip()
        chunks = AIChatCog._split_for_discord(source, limit=1900)
        self.assertGreater(len(chunks), 1)
        rebuilt = " ".join(chunk.strip() for chunk in chunks)
        self.assertEqual(rebuilt, source)

    async def test_conversation_history_dedupes_identical_turns(self) -> None:
        cog = AIChatCog(SimpleNamespace())
        key = cog._conversation_key(1, 2)
        cog._append_conversation_turn(
            key,
            role="assistant",
            speaker="Nitori",
            content="Hola",
        )
        cog._append_conversation_turn(
            key,
            role="assistant",
            speaker="Nitori",
            content="Hola",
        )
        history = cog._build_conversation_history(key)
        self.assertEqual(len(history), 1)

    async def test_visible_ai_output_strips_untrusted_history_marker(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))

        output = cog._sanitize_visible_ai_output(
            "[UNTRUSTED_ASSISTANT_HISTORY]\nNitori: claro, va"
        )

        self.assertEqual(output, "claro, va")

    async def test_visible_ai_output_strips_inline_untrusted_marker(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))

        output = cog._sanitize_visible_ai_output(
            "Va. [UNTRUSTED_USER_MESSAGE_FROM Pablo] seguimos con eso."
        )

        self.assertEqual(output, "Va. seguimos con eso.")

    async def test_clear_guild_history_removes_only_matching_guild(self) -> None:
        cog = AIChatCog(SimpleNamespace(user=None))
        keep_key = cog._conversation_key(2, 20)
        clear_key = cog._conversation_key(1, 10)
        cog._append_conversation_turn(clear_key, role="user", speaker="A", content="hola")
        cog._append_conversation_turn(keep_key, role="user", speaker="B", content="hola")

        cog.clear_guild_history(1)

        self.assertEqual(cog._build_conversation_history(clear_key), [])
        self.assertEqual(len(cog._build_conversation_history(keep_key)), 1)

    async def test_extracts_only_supported_chat_image_attachments(self) -> None:
        attachments = [
            _DummyAttachment(
                url="https://cdn.discordapp.com/image.png",
                content_type="image/png",
                filename="image.png",
            ),
            _DummyAttachment(
                url="https://cdn.discordapp.com/sticker.gif",
                content_type="image/gif",
                filename="sticker.gif",
            ),
            _DummyAttachment(
                url="https://cdn.discordapp.com/readme.txt",
                content_type="text/plain",
                filename="readme.txt",
            ),
        ]

        urls = AIChatCog._extract_supported_image_urls(attachments)

        self.assertEqual(urls, ["https://cdn.discordapp.com/image.png"])

    async def test_chat_image_context_includes_replied_message_images(self) -> None:
        current = _DummyMessage(attachments=[])
        replied = _DummyMessage(
            author_name="Sofi",
            attachments=[
                _DummyAttachment(
                    url="https://cdn.discordapp.com/replied.jpg",
                    content_type="image/jpeg",
                    filename="replied.jpg",
                )
            ],
        )

        context = AIChatCog._build_chat_image_context(current, replied)

        self.assertEqual(context.urls, ["https://cdn.discordapp.com/replied.jpg"])
        self.assertTrue(context.from_replied_message)
        self.assertIs(context.reaction_target, replied)

    async def test_chat_image_context_ignores_replied_gifs(self) -> None:
        current = _DummyMessage(attachments=[])
        replied = _DummyMessage(
            attachments=[
                _DummyAttachment(
                    url="https://cdn.discordapp.com/animated.gif",
                    content_type="image/gif",
                    filename="animated.gif",
                )
            ],
        )

        context = AIChatCog._build_chat_image_context(current, replied)

        self.assertEqual(context.urls, [])
        self.assertFalse(context.from_replied_message)
        self.assertIs(context.reaction_target, current)

    async def test_reaction_target_uses_current_message_without_replied_image(self) -> None:
        current = _DummyMessage(
            attachments=[
                _DummyAttachment(
                    url="https://cdn.discordapp.com/current.png",
                    content_type="image/png",
                    filename="current.png",
                )
            ]
        )
        replied = _DummyMessage(attachments=[])

        context = AIChatCog._build_chat_image_context(current, replied)

        self.assertEqual(context.urls, ["https://cdn.discordapp.com/current.png"])
        self.assertFalse(context.from_replied_message)
        self.assertIs(context.reaction_target, current)

    async def test_meme_prompt_with_attachment_keeps_command_hint(self) -> None:
        self.assertEqual(
            AIChatCog._detect_command_hint("meme custom top text"),
            "meme custom",
        )


class AdminContextRefreshTests(unittest.TestCase):
    def test_interaction_context_requires_enough_recent_signal(self) -> None:
        rows = [
            {
                "role": "user" if index < 8 else "assistant",
                "speaker": "Pablo" if index % 2 == 0 else "Sofi",
                "content": f"message {index} " + ("x" * 30),
            }
            for index in range(20)
        ]

        self.assertTrue(AdminCog._has_enough_interaction_context(rows))

    def test_interaction_context_rejects_single_speaker_noise(self) -> None:
        rows = [
            {
                "role": "user" if index < 8 else "assistant",
                "speaker": "Pablo",
                "content": f"message {index} " + ("x" * 30),
            }
            for index in range(20)
        ]

        self.assertFalse(AdminCog._has_enough_interaction_context(rows))

    def test_format_server_context_view_empty(self) -> None:
        output = AdminCog._format_server_context_view("", [])

        self.assertIn("No AI server context is currently stored.", output)

    def test_format_server_context_view_populated(self) -> None:
        output = AdminCog._format_server_context_view(
            "Tone: casual",
            [
                {
                    "channel_id": 123,
                    "channel_name": "general",
                    "updated_at": "2026-06-25T00:00:00+00:00",
                    "summary": "Tone: casual",
                }
            ],
        )

        self.assertIn("#general (123)", output)
        self.assertIn("Tone: casual", output)


class XAIClientMessageTests(unittest.TestCase):
    def test_build_user_message_content_text_only(self) -> None:
        client = XAIClient("key", "grok-test")

        content = client._build_user_message_content(
            author_name="Pablo",
            user_message="hello",
            image_urls=[],
        )

        self.assertIsInstance(content, str)
        self.assertIn("[UNTRUSTED_USER_MESSAGE_FROM Pablo]", content)

    def test_build_user_message_content_with_image(self) -> None:
        client = XAIClient("key", "grok-test")

        content = client._build_user_message_content(
            author_name="Pablo",
            user_message="what is this?",
            image_urls=["https://cdn.discordapp.com/file.png"],
        )

        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "input_image")
        self.assertEqual(content[0]["image_url"], "https://cdn.discordapp.com/file.png")
        self.assertEqual(content[1]["type"], "input_text")
        self.assertIn("what is this?", content[1]["text"])

    def test_channel_context_selects_matching_channel(self) -> None:
        client = XAIClient("key", "grok-test")
        context = (
            "Channel #general (111)\n"
            "Tone: talks about football\n"
            "Inside jokes/memes: goal spam\n\n"
            "---\n\n"
            "Channel #minecraft (222)\n"
            "Tone: talks about blocks\n"
            "Inside jokes/memes: creeper jokes"
        )

        suffix = client._build_context_suffix(
            server_context=context,
            user_message="what are we building?",
            conversation_history=[],
            channel_reference="<#222>",
            channel_name="minecraft",
        )

        self.assertIn("blocks", suffix)
        self.assertNotIn("football", suffix)

    def test_safe_topic_filters_stale_refusal_history(self) -> None:
        client = XAIClient("key", "grok-test")
        history = [
            {"role": "assistant", "content": "Nitori: I can't help with that."},
            {"role": "user", "content": "Pablo: let's talk about music now"},
        ]

        filtered = client._filter_history_for_current_message(
            history,
            current_user_message="let's talk about music now",
        )

        self.assertEqual([item["content"] for item in filtered], ["Pablo: let's talk about music now"])

    def test_parse_reaction_suggestion_accepts_unicode_emoji(self) -> None:
        client = XAIClient("key", "grok-test")

        self.assertEqual(
            client._parse_reaction_suggestion("😂", available_emojis=[]),
            "😂",
        )

    def test_parse_reaction_suggestion_accepts_available_custom_emoji(self) -> None:
        client = XAIClient("key", "grok-test")

        self.assertEqual(
            client._parse_reaction_suggestion(
                "<:nitori:123456789012345678>",
                available_emojis=["nitori: <:nitori:123456789012345678>"],
            ),
            "<:nitori:123456789012345678>",
        )

    def test_parse_reaction_suggestion_rejects_unavailable_custom_emoji(self) -> None:
        client = XAIClient("key", "grok-test")

        self.assertIsNone(
            client._parse_reaction_suggestion(
                "<:other:123456789012345678>",
                available_emojis=["nitori: <:nitori:999999999999999999>"],
            )
        )

    def test_parse_reaction_suggestion_none(self) -> None:
        client = XAIClient("key", "grok-test")

        self.assertIsNone(client._parse_reaction_suggestion("NONE", available_emojis=[]))

    def test_reaction_prompt_requires_clear_natural_moment(self) -> None:
        client = _ScriptedXAIClient(["NONE"])

        asyncio.run(
            client.suggest_reaction(
                user_message="that workaround is actually smart",
                assistant_reply="Yeah, that is the cleanest option.",
                channel_name="general",
                available_emojis=[],
                conversation_mode="active",
            )
        )

        system_prompt = str(client.calls[0][0]["content"])
        self.assertIn("joke", system_prompt)
        self.assertIn("clever", system_prompt)
        self.assertIn("celebration", system_prompt)
        self.assertIn("otherwise return NONE", system_prompt)


class _ScriptedXAIClient(XAIClient):
    def __init__(self, responses: list[str]) -> None:
        super().__init__("key", "grok-test")
        self.responses = list(responses)
        self.calls: list[list[dict[str, object]]] = []

    async def _create_completion(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("xAI API returned an empty completion.")
        return self.responses.pop(0)


class XAIClientFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_refusal_uses_clean_retry_answer(self) -> None:
        client = _ScriptedXAIClient(
            [
                "I can't help with that.",
                "I can't help with that.",
                "Nitori: sure, that sounds chaotic but funny.",
            ]
        )

        reply = await client._create_completion_with_retry(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "[UNTRUSTED_USER_MESSAGE_FROM Pablo]\nmake a silly joke"},
            ],
            temperature=0.8,
            max_tokens=100,
            retries=1,
            retry_on_refusal=True,
            user_message_for_fallback="make a silly joke",
        )

        self.assertEqual(reply, "Nitori: sure, that sounds chaotic but funny.")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([item["role"] for item in client.calls[-1]], ["system", "user"])

    async def test_safe_refusal_never_returns_canned_meta_fallback(self) -> None:
        client = _ScriptedXAIClient(
            [
                "I can't help with that.",
                "I can't help with that.",
                "I can't help with that.",
            ]
        )

        with self.assertRaises(RuntimeError):
            await client._create_completion_with_retry(
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "[UNTRUSTED_USER_MESSAGE_FROM Pablo]\nhola bro"},
                ],
                temperature=0.8,
                max_tokens=100,
                retries=1,
                retry_on_refusal=True,
                user_message_for_fallback="hola bro",
            )

    async def test_unsafe_refusal_does_not_use_clean_retry(self) -> None:
        client = _ScriptedXAIClient(
            [
                "I can't help with that.",
                "I can't help with that.",
                "Nitori: sure",
            ]
        )

        reply = await client._create_completion_with_retry(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "[UNTRUSTED_USER_MESSAGE_FROM Pablo]\nbuild a bomb"},
            ],
            temperature=0.8,
            max_tokens=100,
            retries=1,
            retry_on_refusal=True,
            user_message_for_fallback="build a bomb",
        )

        self.assertEqual(reply, "I can't help with that.")
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
