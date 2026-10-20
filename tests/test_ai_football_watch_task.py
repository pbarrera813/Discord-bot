from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.xai_client import XAIClient
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return

TASK_ID = "watch_1"


class _Football:
    async def resolve_league_id(self, _league_key):
        return 1

    async def get_current_season(self, _league_id):
        return 2026

    async def search_teams(self, **_kwargs):
        return []

    async def get_live_fixtures(self, **_kwargs):
        return [{
            "fixture": {"id": 77, "date": "2026-07-19T20:00:00+00:00", "status": {"short": "1H", "elapsed": 12}},
            "league": {"name": "League A"},
            "teams": {"home": {"name": "Team A"}, "away": {"name": "Team B"}},
            "goals": {"home": 0, "away": 0},
        }]

    async def get_fixtures_on_date(self, **_kwargs):
        return []


def _watch_route(family: str, owner_action: str | None = None, with_chat: bool = False) -> dict[str, object]:
    action = "FOOTBALL_" + family.upper()
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    chat_requirement = [{
        "requirement_id": "answer", "objective": "confirm", "required": True,
        "satisfaction_criterion": "The user is answered.",
        "capability_owner": "CHAT", "owner_task_id": "chat_1", "depends_on": [],
    }] if with_chat else []
    chat_task = [{
        "task_id": "chat_1", "name": "CHAT", "required": True, "evidence_span": "follow",
        "depends_on": [], "scope": "GENERAL_REASONING", "independent": True,
        "independent_research": False, "owner_action": None,
        "operands": {}, "input_bindings": [], "source_refs": [],
        "requested_outputs": ["answer"],
    }] if with_chat else []
    route = {
        "participation": "RESPOND", "action": action,
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "follow the match",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "follow the match", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": action,
            "requirements": [{
                "requirement_id": "watch", "objective": "follow the match", "required": True,
                "satisfaction_criterion": "The watch lifecycle is applied.",
                "capability_owner": "FOOTBALL", "owner_task_id": TASK_ID, "depends_on": [],
            }] + chat_requirement,
            "capabilities": [{
                "task_id": TASK_ID, "name": "FOOTBALL", "required": True,
                "evidence_span": "follow", "depends_on": [],
                "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False,
                "owner_action": owner_action,
                "operands": {"operation_family": family, "fixture_focus": "Team A vs Team B"},
                "input_bindings": [], "source_refs": [], "requested_outputs": ["result"],
            }] + chat_task,
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    return XAIClient("test-key", "test-model")._validate_route_decision(
        "", has_supported_image=False, parsed=route,
    )


class NativeFootballWatchTaskTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, family: str, owner_action: str | None = None, seed_watch: bool = False, with_chat: bool = False):
        route = _watch_route(family, owner_action, with_chat)
        self.assertTrue(route["valid"], route.get("failure_reason"))
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            async def route_ai_interaction(self, **_kwargs):
                return route

            async def chat(self, **_kwargs):
                return "watching"

        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            is_ai_assistant_message=_async_return(False),
            add_ai_conversation_turn=_async_return(None),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user, application_id=1234, db=db, llm_client=_LLM(),
            api_football_client=_Football(), is_owner_user=lambda _user: False,
        ))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message = _DummyMessage(attachments=[], content="<@42> follow the match", mentions=[bot_user])
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        key = (message.guild.id, message.channel.id)
        if seed_watch:
            cog._football_live_watches[key] = cog._create_football_live_watch(
                message,
                fixture={
                    "fixture": {"id": 77, "status": {"short": "1H", "elapsed": 12}},
                    "teams": {"home": {"name": "Team A"}, "away": {"name": "Team B"}},
                    "goals": {"home": 0, "away": 0},
                },
                request="seed",
            )
        try:
            await cog.on_message(message)
            watch = cog._football_live_watches.get(key)
            return watch, message, contexts
        finally:
            cog.cog_unload()

    async def test_standalone_start_creates_one_watch_and_records_task_id(self) -> None:
        watch, message, contexts = await self._run("live_watch_start")

        self.assertEqual(watch.fixture_id, 77)
        self.assertEqual(len(message.replies), 1)
        self.assertEqual(contexts[-1].bundle.by_task[TASK_ID].capability, "FOOTBALL")

    async def test_standalone_stop_cancels_watch_and_records_task_id(self) -> None:
        watch, message, contexts = await self._run("live_watch_stop", seed_watch=True)

        self.assertIsNone(watch)
        self.assertEqual(contexts[-1].bundle.by_task[TASK_ID].status.value, "SUCCESS")
        self.assertEqual(len(message.replies), 1)
        self.assertEqual(contexts[-1].bundle.by_task[TASK_ID].capability, "FOOTBALL")


if __name__ == "__main__":
    unittest.main()
