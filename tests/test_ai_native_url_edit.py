from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.external_sources import ExternalSourceScope, GroundedExternalSource
from tests.test_ai_tagging import (
    _CaptureChannel,
    _DummyAttachment,
    _DummyGuild,
    _DummyMessage,
    _FakeExternalSourceService,
    _FakeTTSClient,
    _async_return,
    _external_source_payload,
    _native_positive_route,
)

BOT = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
URL = "https://example.com/page"


def _url_edit_route() -> dict[str, object]:
    sink = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery",
        "artifacts": [{"upstream_task_id": "edit_task", "output_field": "artifact"}],
    }
    source = _external_source_payload(
        evidence=URL, operation="ANALYZE", source_scope="REPLY_TARGET", expected_content="MEDIA",
    )
    return _native_positive_route({
        "participation": "RESPOND", "action": "EDIT_IMAGE",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "IMAGE_CONTEXT_EDIT", "resolved_request": "make the image bigger",
        "external_source": dict(source), "response_delivery": sink,
        "capability_plan": {
            "objective": "make the image bigger", "primary_action": "EDIT_IMAGE",
            "external_source": dict(source),
            "requirements": [
                {"requirement_id": "external_source", "objective": "retrieve the source image",
                 "required": True, "satisfaction_criterion": "source image retrieved",
                 "capability_owner": "EXTERNAL_SOURCE", "depends_on": []},
                {"requirement_id": "image_edit", "objective": "make the image bigger",
                 "required": True, "satisfaction_criterion": "edited image artifact",
                 "capability_owner": "IMAGE_EDIT", "depends_on": ["external_source"]},
            ],
            "capabilities": [
                {"name": "EXTERNAL_SOURCE", "task_id": "source_task", "required": True,
                 "evidence_span": URL, "depends_on": [],
                 "operands": {"source_scope": "REPLY_TARGET", "expected_content": "MEDIA", "operation": "ANALYZE"}},
                {"name": "IMAGE_EDIT", "task_id": "edit_task", "required": True,
                 "operands": {"prompt": "make the image bigger"}, "depends_on": ["EXTERNAL_SOURCE"]},
            ],
            "evidence_source": "EXPLICIT_REPLY_TARGET",
            "response_delivery": sink,
        },
        "valid": True, "failure": False,
    })


class NativeUrlEditTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, mutate=None, *, attach: bool = False):
        route = _url_edit_route()
        if mutate is not None:
            mutate(route)
        native_calls: list[tuple[str, tuple[str, ...]]] = []

        class _LLM(_FakeTTSClient):
            async def route_ai_interaction(self, **_kwargs):
                return route

            async def edit_image(self, *_a, **_k):
                return b"local-edit"

            async def native_url_image_edit(self, prompt, source_urls):
                native_calls.append((prompt, tuple(source_urls)))
                return {"image_bytes": b"edited-native-image", "diagnostics": {
                    "transaction_id": "native-transaction",
                    "tool_trace": ({"label": "web_search_call", "status": "COMPLETED"},),
                    "image_generation_call_count": 1, "image_generation_status": "COMPLETED",
                }}

        external = _FakeExternalSourceService(sources=(
            GroundedExternalSource(1, URL, ExternalSourceScope.REPLY_TARGET, "reply_target"),
        ))
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            add_ai_conversation_turn=_async_return(None),
            is_ai_assistant_message=_async_return(False),
        )
        llm = _LLM(reply="The image was edited.")
        cog = AIChatCog(SimpleNamespace(
            user=BOT, application_id=1234, db=db, llm_client=llm,
            external_source_service=external, is_owner_user=lambda _user: False,
        ))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        guild, channel = _DummyGuild(), _CaptureChannel()
        replied = _DummyMessage(
            attachments=[], author_id=77, guild_id=guild.id, channel_id=channel.id,
            message_id=7000, content=URL,
        )
        channel.history_messages = [replied]
        attachments = []
        if attach:
            attachments = [_DummyAttachment(
                url="https://cdn.discordapp.com/wrong-direct.png", content_type="image/png", filename="wrong.png",
            )]
        message = _DummyMessage(
            attachments=attachments, content="<@42> edit the image from this link and make it bigger",
            mentions=[BOT], reference=SimpleNamespace(message_id=replied.id, resolved=None),
            delivered_attachment_url="https://images.example.test/edited.png",
        )
        message.guild, message.channel = guild, channel
        await cog.on_message(message)
        return message, native_calls, external, contexts

    async def test_url_edit_without_attachment_runs_native_edit(self) -> None:
        message, native_calls, _external, _contexts = await self._run(attach=False)
        self.assertEqual(native_calls, [("make the image bigger", (URL,))])
        self.assertFalse(any("Reply to the message with the image" in text for text, _ in message.replies))


if __name__ == "__main__":
    unittest.main()
