from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import validate_native_capability_contract
from services.ai_capability_orchestrator import (
    CapabilityExecutionContext,
    build_capability_execution_plan,
    normalize_terminal_result,
)
from services.external_sources import ExternalContentCompleteness, ExternalExpectedContent, ExternalSourceBundle, ExternalSourceIntent, ExternalSourceOperation, ExternalSourceResult, ExternalSourceScope, ExternalSourceStatus, GroundedExternalSource
from services.web_research import WebResearchResult, WebSource
from services.web_research_context import format_web_research_context
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _FakeTTSClient, _async_return, _explicit_test_plan


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
    def test_analytical_standings_keep_every_team_and_do_not_prove_fulfillment(self) -> None:
        import json
        from dataclasses import replace
        from services.football_operation_service import FootballRetrievalResult
        from services.ai_capability_orchestrator import CapabilityRequirement

        plan = _explicit_test_plan("FOOTBALL", "table", "FOOTBALL_TEAM_QUERY", scope="STRUCTURED_DATA", operands={"operation_family": "standings"})
        plan = replace(plan, requirements=(CapabilityRequirement(
            "qualification", "Analyze qualification", satisfaction_criterion="Prove supported scenarios",
            capability_owner="FOOTBALL", owner_task_id="table",
        ),))
        context = CapabilityExecutionContext(build_capability_execution_plan(plan, fallback_action=plan.primary_action))
        rows = [{"rank": n, "team": {"id": n, "name": f"Team {n}"}, "points": 30 - n,
                 "all": {"played": 14}, "goalsDiff": 0, "group": "Opening", "description": "Qualification" if n <= 8 else None}
                for n in range(1, 19)]
        result = FootballRetrievalResult(standings_table=rows, standing_row=rows[14],
            requested_scope={"request_plan": {"output_payload": ["standing_row", "standings_table"]}})
        context.record(normalize_terminal_result("FOOTBALL", "SELECTED", provider="offline", task_id="table", payload=result))
        self.assertTrue(context.results["table"].satisfies_request)
        self.assertFalse(context.bundle.satisfied)
        prompt = AIChatCog._capability_bundle_prompt_context(context)
        payload = json.loads(prompt.split("Trusted football result: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(len(payload["standings_table"]), 18)
        self.assertEqual(payload["standings_table"][17]["team_id"], 18)
        self.assertEqual(payload["standings_table"][0]["description"], "Qualification")
        self.assertNotIn("standings", payload)

        fixture_result = FootballRetrievalResult(fixtures=[{"fixture": {"id": n}, "detail": "x" * 6000} for n in range(3)],
            requested_scope={"operation_type": "fixture_result", "request_plan": {"output_payload": ["fixtures"]}})
        fixture_context = CapabilityExecutionContext(context.execution_plan)
        fixture_context.record(normalize_terminal_result("FOOTBALL", "SELECTED", provider="offline", task_id="table", payload=fixture_result))
        AIChatCog._capability_bundle_prompt_context(fixture_context)
        self.assertEqual(len(fixture_result.fixtures), 3)

        route = _web_budget_route(("table",))
        route["action"] = route["capability_plan"]["primary_action"] = "FOOTBALL_LOOKUP"
        route["capability_plan"]["requirements"][0].update(
            capability_owner="FOOTBALL", objective="Analyze qualification", satisfaction_criterion="Incorporate the corrected cutoff")
        route["capability_plan"]["capabilities"][0].update(
            name="FOOTBALL", scope="STRUCTURED_DATA", operands={"operation_family": "standings"}, requested_outputs=["result"])
        llm = _EvidenceLLM(route)
        captured_reviews = []

        async def review(**kwargs):
            llm.chat_calls.append(kwargs)
            captured_reviews.append({**kwargs["response_request"], "user_message": kwargs["user_message"],
                                    "evidence": kwargs["trusted_capability_context"]})
            if len(captured_reviews) == 2:
                return {"decision": "read_more", "answer": "", "stop": None, "reads": [{"requirement_ids": ["requirement_table"], "task": {
                    "name": "WEB", "task_id": "rules", "required": False, "evidence_span": "Verify applicable rules",
                    "depends_on": [], "scope": "BROAD_RESEARCH", "independent": True, "independent_research": True,
                    "owner_action": None, "operands": {"query": "applicable official qualification rules"}, "input_bindings": [],
                    "source_refs": [], "requested_outputs": ["result"],
                }}], "assessments": [{"requirement_id": "requirement_table", "status": "partial", "basis": "retrieved",
                    "evidence_task_ids": ["table"], "findings": ["The table alone does not prove qualification."],
                    "missing_evidence": ["Verified rules and remaining fixtures."]}]}
            return {"decision": "limited", "answer": "The table alone does not prove qualification.", "reads": [],
                "stop": {"reason": "analysis_limit", "task_ids": ["table"]} if len(captured_reviews) == 1 else {"reason": "source_blocked", "task_ids": ["rules"]}, "assessments": [{
                "requirement_id": "requirement_table", "status": "partial", "basis": "retrieved", "evidence_task_ids": ["table"],
                "findings": ["The table alone does not prove the required results."],
                "missing_evidence": ["Remaining fixtures and verified regulations."],
            }]}

        async def retrieve(*_args, **kwargs):
            self.assertFalse(kwargs["allow_web_fallback"])
            return normalize_terminal_result("FOOTBALL", "SELECTED", provider="offline", payload=result)

        llm.respond_with_evidence = review
        cog, message = self._make_cog(llm, web=SimpleNamespace())
        cog._execute_football_action = retrieve
        message.content = "<@42> What does this team need? Correction: the cutoff is eight."
        asyncio.run(cog.on_message(message))
        self.assertEqual(len(captured_reviews), 3)
        self.assertEqual(len(llm.chat_calls), 3)
        self.assertIn("analysis_limit_requires_investigation", captured_reviews[1]["correction"]["error"])
        self.assertEqual(captured_reviews[2]["remaining_reads"], 0)
        self.assertIn("the cutoff is eight", captured_reviews[0]["user_message"])
        self.assertIn("Incorporate the corrected cutoff", captured_reviews[0]["evidence"])
        final = llm.chat_calls[0]["trusted_capability_context"]
        self.assertIn("Request satisfied: False", final)
        self.assertEqual(message.replies[0][0], "The table alone does not prove qualification.")
        self.assertIn('"team_id":18', final)

        bad_plan = replace(plan, capabilities=(replace(plan.capabilities[0], operands={
            "operation_family": "standings", "team_candidates": ["Team Alpha", "Team Beta"],
        }),))
        cog.bot.api_football_client = SimpleNamespace()
        invalid = asyncio.run(AIChatCog._execute_football_action(cog, message,
            SimpleNamespace(capability_plan=bad_plan.to_payload()), settings=SimpleNamespace(), lang="en",
            user_prompt="Compare these teams", anchor_type="MENTION", reply_to_trigger=True, result_only=True))
        self.assertEqual(invalid.status.value, "VALIDATION_FAILED")
        self.assertIn("maximum=1", invalid.reason)
        self.assertIn("Team Alpha", invalid.reason)
        self.assertIn("Team Beta", invalid.reason)
        self.assertEqual(invalid.payload.requested_scope["supplied_team_candidates"], ["Team Alpha", "Team Beta"])
        bad_context = CapabilityExecutionContext(build_capability_execution_plan(bad_plan, fallback_action="FOOTBALL_LOOKUP"))
        bad_context.record(replace(invalid, task_id="table"))
        self.assertIn("invalid_team_arity", cog._capability_bundle_prompt_context(bad_context))

        from services.ai_capability_orchestrator import CapabilityRequest
        tasks = tuple(CapabilityRequest("WEB", task_id=f"{n}" + "x" * 75, required=n < 8,
            operands={"query": str(n)}) for n in range(14))
        requirements = tuple(CapabilityRequirement(f"r{n}", "o" * 240,
            satisfaction_criterion=f"criterion{n} " + "c" * 220, capability_owner="WEB", owner_task_id=tasks[n].task_id)
            for n in range(8))
        large = replace(plan, objective="o" * 300, capabilities=tasks[:8], requirements=requirements)
        bounded = CapabilityExecutionContext(replace(build_capability_execution_plan(large, fallback_action="WEB_LOOKUP"), ordered_capabilities=tasks))
        for task in tasks:
            bounded.record(normalize_terminal_result("WEB", "SUCCESS", task_id=task.task_id, provider="p" * 60,
                payload={"text": "evidence"}, scope={"s": "s" * 500}, provenance={"s": "s" * 500}))
        bounded_prompt = AIChatCog._capability_bundle_prompt_context(bounded)
        self.assertLessEqual(len(bounded_prompt), 11000)
        for requirement in requirements:
            self.assertIn(requirement.satisfaction_criterion, bounded_prompt)

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

    def test_native_shared_synthesis_preserves_every_web_task_and_closing_rules(self) -> None:
        task_ids = ("web_alpha", "web_bravo", "web_charlie", "web_delta", "web_failure")
        successful_task_ids = set(task_ids) - {"web_failure"}

        for order in (task_ids, tuple(reversed(task_ids))):
            with self.subTest(order=order):
                class _Web:
                    def __init__(self) -> None:
                        self.queries: list[str] = []
                        self.results: dict[str, WebResearchResult] = {}

                    async def research(self, request, **_kwargs):
                        self.queries.append(request.query)
                        task_id = request.query.removeprefix("evidence ")
                        if task_id == "web_failure":
                            return WebResearchResult(
                                query=request.query,
                                answer="",
                                failure_reason="synthetic_provider_failure",
                                tool_used="synthetic_web",
                            )
                        self.results[task_id] = self_result[task_id]
                        return self.results[task_id]

                self_result = {
                    task_id: self._large_web_result(task_id)
                    for task_id in task_ids
                    if task_id in successful_task_ids
                }
                web = _Web()
                llm = _EvidenceLLM(_web_budget_route(order))
                cog, message = self._make_cog(llm, web=web)

                with self.assertLogs("root", level="INFO") as captured:
                    asyncio.run(cog.on_message(message))

                self.assertEqual(
                    sorted(web.queries),
                    sorted(f"evidence {task_id}" for task_id in task_ids),
                )
                self.assertEqual(len(llm.chat_calls), 1)
                log_messages = [record.getMessage() for record in captured.records]
                synthesis_index = next(
                    index for index, text in enumerate(log_messages)
                    if "AI capability synthesis source_synthesis_started=true" in text
                )
                terminal_index, terminal = next(
                    (index, text)
                    for index, text in enumerate(log_messages)
                    if index < synthesis_index
                    and "AI capability terminal " in text
                    and "results=" in text
                    and text.count("'capability': 'WEB'") == len(task_ids)
                )
                self.assertLess(terminal_index, synthesis_index)
                self.assertEqual(terminal.count("'capability': 'WEB'"), len(task_ids))
                self.assertIn("'status': 'PROVIDER_FAILURE'", terminal)
                trusted = str(llm.chat_calls[0]["trusted_capability_context"])
                self.assertLessEqual(len(trusted), 11000, trusted)
                for task_id in task_ids:
                    self.assertIn(f"task_id={task_id}", trusted)
                    expected_status = "PROVIDER_FAILURE" if task_id == "web_failure" else "SUCCESS"
                    self.assertIn(f"task_id={task_id} | status={expected_status}", trusted)
                self.assertIn("reason=synthetic_provider_failure", trusted)
                for task_id in successful_task_ids:
                    self.assertIn(f"{task_id} grounded finding", trusted)
                    for source_index in range(1, 4):
                        self.assertIn(f"Grounded source {task_id} {source_index}", trusted)
                        self.assertIn(f"evidence.example | https://evidence.example/{task_id}/{source_index}", trusted)
                        self.assertIn(f"{task_id} grounded finding {source_index}", trusted)
                    self.assertEqual(
                        web.results[task_id],
                        self_result[task_id],
                        "synthesis serialization must not mutate the typed WEB result",
                    )
                self.assertEqual(trusted.count("[/TRUSTED_WEB_RESULTS]"), len(successful_task_ids))
                self.assertIn("[/TRUSTED_CAPABILITY_RESULTS]", trusted)
                self.assertIn("Preserve partial, failed, and missing capability states", trusted)
                self.assertIn("[TRUSTED_CAPABILITY_RESULTS]", trusted)
                self.assertIn("omitted", trusted.casefold())
                self.assertEqual(len(message.channel.sent) + len(message.replies), 1)


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


    def test_image_artifact_result_tells_the_reply_the_file_is_attached(self) -> None:
        for capability, action, filename in (
            ("IMAGE_GENERATE", "GENERATE_IMAGE", "nitori_generated.png"),
            ("MEME", "CHAT", "nitori_meme.png"),
        ):
            with self.subTest(capability=capability):
                plan = _explicit_test_plan(capability, "art_1", action, scope="IMAGE_GENERATION")
                context = CapabilityExecutionContext(build_capability_execution_plan(plan, fallback_action=action))
                context.record(normalize_terminal_result(
                    capability, "SUCCESS", provider="synthetic", task_id="art_1",
                    payload={"artifact": b"png-bytes", "filename": filename, "content_type": "image/png", "artifact_hash": "h"},
                ))

                serialized = AIChatCog._capability_bundle_prompt_context(context)

                self.assertIn("Image artifact: present=True", serialized)
                self.assertIn("attached to this reply", serialized)


if __name__ == "__main__":
    unittest.main()
