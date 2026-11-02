from __future__ import annotations

import unittest

from services.ai_capability_orchestrator import validate_native_capability_contract


def _task(task_id: str, name: str = "CHAT", *, depends_on=(), outputs=("answer",), operands=None):
    return {
        "name": name,
        "task_id": task_id,
        "required": True,
        "evidence_span": task_id,
        "depends_on": list(depends_on),
        "scope": "GENERAL_REASONING" if name == "CHAT" else "BROAD_RESEARCH",
        "independent": True,
        "independent_research": name == "WEB",
        "owner_action": None,
        "operands": {} if operands is None else operands,
        "input_bindings": [],
        "source_refs": [],
        "requested_outputs": list(outputs),
    }


class NativeTaskSchemaTests(unittest.TestCase):

    def test_native_requires_ids_operands_and_requirement_owner_task(self):
        missing = {"capabilities": [_task("a")]}
        missing["capabilities"][0].pop("operands")
        self.assertEqual(validate_native_capability_contract({"capability_plan": missing}), "inconsistent_capability_plan")
        plan = {"capabilities": [_task("a")], "requirements": [{
            "requirement_id": "r", "objective": "answer", "required": True,
            "satisfaction_criterion": "answer", "capability_owner": "CHAT",
            "depends_on": [],
        }]}
        self.assertEqual(validate_native_capability_contract({"capability_plan": plan}), "inconsistent_capability_plan")


    def test_binding_requires_declared_dependency_and_known_output(self):
        producer = _task("source", "WEB", outputs=("summary",), operands={"query": "x"})
        consumer = _task("visual", "IMAGE_ANALYZE", depends_on=("source",), outputs=("analysis",))
        consumer["input_bindings"] = [{"upstream_task_id": "source", "output_field": "unknown"}]
        self.assertEqual(validate_native_capability_contract({"capability_plan": {"capabilities": [producer, consumer]}}), "inconsistent_capability_plan")

    def test_native_rejects_operand_type_and_bounds_at_entry(self):
        task = _task("mc", "MINECRAFT", outputs=("status",), operands={"address": 17})
        raw = {"capabilities": [task], "requirements": [{
            "requirement_id": "r", "objective": "status", "required": True,
            "satisfaction_criterion": "status", "capability_owner": "MINECRAFT",
            "owner_task_id": "mc", "depends_on": [],
        }]}
        self.assertEqual(validate_native_capability_contract({"capability_plan": raw}), "inconsistent_capability_plan")

        task["operands"] = {"address": "one.example", "selection_count": 1}
        self.assertEqual(validate_native_capability_contract({"capability_plan": raw}), "inconsistent_capability_plan")
        raw["objective"] = "Read the requested fixtures"
        raw["requirements"][0]["capability_owner"] = "FOOTBALL"
        task.update(name="FOOTBALL", requested_outputs=["result"], operands={
            "operation_family": "fixture_next", "fixture_count": 50, "fixture_round": "Opening - 17"})
        self.assertIsNone(validate_native_capability_contract({"capability_plan": raw}))
        for count in (0, 51, True, "50"):
            task["operands"]["fixture_count"] = count
            self.assertEqual(validate_native_capability_contract({"capability_plan": raw}), "inconsistent_capability_plan")


    def test_delivery_artifacts_require_explicit_declared_producer_binding(self):
        producer = _task("image", "IMAGE_GENERATE", outputs=("artifact",), operands={"prompt": "x"})
        raw = {"objective": "generate image", "capabilities": [producer], "requirements": [{"requirement_id": "r", "objective": "generate image", "required": True, "satisfaction_criterion": "artifact", "capability_owner": "IMAGE_GENERATE", "owner_task_id": "image", "depends_on": []}], "response_delivery": {"artifacts": [{"upstream_task_id": "image", "output_field": "artifact"}]}}
        self.assertIsNone(validate_native_capability_contract({"capability_plan": raw}))
        raw["response_delivery"]["artifacts"][0]["upstream_task_id"] = "IMAGE_GENERATE"
        self.assertEqual(validate_native_capability_contract({"capability_plan": raw}), "inconsistent_capability_plan")



if __name__ == "__main__":
    unittest.main()
