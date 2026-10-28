from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import (
    build_capability_execution_plan,
    normalize_capability_plan,
    validate_native_capability_contract,
)
from services.web_research import WebResearchResult

VISUAL_OBJECTIVE = "compare the discovered pictures"


def _web(task_id: str) -> dict[str, object]:
    return {
        "name": "WEB", "task_id": task_id, "required": True, "evidence_span": task_id, "depends_on": [],
        "scope": "BROAD_RESEARCH", "independent": True, "independent_research": True,
        "selection_count": 1, "owner_action": None, "operands": {"query": f"query {task_id}"},
        "input_bindings": [], "source_refs": [], "requested_outputs": ["answer"],
    }


def _image(task_id: str, web_task_id: str) -> dict[str, object]:
    return {
        "name": "IMAGE_ANALYZE", "task_id": task_id, "required": True, "evidence_span": task_id,
        "depends_on": [web_task_id], "scope": "MEDIA_ANALYSIS", "independent": False,
        "independent_research": False, "selection_count": 2, "owner_action": None, "operands": {},
        "input_bindings": [], "source_refs": [], "requested_outputs": ["text"],
    }


def _requirement(req_id: str, owner: str, task_id: str, objective: str, depends_on: list[str]) -> dict[str, object]:
    return {
        "requirement_id": req_id, "objective": objective, "satisfaction_criterion": "answer",
        "required": True, "capability_owner": owner, "owner_task_id": task_id, "depends_on": depends_on,
    }


class _RecordingWeb:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def research(self, request, **_kwargs):
        self.requests.append(request)
        return WebResearchResult(query=request.query, answer="answer")


class NativeWebVisualTaskTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, requirements: list[dict[str, object]], capabilities: list[dict[str, object]]):
        payload = {
            "objective": "research and inspect", "semantic_source": "router", "semantic_status": "SUCCESS",
            "primary_action": "CHAT", "requirements": requirements, "capabilities": capabilities,
        }
        diagnostic: list[dict[str, object]] = []
        self.assertIsNone(validate_native_capability_contract({"capability_plan": payload}, diagnostic_sink=diagnostic), diagnostic)
        plan = normalize_capability_plan(payload, action="CHAT")
        self.assertTrue(plan.valid, plan.failure_reason)
        execution_plan = build_capability_execution_plan(plan, fallback_action="CHAT")
        cog = AIChatCog.__new__(AIChatCog)
        cog.bot = SimpleNamespace()
        cog._web_research = _RecordingWeb()
        cog._admin_actions = SimpleNamespace()
        await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), author=SimpleNamespace(id=3)),
            "",
            execution_plan,
            settings=SimpleNamespace(server_context=""),
        )
        return {req.query: req for req in cog._web_research.requests}

    async def test_dependent_image_task_enables_visual_request_for_its_web_task(self) -> None:
        requests = await self._run(
            [
                _requirement("r_web", "WEB", "web_1", "research", []),
                _requirement("r_img", "IMAGE_ANALYZE", "img_1", VISUAL_OBJECTIVE, ["r_web"]),
            ],
            [_web("web_1"), _image("img_1", "web_1")],
        )
        request = requests["query web_1"]

        self.assertTrue(request.enable_image_understanding)
        self.assertTrue(request.enable_image_search)
        self.assertIn(VISUAL_OBJECTIVE, request.visual_objective)
        self.assertEqual(request.visual_selection_count, 2)


if __name__ == "__main__":
    unittest.main()
