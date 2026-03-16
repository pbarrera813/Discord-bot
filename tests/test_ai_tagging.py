from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog


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


if __name__ == "__main__":
    unittest.main()
