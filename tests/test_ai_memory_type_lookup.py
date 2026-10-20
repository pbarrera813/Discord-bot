from __future__ import annotations

import asyncio
import json
import unittest

from tests.test_ai_tagging import _FakeResponse, _FakeSession, _ImageXAIClient, _typed_memory_route

RULES_ONLY = {"memory_type": "SERVER_RULE", "subject": None, "key": None, "value": None, "scope": None}


class TypeLevelLookupRouteTests(unittest.TestCase):
    def test_a_lookup_naming_only_a_memory_type_is_a_valid_route(self) -> None:
        route = _typed_memory_route(
            typed_action="SERVER_MEMORY_LOOKUP", compatibility_action="SERVER_MEMORY_LOOKUP", memory=RULES_ONLY
        )
        session = _FakeSession(
            _FakeResponse(status=200, text=json.dumps({"status": "completed", "output_text": json.dumps(route)}))
        )
        result = asyncio.run(
            _ImageXAIClient(session).route_ai_interaction(
                bot_name="Nitori",
                author_name="Pablo",
                current_message="what do you remember about the server rules",
                anchor_type="DIRECT_MENTION",
                recent_context=[],
            )
        )
        self.assertTrue(result["valid"], result.get("failure_reason"))

    def test_a_delete_naming_only_a_memory_type_is_still_refused(self) -> None:
        route = _typed_memory_route(
            typed_action="SERVER_MEMORY_DELETE", compatibility_action="SERVER_MEMORY_DELETE", memory=RULES_ONLY
        )
        session = _FakeSession(
            _FakeResponse(status=200, text=json.dumps({"status": "completed", "output_text": json.dumps(route)}))
        )
        result = asyncio.run(
            _ImageXAIClient(session).route_ai_interaction(
                bot_name="Nitori",
                author_name="Pablo",
                current_message="forget the rules",
                anchor_type="DIRECT_MENTION",
                recent_context=[],
            )
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_reason"], "missing_field")


if __name__ == "__main__":
    unittest.main()
