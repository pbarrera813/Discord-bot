from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityRequest, build_capability_execution_plan, plan_from_route_payload, validate_native_capability_contract


class ImageAnalyzeChatGateTests(unittest.IsolatedAsyncioTestCase):
    def _message(self) -> object:
        return SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2, name="general"),
            author=SimpleNamespace(id=3, display_name="user"),
        )

    def _cog(self, llm: object) -> AIChatCog:
        cog = AIChatCog.__new__(AIChatCog)
        cog.bot = SimpleNamespace(
            llm_client=llm,
            user=SimpleNamespace(display_name="Nitori"),
            is_owner_user=lambda _user: False,
        )
        cog._channel_reference = lambda _channel: "channel:1"
        cog._serialize_text_channels = lambda _guild: []
        cog._serialize_custom_emojis = lambda _guild: []
        cog._admin_actions = SimpleNamespace()
        cog._server_memory = None
        cog._server_memory_context = None
        cog._external_sources = SimpleNamespace(max_sources=3)
        return cog

    def _plan(self, *tasks: CapabilityRequest):
        requirements = []
        capabilities = []
        requirement_ids = {task.task_id: f"{task.task_id}_requirement" for task in tasks}
        for task in tasks:
            name = task.name.upper()
            objective = str(
                task.operands.get("objective")
                or task.operands.get("query")
                or "visual analysis"
            )
            if name == "CHAT":
                operands = {}
            elif name == "IMAGE_ANALYZE":
                operands = {"selection_count": 1}
            else:
                operands = dict(task.operands)
            capabilities.append({
                "name": name,
                "task_id": task.task_id,
                "required": True,
                "evidence_span": task.evidence_span or objective,
                "depends_on": list(task.depends_on),
                "scope": task.scope,
                "independent": True,
                "independent_research": name == "WEB",
                "selection_count": task.selection_count,
                "owner_action": None,
                "operands": operands,
                "input_bindings": [
                    {"upstream_task_id": binding.upstream_task_id, "output_field": binding.output_field}
                    for binding in task.input_bindings
                ],
                "source_refs": list(task.source_refs),
                "requested_outputs": list(task.requested_outputs or (("answer",) if name == "CHAT" else ("result",))),
            })
            requirements.append({
                "requirement_id": requirement_ids[task.task_id],
                "objective": objective,
                "required": True,
                "satisfaction_criterion": "grounded result",
                "capability_owner": name,
                "owner_task_id": task.task_id,
                "depends_on": [requirement_ids[item] for item in task.depends_on],
            })
        raw = {
            "action": "CHAT",
            "capability_plan": {
                "objective": "inspect and explain the image",
                "requirements": requirements,
                "capabilities": capabilities,
                "primary_action": "CHAT",
                "response_delivery": {"artifacts": []},
                "external_source": {},
                "football": {},
                "valid": True,
                "semantic_source": "router",
                "semantic_status": "SUCCESS",
            },
        }
        diagnostics: list[dict[str, object]] = []
        self.assertIsNone(validate_native_capability_contract(raw, diagnostic_sink=diagnostics), diagnostics)
        plan = plan_from_route_payload(raw)
        self.assertTrue(plan.valid, plan.failure_reason)
        return build_capability_execution_plan(plan, fallback_action="CHAT")


    def test_repeated_valid_router_chat_tasks_are_result_only(self) -> None:
        plan = self._plan(
            CapabilityRequest("CHAT", task_id="chat_a", scope="GENERAL_REASONING", operands={"objective": "first"}),
            CapabilityRequest("CHAT", task_id="chat_b", scope="GENERAL_REASONING", operands={"objective": "second"}),
        )
        self.assertTrue(plan.valid)
        self.assertEqual(tuple(item.task_id for item in plan.ordered_capabilities), ("chat_a", "chat_b"))
        self.assertTrue(AIChatCog._is_result_only_composition_plan(plan))


if __name__ == "__main__":
    unittest.main()
