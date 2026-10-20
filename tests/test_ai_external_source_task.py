from __future__ import annotations

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from services.xai_client import XAIClient
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _FakeExternalSourceService, _async_return

URL = "https://domain-a.test/a"


def _external_route(operands: dict[str, object], evidence: str = URL) -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "read the linked page",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "read the linked page", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "source", "objective": "read the linked page", "required": True,
                "satisfaction_criterion": "The page text is retrieved.",
                "capability_owner": "EXTERNAL_SOURCE", "owner_task_id": "external_1", "depends_on": [],
            }],
            "capabilities": [{
                "task_id": "external_1", "name": "EXTERNAL_SOURCE", "required": True,
                "evidence_span": evidence, "depends_on": [],
                "scope": "EXPLICIT_SOURCE", "independent": True, "independent_research": False,
                "owner_action": None, "operands": operands, "input_bindings": [],
                "source_refs": [], "requested_outputs": ["results"],
            }],
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    return XAIClient("test-key", "test-model")._validate_route_decision(
        "", has_supported_image=False, parsed=route,
    )


def _operands(**overrides: object) -> dict[str, object]:
    base = {
        "source_scope": "CURRENT_MESSAGE", "expected_content": "TEXT",
        "operation": "RETRIEVE",
    }
    base.update(overrides)
    return base


class ExternalSourceOperandContractTests(unittest.TestCase):
    def test_evidence_and_task_owned_flags_are_not_operands(self) -> None:
        self.assertIsNone(validate_operands("EXTERNAL_SOURCE", _operands()))
        for key in ("required", "explicit", "evidence_span", "bogus"):
            with self.subTest(key=key):
                self.assertEqual(
                    validate_operands("EXTERNAL_SOURCE", _operands(**{key: True})), "unknown_task_operand"
                )


class NativeExternalSourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, operands: dict[str, object], evidence: str = URL):
        route = _external_route(operands, evidence)
        self.assertTrue(route["valid"], route.get("failure_reason"))
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        external = _FakeExternalSourceService()

        class _LLM:
            async def route_ai_interaction(self, **_kwargs):
                return route

            async def chat(self, **_kwargs):
                return "Here is the page."

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
            user=bot_user, application_id=1234, db=db, llm_client=_LLM(),
            external_source_service=external, is_owner_user=lambda _user: False,
        ))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message = _DummyMessage(
            attachments=[], author_id=99, guild_id=1, channel_id=10,
            content=f"<@42> read {URL}", mentions=[bot_user], message_id=8300,
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        await cog.on_message(message)
        result = contexts[-1].bundle.by_task["external_1"] if contexts else None
        return external, result

    async def test_valid_task_retrieves_exactly_once(self) -> None:
        external, result = await self._run(_operands())

        self.assertEqual(len(external.retrieve_calls), 1)
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)

    async def test_evidence_outside_authorized_message_never_retrieves(self) -> None:
        external, result = await self._run(_operands(), "https://elsewhere.test/x")

        self.assertEqual(external.retrieve_calls, [])
        self.assertEqual(result.status, CapabilityTerminalStatus.VALIDATION_FAILED)
        self.assertFalse(result.satisfies_request)


if __name__ == "__main__":
    unittest.main()
