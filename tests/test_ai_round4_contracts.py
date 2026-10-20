from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from tests.test_ai_tagging import (
    _async_return,
    _CaptureChannel,
    _CaptureChatXAIClient,
    _DummyGuild,
    _DummyMessage,
)


class RouterKnowsWhichChannelTheMessageIsInTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_cog_gives_the_router_the_channel_of_the_message(self) -> None:
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            route_kwargs: dict[str, object] = {}

            async def route_ai_interaction(self, **kwargs):
                self.route_kwargs = kwargs
                return {"participation": "IGNORE", "action": "IGNORE", "participation_confidence": 1.0, "action_confidence": 1.0, "reason_code": "UNRELATED_HUMAN_CHAT", "valid": True, "failure": False}

        llm = _LLM()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(user=bot_user, application_id=1234, db=db, llm_client=llm, is_owner_user=lambda _user: False))
        message = _DummyMessage(attachments=[], content="<@42> quita el slowmode del canal", mentions=[bot_user])
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        await cog.on_message(message)

        self.assertEqual(llm.route_kwargs["channel_metadata"], {"id": 10, "name": "general"})


class ChatKnowsTheAuthorsPermissionsTests(unittest.IsolatedAsyncioTestCase):
    def test_without_permission_facts_the_prompt_has_no_permission_section(self) -> None:
        client = _CaptureChatXAIClient()
        asyncio.run(client.chat(server_context="", user_message="hola", author_name="u", channel_name="general"))
        self.assertNotIn("Trusted facts about the person writing", str(client.messages[0]["content"]))

    async def test_the_cog_hands_the_author_permissions_to_the_chat_call(self) -> None:
        from tests.test_ai_artifact_delivery import _artifact_lifecycle_cog
        from tests.test_ai_meme_adapter import _meme_route, _validated

        cog, message, llm = _artifact_lifecycle_cog({})
        route = _meme_route({"meme_action": "random", "meme_top": "x"})
        plan = route["capability_plan"]
        plan["capabilities"] = [{
            "task_id": "chat_1", "name": "CHAT", "required": True, "evidence_span": "hola", "depends_on": [],
            "scope": "GENERAL_REASONING", "independent": True, "independent_research": False, "owner_action": None,
            "operands": {}, "input_bindings": [], "source_refs": [], "requested_outputs": ["answer"],
        }]
        plan["requirements"] = [{**plan["requirements"][0], "capability_owner": "CHAT", "owner_task_id": "chat_1"}]
        route["response_delivery"]["artifacts"] = plan["response_delivery"]["artifacts"] = []
        calls: list[dict[str, object]] = []

        async def chat(**kwargs):
            calls.append(kwargs)
            return "respuesta"

        async def route_ai_interaction(**_kwargs):
            return _validated(route)

        llm.chat, llm.route_ai_interaction = chat, route_ai_interaction
        await cog.on_message(message)
        self.assertTrue(calls, "the chat was never called")
        self.assertIn("administrator=no", str(calls[0].get("author_permissions")))

    async def test_the_shared_synthesis_also_hands_the_author_permissions_over(self) -> None:
        from tests.test_ai_artifact_delivery import _artifact_lifecycle_cog
        from tests.test_ai_meme_adapter import _FakeMemegen, _meme_route, _validated

        cog, message, llm = _artifact_lifecycle_cog({})
        cog.bot.memegen_client = _FakeMemegen()
        calls: list[dict[str, object]] = []

        async def chat(**kwargs):
            calls.append(kwargs)
            return "listo"

        async def route_ai_interaction(**_kwargs):
            return _validated(_meme_route({"meme_action": "random", "meme_top": "x"}))

        llm.chat, llm.route_ai_interaction = chat, route_ai_interaction
        await cog.on_message(message)
        self.assertIn("administrator=no", str(calls[-1].get("author_permissions")))


if __name__ == "__main__":
    unittest.main()
