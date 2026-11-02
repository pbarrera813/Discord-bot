from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import validate_native_capability_contract
from services.external_sources import ExternalContentCompleteness, ExternalExpectedContent, ExternalSourceBundle, ExternalSourceIntent, ExternalSourceOperation, ExternalSourceResult, ExternalSourceScope, ExternalSourceStatus, GroundedExternalSource
from services.web_research import WebResearchResult, WebSource
from services.web_research_context import format_web_research_context
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _FakeTTSClient, _async_return


def _web_budget_route(task_order: tuple[str, ...]) -> dict[str, object]:
    requirements = []
    capabilities = []
    for task_id in task_order:
        requirements.append({
            "requirement_id": f"requirement_{task_id}",
            "objective": f"Research the evidence for {task_id}.",
            "required": True,
            "satisfaction_criterion": f"A grounded result for {task_id}.",
            "capability_owner": "WEB",
            "owner_task_id": task_id,
            "depends_on": [],
        })
        capabilities.append({
            "name": "WEB",
            "task_id": task_id,
            "required": True,
            "owner_action": None,
            "scope": "BROAD_RESEARCH",
            "evidence_span": f"research the evidence for {task_id}",
            "depends_on": [],
            "independent": True,
            "independent_research": True,
            "operands": {"query": f"evidence {task_id}"},
            "input_bindings": [],
            "source_refs": [],
            "requested_outputs": ["answer"],
        })
    route = {
        "participation": "RESPOND",
        "action": "WEB_LOOKUP",
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST",
        "resolved_request": "Research the requested evidence.",
        "capability_plan": {
            "objective": "Research the requested evidence.",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "WEB_LOOKUP",
            "requirements": requirements,
            "capabilities": capabilities,
        },
        "valid": True,
        "failure": False,
    }
    reason = validate_native_capability_contract(route)
    if reason is not None:
        raise AssertionError(f"invalid repeated-WEB native fixture: {reason}")
    return route


class _EvidenceLLM(_FakeTTSClient):
    def __init__(self, route: dict[str, object]) -> None:
        super().__init__(reply="The combined evidence is available.")
        self.route = route

    async def route_ai_interaction(self, **_kwargs):
        return self.route


class EvidenceBudgetNativeSynthesisTests(unittest.TestCase):

    def _make_cog(self, llm: _EvidenceLLM, *, web: object, football: object | None = None):
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(
                SimpleNamespace(prefix="!", language_code="en", server_context="")
            ),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user,
            application_id=1234,
            db=db,
            llm_client=llm,
            web_research_service=web,
            api_football_client=football,
            is_owner_user=lambda _user: False,
        ))
        message = _DummyMessage(
            attachments=[],
            content="<@42> research the requested evidence",
            mentions=[bot_user],
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        return cog, message

    @staticmethod
    def _large_web_result(task_id: str) -> WebResearchResult:
        source_rows = tuple(
            WebSource(
                title=f"Grounded source {task_id} {index}",
                url=f"https://evidence.example/{task_id}/{index}",
                domain="evidence.example",
                snippet=f"{task_id} grounded finding {index}: " + (f"supported detail {index} " * 24),
            )
            for index in range(1, 4)
        )
        answer = f"{task_id} grounded summary: " + (f"supported detail for {task_id}. " * 48)
        return WebResearchResult(
            query=f"evidence {task_id}",
            answer=answer,
            sources=source_rows,
            citations=tuple(source.url for source in source_rows),
            tool_used="synthetic_web",
        )


    @staticmethod
    def _external_bundle(*, text: str = "") -> ExternalSourceBundle:
        intent = ExternalSourceIntent(
            required=True,
            operation=ExternalSourceOperation.SUMMARIZE,
            source_scope=ExternalSourceScope.CURRENT_MESSAGE,
            explicit=True,
            expected_content=ExternalExpectedContent.TEXT,
        )
        source = GroundedExternalSource(
            index=1,
            original_url="https://source.example/article",
            source_scope=ExternalSourceScope.CURRENT_MESSAGE,
            provenance="current_message",
        )
        result = ExternalSourceResult(
            source=source,
            status=ExternalSourceStatus.SUCCESS,
            text=text,
            completeness=ExternalContentCompleteness.TEXT_COMPLETE,
            text_complete=True,
        )
        return ExternalSourceBundle(intent, (source,), (result,), ())


    def test_web_attribution_requires_a_typed_source_url(self) -> None:
        prose_url = "https://prose.example/mentioned-only-in-summary"
        result = WebResearchResult(
            query="verify the source",
            answer=f"The summary mentions {prose_url} but provides no fitting citation.",
            sources=(WebSource(
                title="Long typed source",
                url="https://typed.example/" + ("path/" * 90),
                domain="typed.example",
                snippet="Provider source excerpt",
            ),),
            tool_used="synthetic_web",
        )

        serialized = format_web_research_context(result, max_chars=300)

        self.assertLessEqual(len(serialized), 300)
        self.assertTrue(serialized.startswith("[TRUSTED_WEB_RESULTS]"))
        self.assertTrue(serialized.endswith("[/TRUSTED_WEB_RESULTS]"))
        self.assertIn("Evidence omitted.", serialized)
        self.assertNotIn(prose_url, serialized)
        self.assertNotIn("summary mentions", serialized)
        complete = WebResearchResult(query="official conditions", answer="Detailed condition. " * 120 + "Critical exception at the end.",
            sources=(WebSource(title="Official", url="https://official.example/conditions", domain="official.example"),))
        full = format_web_research_context(complete, max_chars=5000)
        self.assertIn(complete.answer, full)
        bounded = format_web_research_context(complete, max_chars=500)
        self.assertLessEqual(len(bounded), 500)
        self.assertIn("omitted", bounded)
        self.assertIn("https://official.example/conditions", bounded)



if __name__ == "__main__":
    unittest.main()
