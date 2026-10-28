from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityExecutionPlan, CapabilityRequest, CapabilityTerminalResult, CapabilityTerminalStatus, InputBinding, build_capability_execution_plan, plan_from_route_payload, validate_native_capability_contract


class ChatTaskExecutionTests(unittest.IsolatedAsyncioTestCase):

    def _native_payload(self, *tasks: CapabilityRequest) -> dict[str, object]:
        capabilities: list[dict[str, object]] = []
        requirements: list[dict[str, object]] = []
        requirement_ids = {task.task_id: f"{task.task_id}_requirement" for task in tasks}
        for task in tasks:
            name = task.name.upper()
            objective = str(task.operands.get("objective") or task.operands.get("query") or "test objective")
            operands = dict(task.operands)
            if name == "CHAT":
                operands = {}
            elif name == "WEB":
                operands = {"query": objective}
            capabilities.append({
                "name": name,
                "task_id": task.task_id,
                "required": task.required,
                "evidence_span": task.evidence_span or objective,
                "depends_on": list(task.depends_on),
                "scope": task.scope,
                "independent": task.independent,
                "independent_research": task.independent_research or name == "WEB",
                "selection_count": task.selection_count,
                "owner_action": task.owner_action,
                "operands": operands,
                "input_bindings": [
                    {"upstream_task_id": binding.upstream_task_id, "output_field": binding.output_field}
                    for binding in task.input_bindings
                ],
                "source_refs": list(task.source_refs),
                "requested_outputs": list(task.requested_outputs or ("answer",)),
            })
            requirements.append({
                "requirement_id": requirement_ids[task.task_id],
                "objective": objective,
                "required": task.required,
                "satisfaction_criterion": "answer",
                "capability_owner": name,
                "owner_task_id": task.task_id,
                "depends_on": [requirement_ids[dependency] for dependency in task.depends_on],
            })
        raw = {
            "action": "CHAT",
            "capability_plan": {
                "objective": "test composition",
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
        return raw

    def _plan(self, *tasks: CapabilityRequest) -> CapabilityExecutionPlan:
        plan = plan_from_route_payload(self._native_payload(*tasks))
        self.assertTrue(plan.valid, plan.failure_reason)
        return build_capability_execution_plan(plan, fallback_action="CHAT")

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
        return cog

    def _message(self) -> object:
        return SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2, name="general"),
            author=SimpleNamespace(id=3, display_name="user"),
        )

    async def test_repeated_chat_tasks_call_provider_once_with_independent_objectives(self) -> None:
        llm = SimpleNamespace(chat=AsyncMock(side_effect=["first answer", "second answer"]))
        cog = self._cog(llm)
        context = await cog._execute_live_media_composition_result_only(
            self._message(),
            "global prompt",
            self._plan(
                CapabilityRequest("CHAT", task_id="chat_a", scope="GENERAL_REASONING", operands={"objective": "first"}),
                CapabilityRequest("CHAT", task_id="chat_b", scope="GENERAL_REASONING", operands={"objective": "second"}),
            ),
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual(llm.chat.await_count, 2)
        self.assertEqual(context.results["chat_a"].payload, {"answer": "first answer"})
        self.assertEqual(context.results["chat_b"].payload, {"answer": "second answer"})

    async def test_failed_dependency_holds_chat_without_provider_call(self) -> None:
        llm = SimpleNamespace(chat=AsyncMock(return_value="should not run"))
        cog = self._cog(llm)

        async def failed_web(_message, _prompt, **_kwargs):
            return CapabilityTerminalResult(
                "WEB", CapabilityTerminalStatus.PROVIDER_FAILURE, "web", "FAILURE",
                reason="offline", satisfies_request=False,
            )

        cog._execute_web_result_only = failed_web
        context = await cog._execute_live_media_composition_result_only(
            self._message(),
            "global prompt",
            self._plan(
                CapabilityRequest("WEB", task_id="web", scope="BROAD_RESEARCH", operands={"objective": "source"}),
                CapabilityRequest("CHAT", task_id="chat", scope="GENERAL_REASONING", depends_on=("web",), operands={"objective": "explain"}, input_bindings=(InputBinding("web", "answer"),)),
            ),
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual(llm.chat.await_count, 0)
        self.assertEqual(context.results["chat"].status, CapabilityTerminalStatus.NOT_EXECUTED)

    async def test_chat_consumes_authorized_bound_answer(self) -> None:
        llm = SimpleNamespace(chat=AsyncMock(return_value="grounded answer"))
        cog = self._cog(llm)

        async def web(_message, prompt, **_kwargs):
            return CapabilityTerminalResult(
                "WEB", CapabilityTerminalStatus.SUCCESS, "web", "SUCCESS",
                payload={"answer": prompt},
                provenance={"authorized": True},
                satisfies_request=True,
            )

        cog._execute_web_result_only = web
        context = await cog._execute_live_media_composition_result_only(
            self._message(),
            "global prompt",
            self._plan(
                CapabilityRequest("WEB", task_id="web", scope="BROAD_RESEARCH", operands={"objective": "source"}, requested_outputs=("answer",)),
                CapabilityRequest("CHAT", task_id="chat", scope="GENERAL_REASONING", depends_on=("web",), operands={"objective": "explain"}, input_bindings=(InputBinding("web", "answer"),)),
            ),
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual(llm.chat.await_count, 1)
        self.assertIn("answer=source", llm.chat.await_args.kwargs["trusted_capability_context"])
        self.assertEqual(context.results["chat"].task_id, "chat")


if __name__ == "__main__":
    unittest.main()
