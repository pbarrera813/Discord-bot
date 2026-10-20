from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.xai_client import XAIClient
from tests.test_ai_catalog_output_parity import PRODUCTION_GREETING_ROUTE
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return


class AnsweredMessageUsesTheGuildLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def _chat_kwargs(self, language_code: str) -> dict[str, object]:
        parsed = json.loads(PRODUCTION_GREETING_ROUTE)
        route = XAIClient("test-key", "test-model")._validate_route_decision("", has_supported_image=False, parsed=parsed)
        self.assertTrue(route["valid"], route.get("failure_reason"))
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        calls: list[dict[str, object]] = []

        class _LLM:
            async def route_ai_interaction(self, **_kwargs):
                return route

            async def chat(self, **kwargs):
                calls.append(kwargs)
                return "Respuesta."

        bot_db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code=language_code, server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        bot = SimpleNamespace(user=bot_user, application_id=1234, db=bot_db, llm_client=_LLM(), is_owner_user=lambda _user: False)
        message = _DummyMessage(attachments=[], author_id=99, guild_id=1, channel_id=10, content="<@42> hola", mentions=[bot_user], message_id=8500)
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        await AIChatCog(bot).on_message(message)
        self.assertEqual(len(calls), 1)
        return calls[0]

    async def test_the_guild_language_reaches_the_chat_call(self) -> None:
        self.assertEqual((await self._chat_kwargs("es")).get("reply_language"), "es")
        self.assertEqual((await self._chat_kwargs("en")).get("reply_language"), "en")


if __name__ == "__main__":
    unittest.main()
