from types import SimpleNamespace
import unittest

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import (
    CapabilityExecutionPlan,
    CapabilityRequest,
    CapabilityTerminalResult,
    CapabilityTerminalStatus,
    InputBinding,
)
from services.external_sources import (
    ExternalMediaRelevance,
    ExternalMediaRole,
    ExternalMediaSource,
    ExternalSourceBundle,
    ExternalSourceIntent,
)
class AIBindingMaterializationTests(unittest.IsolatedAsyncioTestCase):

    async def test_external_source_media_binding_uses_authorized_real_payload(self) -> None:
        source = ExternalMediaSource(
            "https://cdn.example/source.png",
            association="GROUNDED_SOURCE_ITEM",
            role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
            relevance=ExternalMediaRelevance.AUTHORITATIVE,
        )
        bundle = ExternalSourceBundle(
            intent=ExternalSourceIntent(required=True),
            sources=(),
            results=(),
            selected_media=(source,),
        )
        cog = AIChatCog.__new__(AIChatCog)
        cog._external_sources = SimpleNamespace()
        cog._admin_actions = SimpleNamespace()
        seen: list[str | None] = []

        async def external(*_args, **_kwargs):
            return CapabilityTerminalResult(
                "EXTERNAL_SOURCE", CapabilityTerminalStatus.SUCCESS, "sources", "SUCCESS",
                payload=bundle, satisfies_request=True,
            )

        async def edit(_prompt, source_url, **_kwargs):
            seen.append(source_url)
            return CapabilityTerminalResult(
                "IMAGE_EDIT", CapabilityTerminalStatus.SUCCESS, "image", "SUCCESS",
                payload={"artifact": b"edited"}, satisfies_request=True,
            )

        cog._execute_external_source_result_only = external
        cog._execute_image_edit_result_only = edit
        source_task = CapabilityRequest("EXTERNAL_SOURCE", task_id="source", operands={})
        edit_task = CapabilityRequest(
            "IMAGE_EDIT",
            task_id="edit",
            depends_on=("source",),
            input_bindings=(InputBinding("source", "media"),),
            operands={"prompt": "edit it"},
        )
        plan = CapabilityExecutionPlan(
            semantic_plan=SimpleNamespace(
                semantic_source="router",
                semantic_status="SUCCESS",
                valid=True,
                capabilities=(source_task, edit_task),
                requirements=(),
            ),
            ordered_capabilities=(source_task, edit_task),
            dispatch_action="CHAT",
        )

        await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "edit the source",
            plan,
            settings=SimpleNamespace(server_context=""),
        )

        self.assertEqual(seen, [source.url])


if __name__ == "__main__":
    unittest.main()
