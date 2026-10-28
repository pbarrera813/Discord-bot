from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import (
    CapabilityExecutionContext, CapabilityRequirement, build_capability_execution_plan,
    normalize_terminal_result,
)
from services.web_research import WebResearchResult, WebSource

from tests.test_ai_tagging import _FakeResponse, _FakeSession, _ImageXAIClient, _explicit_test_plan

FAILURES = [{"task_id": "t1", "capability": "FOOTBALL", "outcome": "PROVIDER_FAILURE", "objective": "stats of Jude Bellingham"}]


def _recover(parsed: object, failures: list[dict[str, str]] | None = None) -> tuple[list[dict[str, str]], _FakeSession]:
    text = parsed if isinstance(parsed, str) else json.dumps({"output_text": json.dumps(parsed)})
    session = _FakeSession(_FakeResponse(status=200, text=text))
    client = _ImageXAIClient(session)
    result = asyncio.run(
        client.plan_information_recovery(
            user_message="stats de J. Bellingham",
            conversation_history=[{"role": "user", "content": "hablando del Real Madrid"}],
            failures=failures or FAILURES,
            request_date="2026-10-01",
        )
    )
    return result, session


class RecoveryPlannerClientTests(unittest.TestCase):

    def test_planner_receives_the_original_operation_and_fulfillment_criteria(self) -> None:
        failure = {**FAILURES[0], "operation": "team_seasons", "satisfaction_criterion": "List the provider's registered seasons"}
        _result, session = _recover({"searches": []}, [failure])
        sent = json.loads(session.calls[0]["json"]["input"][1]["content"])
        self.assertEqual(sent["failures"], [failure])

    def test_fulfillment_verdict_must_be_explicit_and_receives_the_evidence(self) -> None:
        for verdict in (True, False, "true", None):
            with self.subTest(verdict=verdict):
                session = _FakeSession(_FakeResponse(status=200, text=json.dumps({"output_text": json.dumps({"satisfies_request": verdict})})))
                client = _ImageXAIClient(session)
                result = asyncio.run(client.assess_information_recovery(
                    user_message="Which seasons are registered?",
                    failure={**FAILURES[0], "operation": "team_seasons", "satisfaction_criterion": "List registered seasons"},
                    answer="The club was founded long ago.",
                    sources=["https://example.test/history"], request_date="2026-10-26",
                ))
                self.assertIs(result, verdict is True)
                sent = json.loads(session.calls[0]["json"]["input"][1]["content"])
                self.assertEqual(sent["failure"]["operation"], "team_seasons")
                self.assertEqual(sent["failure"]["satisfaction_criterion"], "List registered seasons")
                self.assertEqual(sent["answer"], "The club was founded long ago.")
                self.assertEqual(sent["sources"], ["https://example.test/history"])

    def test_decisions_that_do_not_validate_search_nothing(self) -> None:
        cases = [
            {"searches": [{"task_id": "t1", "search": False, "query": "Jude Bellingham stats this season"}]},
            {"searches": [{"task_id": "other", "search": True, "query": "Jude Bellingham stats this season"}]},
            {"searches": [{"task_id": "t1", "search": True, "query": "x"}]},
            {"searches": [{"task_id": "t1", "search": True, "query": ""}]},
            {"searches": "nope"},
            {},
            "not json",
        ]
        for parsed in cases:
            with self.subTest(parsed=parsed):
                result, _session = _recover(parsed)
                self.assertEqual(result, [])

    def test_at_most_two_searches_and_one_per_task(self) -> None:
        failures = [{**FAILURES[0], "task_id": f"t{index}"} for index in range(4)]
        decision = {"searches": [{"task_id": f"t{index}", "search": True, "query": f"question number {index}"} for index in range(4)]}
        decision["searches"].append({"task_id": "t0", "search": True, "query": "duplicate of the first task"})
        result, _session = _recover(decision, failures)
        self.assertEqual([item["task_id"] for item in result], ["t0", "t1"])


class RecoveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_keeps_the_contract_date_and_failed_result_until_verified(self) -> None:
        for verdict in (True, False, None, "true", RuntimeError("synthetic verdict failure")):
            with self.subTest(verdict=verdict):
                plan = _explicit_test_plan("FOOTBALL", "seasons", "FOOTBALL_TEAM_QUERY", scope="STRUCTURED_DATA", operands={"operation_family": "team_seasons", "team_candidates": ["Northbank Town"]})
                plan = replace(plan, requirements=(CapabilityRequirement(
                    requirement_id="registered", objective="List registered seasons",
                    satisfaction_criterion="Return seasons actually registered by the provider",
                    capability_owner="FOOTBALL", owner_task_id="seasons",
                ),))
                context = CapabilityExecutionContext(build_capability_execution_plan(plan, fallback_action=plan.primary_action))
                prior = normalize_terminal_result("FOOTBALL", "NOT_FOUND", provider="api-football", task_id="seasons")
                context.record(prior)
                planner_calls, verdict_calls, web_calls = [], [], []

                async def planner(**kwargs):
                    planner_calls.append(kwargs)
                    return [{"task_id": "seasons", "query": "Northbank registered seasons"}]

                async def assess(**kwargs):
                    verdict_calls.append(kwargs)
                    if isinstance(verdict, Exception):
                        raise verdict
                    return verdict

                payload = WebResearchResult(query="Northbank registered seasons", answer="The club has played since its foundation.", sources=(WebSource(title="History", url="https://example.test/history", domain="example.test"),))

                async def research(request, **kwargs):
                    web_calls.append(request)
                    return payload

                cog = AIChatCog.__new__(AIChatCog)
                cog.bot = SimpleNamespace(llm_client=SimpleNamespace(plan_information_recovery=planner, assess_information_recovery=assess), api_football_client=SimpleNamespace(today_iso=lambda: "2026-10-26", timezone_name="America/Mexico_City"), settings=SimpleNamespace(xai_web_search_max_sources=3))
                cog._web_research = SimpleNamespace(research=research)
                cog._build_conversation_history = lambda _key: []
                message = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), author=SimpleNamespace(id=3), created_at=datetime(2026, 10, 27, 1, 19, tzinfo=timezone.utc))
                await cog._recover_unavailable_information(message, context, user_message="Which seasons are registered?", settings=SimpleNamespace())
                self.assertEqual(planner_calls[0]["request_date"], "2026-10-26")
                self.assertEqual(web_calls[0].request_date, "2026-10-26")
                failure = planner_calls[0]["failures"][0]
                self.assertEqual(failure["operation"], "team_seasons")
                self.assertEqual(failure["satisfaction_criterion"], "Return seasons actually registered by the provider")
                self.assertEqual(verdict_calls[0]["failure"], failure)
                self.assertEqual(verdict_calls[0]["request_date"], "2026-10-26")
                self.assertIs(context.bundle.satisfied, verdict is True)
                if verdict is True:
                    self.assertEqual(context.results["seasons"].provider, "web-fallback")
                    self.assertEqual(context.results["seasons"].task_id, prior.task_id)
                    self.assertIs(context.results["seasons"].payload, payload)
                else:
                    self.assertIs(context.results["seasons"], prior)


if __name__ == "__main__":
    unittest.main()
