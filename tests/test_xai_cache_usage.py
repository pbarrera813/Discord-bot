from __future__ import annotations

import asyncio
import json
import unittest

from services.xai_client import XAIClient


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body)

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Session:
    closed = False

    def __init__(self, captured: dict, status: int, body: dict) -> None:
        self._captured, self._status, self._body = captured, status, body

    def post(self, _url: str, json: object = None, headers: object = None) -> _Response:
        self._captured["payload"] = json
        return _Response(self._status, self._body)


def _client(captured: dict, status: int = 200, body: dict | None = None) -> XAIClient:
    client = XAIClient("offline-key", "test-model")

    async def _session() -> _Session:
        return _Session(captured, status, body or {})

    client._get_session = _session
    return client


_COMPLETION = {
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "hola"}]}],
    "usage": {
        "input_tokens": 4731,
        "input_tokens_details": {"cached_tokens": 4096},
        "output_tokens": 318,
        "output_tokens_details": {"reasoning_tokens": 290},
        "cost_in_usd_ticks": "65743500",
    },
}


class CompletionCacheKeyAndUsageTests(unittest.TestCase):
    def test_a_key_is_sent_only_when_given_and_usage_is_logged(self) -> None:
        for cache_key, expected_role in (("nitori-chat", "nitori-chat"), (None, "other")):
            with self.subTest(cache_key=cache_key):
                captured: dict = {}
                client = _client(captured, body=_COMPLETION)
                with self.assertLogs(level="INFO") as logs:
                    result = asyncio.run(client._create_completion(
                        [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=20, cache_key=cache_key,
                    ))
                self.assertEqual(str(result), "hola")
                self.assertEqual(captured["payload"].get("prompt_cache_key"), cache_key)
                self.assertIn(
                    f"AI xAI usage role={expected_role} input=4731 cached=4096 output=318 reasoning=290 cost_usd=0.0066",
                    "\n".join(logs.output),
                )


class RouterRequestShapeTests(unittest.TestCase):
    def test_the_router_carries_a_key_and_puts_the_per_message_parts_last(self) -> None:
        captured: dict = {}
        client = _client(captured, status=400, body={"error": "captured offline"})
        asyncio.run(client.route_ai_interaction(
            bot_name="Nitori", author_name="tester", current_message="hello", anchor_type="NAME_AT_START",
            recent_context=[], available_emojis=["a: <:a:1>"], channel_metadata={"id": 5, "name": "general"},
        ))
        payload = captured["payload"]
        self.assertEqual(payload["prompt_cache_key"], "nitori-router")
        keys = list(json.loads(payload["input"][1]["content"]))
        stable = ("bot", "bot_aliases", "channel", "available_emojis", "request_timezone")
        volatile = ("current_message", "mentions", "reply", "lease", "request_date", "recent_channel_sequence")
        self.assertLess(max(keys.index(key) for key in stable), min(keys.index(key) for key in volatile))
        self.assertEqual(keys[-1], "recent_channel_sequence")


class ReactionRequestShapeTests(unittest.TestCase):
    def test_the_emoji_list_comes_before_the_message_and_the_call_carries_a_key(self) -> None:
        captured: dict = {}
        client = _client(captured, body={**_COMPLETION, "output": [{"type": "message", "content": [{"type": "output_text", "text": "NONE"}]}]})
        asyncio.run(client.suggest_reaction(
            user_message="that is clever", assistant_reply="yes", channel_name="general",
            available_emojis=["nitori: <:nitori:999999999999999999>"],
        ))
        user = captured["payload"]["input"][1]["content"]
        self.assertLess(user.index("Available custom emoji"), user.index("User message"))
        self.assertEqual(captured["payload"]["prompt_cache_key"], "nitori-reaction")


class ChatPromptOrderTests(unittest.TestCase):
    def test_the_per_message_sentence_comes_after_the_stable_server_parts(self) -> None:
        systems = []
        for mode in ("mention", "continuation"):
            captured: dict = {}
            client = _client(captured, body=_COMPLETION)
            asyncio.run(client.chat(
                server_context="Tone: casual", user_message="hi", author_name="tester", channel_name="general",
                available_channels=["#general"], available_emojis=["nitori: <:nitori:1>"],
                author_permissions="administrator=no", conversation_mode=mode,
            ))
            systems.append(captured["payload"]["input"][0]["content"])
        for system in systems:
            stable_end = system.index("Available custom server emojis")
            self.assertLess(system.index("Server context provided"), system.index("Conversation mode:"))
            self.assertLess(stable_end, system.index("Conversation mode:"))
            self.assertLess(system.index("Conversation mode:"), system.index("Trusted facts about the person"))
        common = 0
        while systems[0][common] == systems[1][common]:
            common += 1
        self.assertGreater(common, systems[0].index("Available custom server emojis"))

    def test_server_memories_are_their_own_last_section_not_a_reply_style(self) -> None:
        def system_for(memory: str) -> str:
            captured: dict = {}
            asyncio.run(_client(captured, body=_COMPLETION).chat(
                server_context=f"Tone: casual\n\n{memory}", user_message="hi", author_name="tester", channel_name="general",
                available_emojis=["nitori: <:nitori:1>"], author_permissions="administrator=no",
            ))
            return captured["payload"]["input"][0]["content"]

        with_memory = system_for("[TRUSTED_SERVER_MEMORY]\nAbout Ana (<@10>) joke: eats boogers\n[/TRUSTED_SERVER_MEMORY]")
        without = system_for("")

        self.assertNotIn("How the bot should reply", with_memory)
        self.assertGreater(with_memory.index("About Ana"), with_memory.index("Trusted facts about the person"))
        self.assertTrue(with_memory.startswith(without[:with_memory.index("Server memories relevant")]))
        self.assertEqual(with_memory.count("\n[TRUSTED_SERVER_MEMORY]"), 1)


if __name__ == "__main__":
    unittest.main()
