from __future__ import annotations

import unittest
from dataclasses import replace

from cogs.ai_chat import MAX_RESULT_IMAGE_ARTIFACT_BYTES
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.memegen import MemeGenClient
from services.xai_client import XAIClient
from tests.test_ai_artifact_delivery import _LifecycleAttachment, _artifact_lifecycle_cog, _multi_image_route

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


class MemeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        operands: dict[str, object],
        *,
        memegen: object,
        refs=None,
        attach: bool = False,
        tamper=None,
    ):
        cog, message, llm = _artifact_lifecycle_cog({})
        if attach:
            message.attachments = [_LifecycleAttachment(
                attachment_id=100, filename="source.png", size=4, url=SOURCE_URL,
            )]
        cog.bot.memegen_client = memegen
        if tamper is not None:
            honest = cog._image_artifact_terminal_result
            cog._image_artifact_terminal_result = lambda *args, **kwargs: tamper(honest(*args, **kwargs))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)

        async def route_ai_interaction(**kwargs):
            sources = kwargs["image_metadata"].get("image_sources", [])
            source_refs = list(refs(sources)) if refs is not None else []
            return _validated(_meme_route(operands, source_refs))

        llm.route_ai_interaction = route_ai_interaction
        await cog.on_message(message)
        result = contexts[-1].bundle.by_task[TASK_ID] if contexts else None
        return message, result

    @staticmethod
    def _delivered(message) -> list[tuple[str, bytes]]:
        return [item for send in message.channel.sends for item in send["files"]]

    async def _assert_success(self, operands, *, expected_url: str, refs=None, attach=False):
        memegen = _FakeMemegen()
        message, result = await self._run(operands, memegen=memegen, refs=refs, attach=attach)
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)
        self.assertTrue(result.satisfies_request)
        self.assertEqual(result.payload["artifact"], PNG)
        self.assertEqual(result.payload["content_type"], "image/png")
        self.assertEqual(memegen.fetch_urls, [expected_url])
        delivered = self._delivered(message)
        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0][0].endswith(".png"))
        self.assertEqual(delivered[0][1], PNG)
        return memegen

    async def _assert_failure(self, operands, *, reason: str | None, status=None, memegen=None, refs=None, attach=False):
        memegen = memegen if memegen is not None else _FakeMemegen()
        message, result = await self._run(operands, memegen=memegen, refs=refs, attach=attach)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertFalse(result.satisfies_request)
        if reason is not None:
            self.assertEqual(result.reason, reason)
        if status is not None:
            self.assertEqual(result.status, status)
        self.assertEqual(memegen.fetch_urls, [])
        self.assertEqual(self._delivered(message), [])
        return result

    async def test_template_by_exact_id_fetches_once_and_delivers_one_png(self) -> None:
        operands = {"meme_action": "template", "meme_template": "drake", "meme_top": "hello", "meme_bottom": "world"}
        expected = MemeGenClient().build_template_url(template_id="drake", top_text="hello", bottom_text="world")
        memegen = await self._assert_success(operands, expected_url=expected)
        self.assertEqual(memegen.list_calls, 1)


    async def test_template_miss_is_typed_and_never_fetches(self) -> None:
        operands = {"meme_action": "template", "meme_template": "Unknown Meme", "meme_top": "hello"}
        await self._assert_failure(operands, reason="meme_template_not_found", status=CapabilityTerminalStatus.VALIDATION_FAILED)


    async def test_a_forged_meme_result_is_never_delivered(self) -> None:
        forgeries = {
            "image provider": lambda result: replace(result, provider="xai_image"),
            "image provenance source": lambda result: replace(result, provenance={**result.provenance, "source": "generate_image"}),
            "unknown execution path": lambda result: replace(result, provenance={**result.provenance, "execution_path": "memegen_other"}),
        }
        operands = {"meme_action": "template", "meme_template": "drake", "meme_top": "hello"}
        for name, tamper in forgeries.items():
            with self.subTest(name):
                message, _result = await self._run(operands, memegen=_FakeMemegen(), tamper=tamper)
                self.assertEqual(self._delivered(message), [])

    async def test_an_image_generation_result_cannot_borrow_memegen_trust(self) -> None:
        task = {
            "task_id": "image_a", "name": "IMAGE_GENERATE", "required": True,
            "evidence_span": "generate image", "depends_on": [], "scope": "IMAGE_GENERATION",
            "independent": True, "independent_research": False, "owner_action": None,
            "operands": {"prompt": "image prompt"}, "input_bindings": [], "source_refs": [],
            "requested_outputs": ["artifact"],
        }
        route = _multi_image_route(tasks=[task], artifacts=[{"upstream_task_id": "image_a", "output_field": "artifact"}])
        cog, message, _llm = _artifact_lifecycle_cog(route)

        async def borrowed(*_args, **_kwargs):
            return cog._image_artifact_terminal_result(
                "IMAGE_GENERATE", PNG, source="memegen", provider="memegen", filename="nitori_meme.png",
                provenance={"execution_path": "memegen_template"},
            )

        cog._execute_image_generation_result_only = borrowed
        await cog.on_message(message)
        self.assertEqual(message.channel.sends[0]["files"], [])

    async def test_random_picks_from_the_provider_list_and_delivers(self) -> None:
        operands = {"meme_action": "random", "meme_top": "hello"}
        memegen = _FakeMemegen()
        message, result = await self._run(operands, memegen=memegen)
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)
        self.assertEqual(len(memegen.fetch_urls), 1)
        self.assertTrue(any(f"/images/{item['id']}/" in memegen.fetch_urls[0] for item in TEMPLATES))
        self.assertEqual(len(self._delivered(message)), 1)

    async def test_custom_background_is_the_exact_authorized_ref_url(self) -> None:
        operands = {"meme_action": "custom", "meme_top": "top", "meme_bottom": "bottom"}
        expected = MemeGenClient().build_custom_url(background_url=SOURCE_URL, top_text="top", bottom_text="bottom")
        await self._assert_success(
            operands, expected_url=expected, attach=True,
            refs=lambda sources: [sources[0]["ref"]],
        )


    def test_a_non_null_operand_of_another_capability_is_still_refused(self) -> None:
        for leaked in ({"prompt": "texto gracioso"}, {"query": "memes de lunes"}):
            with self.subTest(leaked):
                route = _meme_route({"meme_action": "random", "meme_top": "hello", **leaked})
                self.assertFalse(_validated(route)["valid"])


    async def test_download_problems_are_typed_and_nothing_is_attached(self) -> None:
        operands = {"meme_action": "template", "meme_template": "drake", "meme_top": "hello"}
        cases = (
            ("not_png", _FakeMemegen(content_type="image/jpeg"), "meme_not_png"),
            ("fetch_error", _FakeMemegen(fetch_error=RuntimeError("down")), "RuntimeError"),
            ("oversize", _FakeMemegen(body=b"x" * (MAX_RESULT_IMAGE_ARTIFACT_BYTES + 1)), "invalid_image_artifact"),
        )
        for name, memegen, reason in cases:
            with self.subTest(case=name):
                message, result = await self._run(operands, memegen=memegen)
                self.assertEqual(result.status, CapabilityTerminalStatus.PROVIDER_FAILURE)
                self.assertEqual(result.reason, reason)
                self.assertFalse(result.satisfies_request)
                self.assertEqual(len(memegen.fetch_urls), 1)
                self.assertEqual(self._delivered(message), [])


if __name__ == "__main__":
    unittest.main()

class MemeBoundCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, meme_operands: dict[str, object], *, caption: str = "Cuando el lunes te gana", binding_field: str = "answer"):
        cog, message, llm = _artifact_lifecycle_cog({})
        memegen = _FakeMemegen()
        cog.bot.memegen_client = memegen
        calls: list[dict[str, object]] = []

        async def chat(**kwargs):
            calls.append(kwargs)
            return caption if len(calls) == 1 else "listo"

        llm.chat = chat
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)

        async def route_ai_interaction(**_kwargs):
            return _validated(_bound_caption_route(meme_operands, binding_field=binding_field))

        llm.route_ai_interaction = route_ai_interaction
        await cog.on_message(message)
        by_task = contexts[-1].bundle.by_task if contexts else {}
        return message, memegen, by_task


    async def test_only_text_outputs_may_feed_a_caption(self) -> None:
        route = _bound_caption_route({"meme_action": "random", "meme_text_slot": "top"}, binding_field="artifact")
        self.assertFalse(_validated(route)["valid"])

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


    async def test_the_translation_binds_through_its_result_output_as_xai_chose_in_production(self) -> None:
        self.assertTrue(_validated(_translate_meme_route(via_chat=False, direct_field="result"))["valid"])
        message, memegen, calls = await self._run(via_chat=False, direct_field="result")
        self.assertIn("/Good_morning/", memegen.fetch_urls[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([f for send in message.channel.sends for f in send["files"]]), 1)

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

    def test_a_free_standing_non_independent_chat_next_to_a_specialized_task_is_still_refused(self) -> None:
        route = _translate_meme_route(via_chat=True)
        plan = route["capability_plan"]
        for task in plan["capabilities"]:
            if task["name"] == "CHAT":
                task["input_bindings"] = []
        plan["capabilities"][2]["input_bindings"] = [{"upstream_task_id": "translate1", "output_field": "text"}]
        plan["capabilities"][2]["depends_on"] = ["translate1", "chat_caption"]
        self.assertFalse(_validated(route)["valid"])
