from __future__ import annotations

import asyncio
import json
import unittest

from tests.test_ai_tagging import _FakeResponse, _FakeSession, _ImageXAIClient

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


if __name__ == "__main__":
    unittest.main()
