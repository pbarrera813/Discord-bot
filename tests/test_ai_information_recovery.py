from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import (
    CapabilityExecutionContext, CapabilityRequirement, CapabilityRequest, build_capability_execution_plan,
    execute_ordered_capabilities, normalize_terminal_result,
)
from services.web_research import WebResearchResult, WebSource

from tests.test_ai_tagging import _explicit_test_plan

class RecoveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_investigation_preserves_successful_reads_and_bounds_follow_up_work(self) -> None:
        from services.football_operation_service import FootballRetrievalResult
        from services.football_query_service import compile_football_operation

        def task(task_id, family="FOOTBALL", operands=None):
            return {"name": family, "task_id": task_id, "required": False, "evidence_span": "Retrieve missing evidence",
                    "depends_on": [], "scope": "BROAD_RESEARCH" if family == "WEB" else "STRUCTURED_DATA",
                    "independent": True, "independent_research": family == "WEB", "owner_action": None,
                    "operands": operands or {"operation_family": "fixture_next", "fixture_count": 50, "fixture_round": "Opening - 17"},
                    "input_bindings": [], "source_refs": [], "requested_outputs": ["answer" if family == "WEB" else "result"]}

        def review(decision, reads=(), supported=False):
            stop = None
            if decision == "limited":
                stop = {"reason": "source_blocked" if mode == "unavailable" else "budget_exhausted" if mode == "budget" else "analysis_limit",
                        "task_ids": ["rules"] if mode == "unavailable" else ["table"] if mode != "budget" else []}
            return {"decision": decision, "stop": stop, "answer": "" if decision == "read_more" else "Supported answer with any missing evidence stated.", "assessments": [{"requirement_id": "qualify",
                    "status": "supported" if supported else "partial", "basis": "retrieved", "evidence_task_ids": ["table"],
                    "findings": ["Observed 15 points; calculations must distinguish possible from mandatory outcomes."],
                    "missing_evidence": [] if supported else ["Remaining fixtures and applicable qualification/tiebreak rules."]}],
                    "reads": [{"requirement_ids": ["qualify"], "task": item} for item in reads]}

        for mode in ("success", "unavailable", "budget", "duplicate", "resolved_duplicate", "mutation", "code", "malformed", "unsupported", "cancelled", "cancelled_during_read", "direct", "chat_read", "failed_action", "repair_reference", "repair_duplicate", "repair_combined", "repair_budget"):
            with self.subTest(mode=mode):
                plan = _explicit_test_plan("FOOTBALL", "table", "FOOTBALL_TEAM_QUERY", operands={"operation_family": "standings"})
                if mode in {"direct", "chat_read"}:
                    plan = _explicit_test_plan("CHAT", "table", "CHAT", scope="GENERAL_REASONING")
                plan = replace(plan, requirements=(CapabilityRequirement(
                    "qualify", "Analyze qualification including the latest correction", satisfaction_criterion="Use the corrected cutoff and prove supported scenarios",
                    capability_owner="CHAT" if mode in {"direct", "chat_read"} else "FOOTBALL", owner_task_id="table",
                ),), capabilities=plan.capabilities + (CapabilityRequest(
                    "ADMIN", required=False, task_id="done", operands={"admin_action": "create_channel"},
                ),))
                if mode == "failed_action":
                    plan = replace(plan, requirements=(replace(plan.requirements[0], capability_owner="ADMIN", owner_task_id="done"),),
                        capabilities=tuple(replace(item, required=True) if item.task_id == "done" else item for item in plan.capabilities))
                if mode == "chat_read":
                    plan = replace(plan, objective="Compare Python release support", requirements=(replace(plan.requirements[0],
                        objective="Compare the current supported Python releases", satisfaction_criterion="Use the corrected support priority"),))
                context = CapabilityExecutionContext(build_capability_execution_plan(plan, fallback_action=plan.primary_action))
                calls, reviews, writes = [], [], []
                original = FootballRetrievalResult(standing_row={"points": 15},
                    requested_scope={"request_plan": {"output_payload": ["standing_row"]}})

                async def first(request, _context):
                    if request.name == "CHAT":
                        return normalize_terminal_result("CHAT", "PENDING", provider="orchestrator", task_id=request.task_id, reason="awaiting_response")
                    if request.name == "ADMIN":
                        writes.append(request.task_id)
                        if mode == "failed_action":
                            return normalize_terminal_result("ADMIN", "PERMISSION_DENIED", provider="offline", task_id=request.task_id)
                    return normalize_terminal_result(request.name, "SUCCESS", provider="offline", task_id=request.task_id,
                        scope={"operation_type": "standings", "league_id": 1, "season": 2026} if request.name == "FOOTBALL" else {},
                        payload=original if request.name == "FOOTBALL" else {"created": True})

                await execute_ordered_capabilities(context.execution_plan, {"FOOTBALL": first, "ADMIN": first, "CHAT": first}, context=context,
                    preflight=lambda *_: True)

                async def reviewer(request):
                    reviews.append(request)
                    if mode.startswith("repair_"):
                        if mode == "repair_budget":
                            return {"decision": "answer"} if len(reviews) < 3 else review("read_more", [task("too_late")])
                        if len(reviews) == 1:
                            answer = review("read_more", [task("calendar")])
                            if mode in {"repair_reference", "repair_combined"}:
                                answer["assessments"][0]["evidence_task_ids"] = ["missing_task"]
                            if mode in {"repair_duplicate", "repair_combined"}:
                                answer["reads"].append({"requirement_ids": ["qualify"], "task": task("same_operands")})
                            return answer
                        if len(reviews) == 2:
                            self.assertIn("correction", request)
                            if mode == "repair_combined":
                                self.assertIn("unavailable_evidence_reference", request["correction"]["error"])
                                self.assertIn("duplicate_investigation_read", request["correction"]["error"])
                            return review("read_more", [task("calendar")])
                        self.assertEqual(request["remaining_reads"], 0)
                        answer = review("answer", supported=True)
                        answer["assessments"][0]["evidence_task_ids"].append("calendar")
                        return answer
                    if mode == "direct":
                        answer = review("answer", supported=True)
                        answer["assessments"][0].update(basis="context", evidence_task_ids=[])
                        return answer
                    if mode == "chat_read":
                        answer = review("read_more", [task("support", "WEB", {"query": "official current Python release support schedules"})]) if len(reviews) == 1 else review("answer", supported=True)
                        answer["assessments"][0].update(basis="context" if len(reviews) == 1 else "retrieved",
                            evidence_task_ids=[] if len(reviews) == 1 else ["support"],
                            findings=[] if len(reviews) == 1 else ["The retrieved release policy supports the comparison."])
                        return answer
                    if mode == "failed_action":
                        return review("answer", supported=True)
                    if mode in {"success", "unavailable", "cancelled_during_read"}:
                        if len(reviews) == 1:
                            return review("read_more", [task("calendar"), task("rules", "WEB", {"research_question": "official current competition regulations"})])
                        if mode == "unavailable":
                            return review("limited")
                        answer = review("answer", supported=True)
                        answer["assessments"][0]["evidence_task_ids"] += ["calendar", "rules"]
                        answer["assessments"][0]["findings"] = ["Derived maximum: 15 + 3 * 3 = 24; this alone does not prove three wins are necessary."]
                        return answer
                    if mode == "budget":
                        return review("limited") if len(reviews) == 3 else review("read_more", [
                            task(f"read_{len(reviews)}_{n}", operands={"operation_family": "fixture_next", "fixture_count": 10 + n + len(reviews) * 3}) for n in range(3)
                        ])
                    if mode == "duplicate":
                        return review("read_more", [task("again", operands={"operation_family": "standings"})])
                    if mode == "resolved_duplicate":
                        return review("limited") if len(reviews) == 2 else review("read_more", [task("same_scope", operands={"operation_family": "standings", "data_focus": "standings"})])
                    if mode == "mutation":
                        return review("read_more", [task("write", "ADMIN", {"admin_action": "delete_channel"})])
                    if mode == "code":
                        return review("read_more", [task("run", "CODE", {"code_action": "run", "code_language": "python", "code": "print(1)"})])
                    if mode == "unsupported":
                        answer = review("answer", supported=True)
                        answer["assessments"][0]["evidence_task_ids"] = []
                        return answer
                    if mode == "malformed":
                        return {"decision": "answer"}
                    raise AssertionError("A cancelled or direct request must not call the reviewer")

                async def football(_message, decision, **kwargs):
                    self.assertFalse(kwargs["allow_web_fallback"])
                    operands = decision.capability_plan["football"]
                    operation = compile_football_operation("FOOTBALL_LOOKUP", "", {"semantic_source": "router", **operands})
                    calls.append((operation.fixture_count, operation.fixture_round))
                    return normalize_terminal_result("FOOTBALL", "SELECTED", provider="offline", payload=FootballRetrievalResult(),
                        scope={"operation_type": "standings", "league_id": 1, "season": 2026} if mode == "resolved_duplicate" else {})

                async def research(request, **_kwargs):
                    calls.append(request.query)
                    if mode == "unavailable":
                        return WebResearchResult(query=request.query, answer="", failure_reason="DISABLED")
                    return WebResearchResult(query=request.query, answer="The current official policy supplies the support dates." if mode == "chat_read" else "The current regulation supplies the cutoff and tiebreak rules.",
                        sources=(WebSource(title="Regulation", url="https://official.example/regulation", domain="official.example"),))

                cog = AIChatCog.__new__(AIChatCog)
                cog.bot = SimpleNamespace(llm_client=SimpleNamespace(), settings=SimpleNamespace(), api_football_client=None)
                cog._execute_football_action = football
                cog._web_research = SimpleNamespace(research=research)
                message = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), author=SimpleNamespace(id=3),
                    created_at=datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc))

                def active():
                    return mode != "cancelled" and not (mode == "cancelled_during_read" and calls)

                async def execute(reads):
                    await cog._execute_live_media_composition_result_only(message, "original question", replace(context.execution_plan, ordered_capabilities=reads),
                        settings=SimpleNamespace(), execution_context=context, can_continue=active)

                async def run():
                    return await cog._answer_capability_request(message, context, user_message="Correction: prioritize support duration." if mode == "chat_read" else "Correction: the cutoff is eight, verify it.",
                        conversation_history=[{"role": "user", "content": "Compare the supported Python releases." if mode == "chat_read" else "What does my team need to qualify?"}],
                        response_call=reviewer, execute_reads=execute, can_continue=active)

                if mode in {"duplicate", "mutation", "code", "malformed", "unsupported", "cancelled", "cancelled_during_read", "failed_action", "repair_budget"}:
                    with self.assertRaises(ValueError):
                        await run()
                else:
                    self.assertEqual(await run(), "Supported answer with any missing evidence stated.")
                if mode not in {"direct", "chat_read"}:
                    self.assertIs(context.results["table"].payload, original)
                self.assertTrue(context.results["table"].satisfies_request)
                self.assertEqual(writes, ["done"])
                self.assertLessEqual(len(reviews), 3)
                self.assertLessEqual(len(context.evidence_requirements), 6)
                self.assertEqual(context.bundle.satisfied, mode in {"success", "direct", "chat_read", "repair_reference", "repair_duplicate", "repair_combined"})
                if mode == "success":
                    self.assertEqual(calls[0], (50, "Opening - 17"))
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[1], "official current competition regulations")
                    self.assertEqual(len(reviews), 2)
                    self.assertIn("corrected cutoff", reviews[0]["evidence"])
                    self.assertIn("the cutoff is eight", reviews[0]["user_message"])
                    final = cog._capability_bundle_prompt_context(context)
                    self.assertIn("15 + 3 * 3", final)
                    self.assertIn("Request satisfied: True", final)
                elif mode == "budget":
                    self.assertEqual(len(calls), 6)
                    self.assertEqual(len(reviews), 3)
                    self.assertEqual(reviews[-1]["remaining_reads"], 0)
                elif mode == "resolved_duplicate":
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(context.results["same_scope"].reason, "duplicate_resolved_read:table")
                elif mode == "unavailable":
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(context.results["rules"].native_outcome, "DISABLED")
                    self.assertIn("qualification/tiebreak rules", cog._capability_bundle_prompt_context(context))
                elif mode == "cancelled_during_read":
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(context.investigation_stop_reason, "superseded")
                    self.assertNotIn("rules", context.results)
                elif mode == "chat_read":
                    self.assertEqual(len(reviews), 2)
                    self.assertEqual(calls, ["official current Python release support schedules"])
                    self.assertIn("prioritize support duration", reviews[1]["user_message"])
                    self.assertEqual(context.results["table"].payload["answer"], "Supported answer with any missing evidence stated.")
                elif mode in {"repair_reference", "repair_duplicate", "repair_combined"}:
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(reviews), 3)
                    self.assertIn("reference" if mode == "repair_reference" else "identical executable operands", reviews[1]["correction"]["error"])
                else:
                    self.assertFalse(calls)



if __name__ == "__main__":
    unittest.main()
