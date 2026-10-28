from __future__ import annotations

import asyncio
import unittest

from services.ai_capability_orchestrator import CapabilityPlan, CapabilityRequest, CapabilityTerminalStatus, build_capability_execution_plan, execute_ordered_capabilities, normalize_capability_plan, normalize_terminal_result, validate_native_capability_contract


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

    def test_repeated_tasks_keep_independent_results(self):
        raw = {"objective": "", "capabilities": [_task("a"), _task("b")], "requirements": [
            {"requirement_id": "ra", "objective": "a", "required": True, "satisfaction_criterion": "a", "capability_owner": "CHAT", "owner_task_id": "a", "depends_on": []},
            {"requirement_id": "rb", "objective": "b", "required": True, "satisfaction_criterion": "b", "capability_owner": "CHAT", "owner_task_id": "b", "depends_on": []},
        ]}
        self.assertIsNone(validate_native_capability_contract({"capability_plan": raw}))
        execution = build_capability_execution_plan(normalize_capability_plan(raw, action="CHAT"), fallback_action="CHAT")
        async def handler(request, _context):
            return normalize_terminal_result("CHAT", "SUCCESS", provider="test", task_id=request.task_id, satisfies_request=True)
        context = asyncio.run(execute_ordered_capabilities(execution, {"CHAT": handler}))
        self.assertEqual(set(context.bundle.by_task), {"a", "b"})


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


    def test_delivery_artifacts_require_explicit_declared_producer_binding(self):
        producer = _task("image", "IMAGE_GENERATE", outputs=("artifact",), operands={"prompt": "x"})
        raw = {"objective": "generate image", "capabilities": [producer], "requirements": [{"requirement_id": "r", "objective": "generate image", "required": True, "satisfaction_criterion": "artifact", "capability_owner": "IMAGE_GENERATE", "owner_task_id": "image", "depends_on": []}], "response_delivery": {"artifacts": [{"upstream_task_id": "image", "output_field": "artifact"}]}}
        self.assertIsNone(validate_native_capability_contract({"capability_plan": raw}))
        raw["response_delivery"]["artifacts"][0]["upstream_task_id"] = "IMAGE_GENERATE"
        self.assertEqual(validate_native_capability_contract({"capability_plan": raw}), "inconsistent_capability_plan")


    def test_invalid_execution_plan_marks_tasks_not_executed(self):
        request = CapabilityRequest("CHAT", task_id="a", operands={}, requested_outputs=("answer",))
        plan = CapabilityPlan("", (request,), "CHAT", {}, {}, {}, valid=False)
        execution = build_capability_execution_plan(plan, fallback_action="CHAT")
        async def handler(_request, _context):
            raise AssertionError("invalid plan must not dispatch")
        context = asyncio.run(execute_ordered_capabilities(execution, {"CHAT": handler}))
        self.assertEqual(context.results["a"].status, CapabilityTerminalStatus.NOT_EXECUTED)

    def test_catalog_is_validated_before_mutation_and_preflight_exceptions_hold_batch(self):
        good = CapabilityRequest("ADMIN", task_id="good", operands={"admin_action": "ban"}, requested_outputs=("result",))
        bad = CapabilityRequest("ADMIN", task_id="bad", operands={"operation": "ban"}, requested_outputs=("result",))
        semantic = CapabilityPlan("", (good, bad), "ADMIN_ACTION", {}, {}, {})
        execution = build_capability_execution_plan(semantic, fallback_action="ADMIN_ACTION")
        calls = []
        async def handler(request, _context):
            calls.append(request.task_id)
            return normalize_terminal_result("ADMIN", "SUCCESS", provider="test", task_id=request.task_id, satisfies_request=True)
        async def preflight(_tasks, _context):
            raise RuntimeError("preflight")
        context = asyncio.run(execute_ordered_capabilities(execution, {"ADMIN": handler}, preflight=preflight))
        self.assertEqual(calls, [])
        self.assertEqual(context.results["good"].status, CapabilityTerminalStatus.NOT_EXECUTED)
        self.assertEqual(context.results["bad"].status, CapabilityTerminalStatus.VALIDATION_FAILED)


if __name__ == "__main__":
    unittest.main()
