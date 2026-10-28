from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityRequest
from services.external_sources import ExternalSourceService
import tests.test_ai_chat_image_chat_gate as chat_gate
import tests.test_ai_tagging as tagging


class ImageSourceRefsTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, message_id: int, url: str) -> object:
        return SimpleNamespace(
            id=message_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2, name="general"),
            author=SimpleNamespace(id=3, display_name="user"),
            attachments=[SimpleNamespace(url=url, content_type="image/png", filename="image.png")],
            embeds=[],
        )


    async def test_on_message_routes_exact_ref_into_existing_vision_executor(self) -> None:
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        route_metadata: dict[str, object] = {}

        class LLM:
            async def route_ai_interaction(self, **kwargs):
                route_metadata.update(kwargs["image_metadata"])
                ref = route_metadata["image_sources"][0]["ref"]
                return tagging._native_positive_route({
                    "participation": "RESPOND", "action": "ANALYZE_IMAGE",
                    "participation_confidence": 1.0, "action_confidence": 1.0,
                    "reason_code": "DIRECT_REQUEST", "resolved_request": "inspect image",
                    "capability_plan": {
                        "objective": "inspect image", "requirements": [{
                            "requirement_id": "image_requirement", "objective": "inspect image",
                            "required": True, "satisfaction_criterion": "visual result",
                            "capability_owner": "IMAGE_ANALYZE", "owner_task_id": "image_task",
                            "depends_on": [],
                        }],
                        "capabilities": [{
                            "name": "IMAGE_ANALYZE", "task_id": "image_task", "required": True,
                            "scope": "MEDIA_ANALYSIS", "independent": True,
                            "independent_research": False, "selection_count": 1,
                            "source_refs": [ref], "operands": {"selection_count": 1},
                        }],
                    }, "valid": True, "failure": False,
                })

            def __init__(self):
                self.chat_calls = []

            async def chat(self, **kwargs):
                self.chat_calls.append(kwargs)
                return "vision observation"

        llm = LLM()
        db = SimpleNamespace(
            get_or_create_guild_settings=tagging._async_return(
                SimpleNamespace(prefix="!", language_code="en", server_context="")
            ),
            is_ai_channel_allowed=tagging._async_return(True),
            get_ai_conversation_history=tagging._async_return([]),
            add_ai_conversation_turn=tagging._async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user, application_id=1234, db=db, llm_client=llm,
            is_owner_user=lambda _user: False,
        ))
        message = tagging._DummyMessage(
            attachments=[tagging._DummyAttachment(
                url="https://cdn.example/current.png", content_type="image/png", filename="current.png"
            )], content="<@42> inspect the image", mentions=[bot_user],
        )
        message.guild = tagging._DummyGuild()
        message.channel = tagging._CaptureChannel()
        await cog.on_message(message)

        self.assertEqual(route_metadata["image_sources"][0]["url"], "https://cdn.example/current.png")
        vision_calls = [call for call in llm.chat_calls if call.get("image_urls")]
        self.assertEqual(vision_calls[0]["image_urls"], ["https://cdn.example/current.png"])
        self.assertEqual(message.replies[0][0], "vision observation")


    async def test_native_direct_image_source_ref_is_request_local_and_provenanced(self) -> None:
        current = self._message(101, "https://cdn.example/current.png")
        replied = self._message(202, "https://cdn.example/reply.png")
        image_context = AIChatCog._build_chat_image_context(current, replied)
        projection = AIChatCog._image_source_projection(image_context)
        self.assertEqual([item["source_kind"] for item in projection], ["current_message", "reply_target"])
        self.assertEqual([item["source_message_id"] for item in projection], [101, 202])

        llm = SimpleNamespace(chat=AsyncMock(return_value="bounded analysis"))
        cog = chat_gate.ImageAnalyzeChatGateTests()._cog(llm)
        cog._external_sources = ExternalSourceService()
        cog.bot.user.name = "Nitori"
        cog.bot.user.id = 42
        plan = chat_gate.ImageAnalyzeChatGateTests()._plan(
            CapabilityRequest(
                "IMAGE_ANALYZE",
                task_id="image",
                scope="MEDIA_ANALYSIS",
                selection_count=2,
                source_refs=(str(projection[0]["ref"]), str(projection[1]["ref"])),
            )
        )
        result = await cog._execute_live_media_composition_result_only(
            current,
            "inspect the selected image",
            plan,
            settings=SimpleNamespace(server_context=""),
            image_context=image_context,
        )

        image_result = result.results["image"]
        self.assertEqual(image_result.status.value, "SUCCESS", repr(image_result.reason))
        self.assertEqual(image_result.payload["media_urls"], ("https://cdn.example/current.png", "https://cdn.example/reply.png"))
        self.assertEqual(tuple(item["source_kind"] for item in image_result.provenance["source_entries"]), ("current_message", "reply_target"))
        self.assertIsNone(image_result.provenance.get("source_message_id"))
        self.assertEqual(llm.chat.await_args.kwargs["image_urls"], ["https://cdn.example/current.png", "https://cdn.example/reply.png"])


    async def test_unknown_ref_and_preview_are_rejected_without_image_provider_call(self) -> None:
        current = self._message(101, "https://cdn.example/current.png")
        current.embeds = [
            SimpleNamespace(
                url="https://source.example/page",
                image=SimpleNamespace(url="https://cdn.example/preview.png", content_type="image/png"),
                thumbnail=SimpleNamespace(url=""),
            )
        ]
        context = AIChatCog._build_chat_image_context(current, None)
        metadata = AIChatCog._route_image_metadata(context)
        self.assertEqual(len(metadata["image_sources"]), 1)
        self.assertNotIn("preview", str(metadata["image_sources"]))

        llm = SimpleNamespace(chat=AsyncMock(return_value="must not run"))
        cog = chat_gate.ImageAnalyzeChatGateTests()._cog(llm)
        cog._external_sources = ExternalSourceService()
        cog.bot.user.name = "Nitori"
        cog.bot.user.id = 42
        plan = chat_gate.ImageAnalyzeChatGateTests()._plan(
            CapabilityRequest(
                "IMAGE_ANALYZE",
                task_id="image",
                scope="MEDIA_ANALYSIS",
                source_refs=("img:stale:unknown",),
            )
        )
        result = await cog._execute_live_media_composition_result_only(
            current,
            "inspect",
            plan,
            settings=SimpleNamespace(server_context=""),
            image_context=context,
        )
        self.assertEqual(result.results["image"].status.value, "VALIDATION_FAILED")
        self.assertEqual(result.results["image"].reason, "invalid_image_source_ref")
        self.assertEqual(llm.chat.await_count, 0)


if __name__ == "__main__":
    unittest.main()
