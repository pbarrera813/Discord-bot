from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from tests.test_ai_tagging import (
    _CaptureChannel,
    _DummyGuild,
    _DummyMessage,
    _async_return,
    _native_positive_route,
)

BOT = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")


def _route(action: str, capability: str, task_id: str, operands: dict[str, object] | None, delivery: dict | None):
    plan = {
        "objective": "do it",
        "requirements": [{
            "requirement_id": "req", "objective": "do it", "required": True,
            "satisfaction_criterion": "done", "capability_owner": capability, "depends_on": [],
        }],
        "capabilities": [{
            "name": capability, "task_id": task_id, "required": True,
            **({"operands": operands} if operands is not None else {}),
        }],
    }
    route = {
        "participation": "RESPOND", "action": action,
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "do it",
        "capability_plan": plan, "valid": True, "failure": False,
    }
    if delivery is not None:
        route["response_delivery"] = delivery
        plan["response_delivery"] = delivery
    return _native_positive_route(route)


class MissingSourceTaskTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, route: dict[str, object]):
        class _LLM:
            async def route_ai_interaction(self, **_kwargs):
                return route

            async def chat(self, **_kwargs):
                raise AssertionError("no synthesis for a missing source")

            async def edit_image(self, *_a, **_k):
                raise AssertionError("no provider call without a source image")

            async def analyze_images(self, *_a, **_k):
                raise AssertionError("no provider call without an image")

        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            add_ai_conversation_turn=_async_return(None),
            is_ai_assistant_message=_async_return(False),
        )
        cog = AIChatCog(SimpleNamespace(
            user=BOT, application_id=1234, db=db, llm_client=_LLM(), is_owner_user=lambda _user: False,
        ))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message = _DummyMessage(attachments=[], content="<@42> do it", mentions=[BOT])
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        await cog.on_message(message)
        return message, contexts

    async def test_image_edit_without_source_records_its_task_result(self) -> None:
        sink = {
            "modality": "TEXT", "explicit": True, "source": "CURRENT_MESSAGE",
            "evidence_span": "deliver", "semantic_reason": "artifact", "failure_reason": None,
            "ambiguous_reason": None, "artifacts": [{"upstream_task_id": "edit_task", "output_field": "artifact"}],
        }
        route = _route("EDIT_IMAGE", "IMAGE_EDIT", "edit_task", {"prompt": "make it bigger"}, sink)

        message, contexts = await self._run(route)

        self.assertEqual(len(message.replies), 1)
        result = contexts[-1].bundle.by_task["edit_task"]
        self.assertEqual(result.status.value, "VALIDATION_FAILED")
        self.assertEqual(result.reason, "missing_edit_image")


if __name__ == "__main__":
    unittest.main()
