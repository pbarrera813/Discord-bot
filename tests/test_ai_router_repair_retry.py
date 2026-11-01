from __future__ import annotations

import asyncio
import copy
import json
import unittest

from tests.test_ai_tagging import _FakeResponse, _FakeSession, _ImageXAIClient, _native_positive_route, _route_payload, _typed_memory_route

RULES_ONLY = {"memory_type": "SERVER_RULE", "subject": None, "key": None, "value": None, "scope": None}


def _valid_route() -> dict:
    return _typed_memory_route(
        typed_action="SERVER_MEMORY_LOOKUP", compatibility_action="SERVER_MEMORY_LOOKUP", memory=RULES_ONLY
    )


def _route_with_foreign_operand() -> dict:
    route = copy.deepcopy(_valid_route())
    route["capability_plan"]["capabilities"][0]["operands"]["query"] = "belongs to another capability"
    return route


def _response(route: dict) -> _FakeResponse:
    return _FakeResponse(status=200, text=json.dumps({"status": "completed", "output_text": json.dumps(route)}))


def _route(session: _FakeSession) -> dict:
    return asyncio.run(_ImageXAIClient(session).route_ai_interaction(
        bot_name="Nitori", author_name="Pablo", current_message="what do you remember about the rules",
        anchor_type="DIRECT_MENTION", recent_context=[],
    ))


class RouterRepairRetryTests(unittest.TestCase):
    def test_a_refused_plan_is_sent_back_once_with_the_validator_reason(self) -> None:
        bad = _route_with_foreign_operand()
        session = _FakeSession([_response(bad), _response(_valid_route())])

        result = _route(session)

        self.assertTrue(result["valid"], result.get("failure_reason"))
        self.assertEqual(len(session.calls), 2)
        retry_input = session.calls[1]["json"]["input"]
        self.assertEqual([item["role"] for item in retry_input], ["system", "user", "assistant", "user"])
        self.assertEqual(json.loads(retry_input[2]["content"]), bad)
        self.assertIn("inconsistent_capability_plan", retry_input[3]["content"])
        long_span = _valid_route()
        long_span["capability_plan"]["capabilities"][0]["evidence_span"] = "x" * 183
        session = _FakeSession([_response(long_span), _response(_valid_route())])
        self.assertTrue(_route(session)["valid"])
        feedback = session.calls[1]["json"]["input"][-1]["content"]
        self.assertIn("capabilities[0].evidence_span", feedback)
        self.assertIn("length 183 exceeds 180", feedback)
        from tests.test_ai_evidence_budget import _web_budget_route
        route = _web_budget_route(("table",))
        route["action"] = route["capability_plan"]["primary_action"] = "FOOTBALL_LOOKUP"
        route["capability_plan"]["requirements"][0]["capability_owner"] = "FOOTBALL"
        route["capability_plan"]["capabilities"][0].update(name="FOOTBALL", scope="STRUCTURED_DATA", independent_research=False,
            operands={"operation_family":"standings", "team_candidates":[]}, requested_outputs=["result"])
        route = _route_payload(_native_positive_route(route))
        corrected = copy.deepcopy(route)
        route["capability_plan"]["capabilities"][0]["operands"]["team_candidates"] = ["Club A", "Club B"]
        session = _FakeSession([_response(route), _response(corrected)])
        self.assertTrue(_route(session)["valid"])
        feedback = session.calls[1]["json"]["input"][-1]["content"]
        self.assertIn("invalid_team_arity", feedback)
        self.assertIn("team_candidates", feedback)
        self.assertIn("Club A", feedback)
        self.assertIn("Club B", feedback)

    def test_the_retry_is_bounded_to_one(self) -> None:
        session = _FakeSession([_response(_route_with_foreign_operand()), _response(_route_with_foreign_operand())])

        result = _route(session)

        self.assertFalse(result["valid"])
        self.assertEqual(len(session.calls), 2)

    def test_a_valid_plan_is_not_retried(self) -> None:
        session = _FakeSession([_response(_valid_route())])

        self.assertTrue(_route(session)["valid"])
        self.assertEqual(len(session.calls), 1)

    def test_a_provider_error_is_not_retried(self) -> None:
        session = _FakeSession([_FakeResponse(status=400, text='{"error":"rejected"}')])

        self.assertFalse(_route(session)["valid"])
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
