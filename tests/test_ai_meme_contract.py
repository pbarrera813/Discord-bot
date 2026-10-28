from __future__ import annotations

import unittest

from services.ai_capability_orchestrator import validate_native_capability_contract
from services.ai_operation_catalog import validate_operands


def _meme_task(task_id: str = "meme_1", operands: dict | None = None) -> dict:
    return {
        "name": "MEME",
        "task_id": task_id,
        "required": True,
        "evidence_span": "meme this",
        "depends_on": [],
        "scope": "IMAGE_GENERATION",
        "independent": True,
        "independent_research": False,
        "owner_action": None,
        "operands": {"meme_action": "random", "meme_top": "top", "meme_bottom": "bottom"} if operands is None else operands,
        "input_bindings": [],
        "source_refs": [],
        "requested_outputs": ["artifact"],
    }


class MemeContractTests(unittest.TestCase):
    def test_operands_accept_each_action_and_reject_bad_values(self) -> None:
        self.assertIsNone(validate_operands("MEME", {"meme_action": "template", "meme_template": "drake", "meme_top": "a"}))
        self.assertIsNone(validate_operands("MEME", {"meme_action": "random", "meme_top": "a", "meme_bottom": "b"}))
        self.assertIsNone(validate_operands("MEME", {"meme_action": "custom", "meme_bottom": "b"}))
        self.assertIsNotNone(validate_operands("MEME", {"meme_action": "template", "meme_top": "x" * 201}))
        self.assertIsNotNone(validate_operands("MEME", {"meme_action": "template", "meme_template": 7}))
        self.assertIsNotNone(validate_operands("MEME", {"meme_action": "shout", "meme_top": "a"}))
        self.assertIsNotNone(validate_operands("MEME", {"meme_action": "random", "text": "a"}))


    def test_router_shaped_meme_artifact_plan_validates(self) -> None:
        raw = {
            "objective": "caption an image with a meme",
            "capabilities": [_meme_task()],
            "requirements": [{
                "requirement_id": "r_meme",
                "objective": "caption an image with a meme",
                "required": True,
                "satisfaction_criterion": "artifact",
                "capability_owner": "MEME",
                "owner_task_id": "meme_1",
                "depends_on": [],
            }],
            "response_delivery": {"artifacts": [{"upstream_task_id": "meme_1", "output_field": "artifact"}]},
        }
        self.assertIsNone(validate_native_capability_contract({"capability_plan": raw}))


class RouterOperandLeakTests(unittest.TestCase):
    def test_the_production_meme_custom_operand_set_validates(self) -> None:
        operands = {
            "meme_action": "custom", "meme_template": None, "meme_top": "Cuando intentas ser productivo",
            "meme_bottom": "Pero el lunes te gana", "emoji": None, "emojis": [], "query": None,
            "research_question": None, "url": None, "source_url": None, "source_scope": None,
            "expected_content": None, "operation": None, "prompt": None, "selection_count": 1,
        }
        self.assertIsNone(validate_operands("MEME", operands))


if __name__ == "__main__":
    unittest.main()
