from __future__ import annotations

import unittest

from services.memegen import MemeGenClient
from services.xai_client import XAIClient
from tests.test_ai_artifact_delivery import _artifact_lifecycle_cog

TASK_ID = "meme_1"
PNG = b"\x89PNG\r\n\x1a\nmeme-bytes"
SOURCE_URL = "https://cdn.example/current.png"
TEMPLATES = [{"id": "drake", "name": "Drake Hotline Bling"}, {"id": "fry", "name": "Futurama Fry"}]


class _FakeMemegen(MemeGenClient):
    def __init__(self, *, body: bytes = PNG, content_type: str = "image/png", fetch_error: Exception | None = None) -> None:
        super().__init__()
        self.body = body
        self.content_type = content_type
        self.fetch_error = fetch_error
        self.list_calls = 0
        self.fetch_urls: list[str] = []

    async def list_templates(self) -> list[dict[str, object]]:
        self.list_calls += 1
        return [dict(item) for item in TEMPLATES]

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        self.fetch_urls.append(url)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.body, self.content_type


def _meme_route(operands: dict[str, object], source_refs: list[str] | tuple[str, ...] = ()) -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": True, "source": "CURRENT_MESSAGE",
        "evidence_span": "make a meme", "semantic_reason": "meme artifact request",
        "failure_reason": None, "ambiguous_reason": None,
        "artifacts": [{"upstream_task_id": TASK_ID, "output_field": "artifact"}],
    }
    capability = {
        "task_id": TASK_ID, "name": "MEME", "required": True, "evidence_span": "make a meme",
        "depends_on": [], "scope": "IMAGE_GENERATION", "independent": True, "independent_research": False,
        "owner_action": None, "operands": operands, "input_bindings": [],
        "source_refs": list(source_refs), "requested_outputs": ["artifact"],
    }
    requirement = {
        "requirement_id": "meme_delivery", "objective": "make a meme", "required": True,
        "satisfaction_criterion": "The meme image is delivered.", "capability_owner": "MEME",
        "owner_task_id": TASK_ID, "depends_on": [],
    }
    return {
        "participation": "RESPOND", "action": "CHAT", "participation_confidence": 1.0,
        "action_confidence": 1.0, "reason_code": "DIRECT_REQUEST", "resolved_request": "make a meme",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "make a meme", "semantic_source": "router", "semantic_status": "SUCCESS",
            "primary_action": "CHAT", "requirements": [requirement], "capabilities": [capability],
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }


def _validated(route: dict[str, object]) -> dict[str, object]:
    return XAIClient("test-key", "test-model")._validate_route_decision("", has_supported_image=False, parsed=route)


def _bound_caption_route(meme_operands: dict[str, object], *, binding_field: str = "answer") -> dict[str, object]:
    base = _meme_route(meme_operands)
    plan = base["capability_plan"]
    meme = plan["capabilities"][0]
    meme["depends_on"] = ["caption_1"]
    meme["independent"] = False
    meme["input_bindings"] = [{"upstream_task_id": "caption_1", "output_field": binding_field}]
    chat = {
        "task_id": "caption_1", "name": "CHAT", "required": True, "evidence_span": "meme about it",
        "depends_on": [], "scope": "GENERAL_REASONING", "independent": True, "independent_research": False,
        "owner_action": None, "operands": {}, "input_bindings": [], "source_refs": [],
        "requested_outputs": ["answer"],
    }
    plan["capabilities"] = [chat, meme]
    plan["requirements"] = [
        {
            "requirement_id": "caption_req", "objective": "write a short caption", "required": True,
            "satisfaction_criterion": "a caption exists", "capability_owner": "CHAT",
            "owner_task_id": "caption_1", "depends_on": [],
        },
        {**plan["requirements"][0], "depends_on": ["caption_req"]},
    ]
    return base


def _translate_meme_route(*, via_chat: bool, direct_field: str = "text") -> dict[str, object]:
    base = _meme_route({"meme_action": "random", "meme_text_slot": "top"})
    plan = base["capability_plan"]
    translate = {
        "task_id": "translate1", "name": "TRANSLATE", "required": True, "evidence_span": "traduce buenos dias",
        "depends_on": [], "scope": "GENERAL_REASONING", "independent": False, "independent_research": False,
        "owner_action": None, "operands": {"text": "buenos dias", "target_language": "english"},
        "input_bindings": [], "source_refs": [], "requested_outputs": ["result" if via_chat else direct_field],
    }
    chat = {
        "task_id": "chat_caption", "name": "CHAT", "required": True, "evidence_span": "haz un meme con esa traduccion",
        "depends_on": ["translate1"], "scope": "GENERAL_REASONING", "independent": False, "independent_research": False,
        "owner_action": None, "operands": {},
        "input_bindings": [{"upstream_task_id": "translate1", "output_field": "result"}],
        "source_refs": [], "requested_outputs": ["answer"],
    }
    meme = plan["capabilities"][0]
    meme["independent"] = False
    meme["depends_on"] = ["chat_caption" if via_chat else "translate1"]
    meme["input_bindings"] = [{"upstream_task_id": "chat_caption" if via_chat else "translate1",
                               "output_field": "answer" if via_chat else direct_field}]
    plan["capabilities"] = [translate, chat, meme] if via_chat else [translate, meme]
    base_req = plan["requirements"][0]
    reqs = [{"requirement_id": "req_translate", "objective": "translate", "required": True,
             "satisfaction_criterion": "translation", "capability_owner": "TRANSLATE",
             "owner_task_id": "translate1", "depends_on": []}]
    previous = "req_translate"
    if via_chat:
        reqs.append({"requirement_id": "req_caption", "objective": "write a short top caption from the translation",
                     "required": True, "satisfaction_criterion": "caption", "capability_owner": "CHAT",
                     "owner_task_id": "chat_caption", "depends_on": [previous]})
        previous = "req_caption"
    reqs.append({**base_req, "depends_on": [previous]})
    plan["requirements"] = reqs
    return base



if __name__ == "__main__":
    unittest.main()


class MemeCaptionFromTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, via_chat: bool, direct_field: str = "text"):
        cog, message, llm = _artifact_lifecycle_cog({})
        memegen = _FakeMemegen()
        cog.bot.memegen_client = memegen
        chat_calls: list[dict[str, object]] = []

        async def translate(**_kwargs):
            return "Good morning"

        async def chat(**kwargs):
            chat_calls.append(kwargs)
            return "Good morning" if via_chat and len(chat_calls) == 1 else "listo"

        async def route_ai_interaction(**_kwargs):
            return _validated(_translate_meme_route(via_chat=via_chat, direct_field=direct_field))

        llm.translate, llm.chat, llm.route_ai_interaction = translate, chat, route_ai_interaction
        await cog.on_message(message)
        return message, memegen, chat_calls



    async def test_a_bound_result_without_text_is_a_typed_failure(self) -> None:
        from services.ai_capability_orchestrator import normalize_terminal_result

        cog, _message, _llm = _artifact_lifecycle_cog({})
        cog.bot.memegen_client = memegen = _FakeMemegen()
        football = normalize_terminal_result("FOOTBALL", "SUCCESS", provider="x", payload={"fixtures": []}, satisfies_request=True)
        result = await cog._execute_meme_result_only(
            {"meme_action": "random", "meme_text_slot": "top", "result": football}, None,
        )
        self.assertEqual(result.reason, "invalid_meme_text_binding")
        self.assertEqual(memegen.fetch_urls, [])
