from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import known_output_fields, operation_effect, validate_operands
from services.xai_client import XAIClient
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return

REPLIED_TEXT = "hola a todos, esto es una prueba de traduccion"


def _translate_route(operands: dict[str, object], *, task_id: str = "translate_1") -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "translate the text",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "translate the text", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "translation", "objective": "translate the text", "required": True,
                "satisfaction_criterion": "The translation is reported.",
                "capability_owner": "TRANSLATE", "owner_task_id": task_id, "depends_on": [],
            }],
            "capabilities": [{
                "task_id": task_id, "name": "TRANSLATE", "required": True,
                "evidence_span": "translate the text", "depends_on": [],
                "scope": "GENERAL_REASONING", "independent": True, "independent_research": False,
                "owner_action": None, "operands": operands, "input_bindings": [],
                "source_refs": [], "requested_outputs": ["result"],
            }],
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    return XAIClient("test-key", "test-model")._validate_route_decision(
        "", has_supported_image=False, parsed=route,
    )


class TranslateCatalogTests(unittest.TestCase):
    def test_family_is_read_only_and_operands_are_typed(self) -> None:
        self.assertEqual(known_output_fields("TRANSLATE"), {"text", "result"})
        self.assertEqual(operation_effect(SimpleNamespace(name="TRANSLATE", operands={})), "read")
        for operands in (
            {"text": "hello", "target_language": "German"},
            {"text_source": "REPLIED_MESSAGE", "target_language": "Japanese"},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("TRANSLATE", operands))
        for operands, predicate in (
            ({"text": "x" * 1801, "target_language": "German"}, "task_operand_type_or_bounds_invalid"),
            ({"target_language": "L" * 41}, "task_operand_type_or_bounds_invalid"),
            ({"text_source": "ANY_MESSAGE"}, "task_operand_enum_invalid"),
            ({"text": 5}, "task_operand_type_or_bounds_invalid"),
            ({"address": "example.test"}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("TRANSLATE", operands), predicate)


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        route: dict[str, object],
        *,
        content: str,
        reply_to: object | None = None,
        translate_fails: bool = False,
        translated: str | None = None,
    ):
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            def __init__(self) -> None:
                self.route_calls = 0
                self.translate_calls: list[tuple[str, str]] = []
                self.chat_calls: list[dict[str, object]] = []

            async def route_ai_interaction(self, **_kwargs):
                self.route_calls += 1
                return route

            async def translate(self, *, text: str, target_language: str) -> str:
                self.translate_calls.append((text, target_language))
                if translate_fails:
                    raise RuntimeError("translation down")
                return translated if translated is not None else f"[{target_language}] {text}"

            async def chat(self, **kwargs):
                self.chat_calls.append(kwargs)
                return "Here is the translation."

        llm = _LLM()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(
                SimpleNamespace(prefix="!", language_code="en", server_context="")
            ),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user, application_id=1234, db=db, llm_client=llm,
            is_owner_user=lambda _user: False,
        ))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        channel = _CaptureChannel()
        guild = _DummyGuild()
        reference = None
        if reply_to is not None:
            channel.history_messages.append(reply_to)
            reference = SimpleNamespace(message_id=reply_to.id, resolved=None)
        message = _DummyMessage(
            attachments=[], author_id=99, guild_id=1, channel_id=10,
            content=f"<@42> {content}", mentions=[bot_user], message_id=8200, reference=reference,
        )
        message.guild = guild
        message.channel = channel
        await cog.on_message(message)
        result = contexts[-1].bundle.by_task["translate_1"] if contexts else None
        return llm, message, result


def _human_message(*, content: str = REPLIED_TEXT, bot: bool = False, message_id: int = 700):
    return _DummyMessage(
        attachments=[], author_id=55, author_bot=bot, guild_id=1, channel_id=10,
        content=content, message_id=message_id,
    )


class TypedTranslateLifecycleTests(_Harness):
    async def test_replied_message_is_translated_from_structural_source(self) -> None:
        route = _translate_route({"text_source": "REPLIED_MESSAGE", "target_language": "German"})
        self.assertTrue(route["valid"], route.get("failure_reason"))

        llm, message, result = await self._run(route, content="please do that", reply_to=_human_message())

        self.assertEqual(llm.translate_calls, [(REPLIED_TEXT, "German")])
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)
        self.assertEqual(result.payload["text"], f"[German] {REPLIED_TEXT}")
        self.assertEqual(result.payload["target_language"], "German")
        self.assertEqual(len(llm.chat_calls), 1)
        self.assertIn(f"[German] {REPLIED_TEXT}", str(llm.chat_calls[0]["trusted_capability_context"]))
        self.assertEqual([reply for reply, _mention in message.replies], ["Here is the translation."])


    async def test_structural_operand_failures_never_reach_the_provider(self) -> None:
        cases = (
            ("both_sources", {"text": "hi", "text_source": "REPLIED_MESSAGE", "target_language": "German"},
             _human_message(), "translate_text_source_invalid"),
            ("no_source", {"target_language": "German"}, None, "translate_text_source_invalid"),
            ("no_target", {"text": "hi"}, None, "missing_translate_operand"),
            ("blank_target", {"text": "hi", "target_language": "   "}, None, "missing_translate_operand"),
            ("no_reply", {"text_source": "REPLIED_MESSAGE", "target_language": "German"}, None,
             "reply_source_unavailable"),
            ("bot_author", {"text_source": "REPLIED_MESSAGE", "target_language": "German"},
             _human_message(bot=True), "reply_source_unavailable"),
            ("empty_reply", {"text_source": "REPLIED_MESSAGE", "target_language": "German"},
             _human_message(content="   "), "reply_source_unavailable"),
        )
        for name, operands, reply_to, reason in cases:
            with self.subTest(case=name):
                llm, _message, result = await self._run(
                    _translate_route(operands), content="do it", reply_to=reply_to,
                )
                self.assertEqual(llm.translate_calls, [])
                self.assertEqual(result.status, CapabilityTerminalStatus.VALIDATION_FAILED)
                self.assertEqual(result.reason, reason)
                self.assertFalse(result.satisfies_request)

    async def test_provider_error_is_typed_and_not_satisfied(self) -> None:
        route = _translate_route({"text": "hi", "target_language": "German"})

        llm, _message, result = await self._run(route, content="translate", translate_fails=True)

        self.assertEqual(len(llm.translate_calls), 1)
        self.assertEqual(result.status, CapabilityTerminalStatus.PROVIDER_FAILURE)
        self.assertEqual(result.reason, "RuntimeError")
        self.assertFalse(result.satisfies_request)


if __name__ == "__main__":
    unittest.main()
