from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from services.api_ninjas import execute_operation
from services.xai_client import XAIClient
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return


def _ninjas_route(operands: dict[str, object], *, task_id: str = "ninjas_1") -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "provider-backed lookup",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "provider-backed lookup", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "lookup", "objective": "provider-backed lookup", "required": True,
                "satisfaction_criterion": "The provider result is reported.",
                "capability_owner": "NINJAS", "owner_task_id": task_id, "depends_on": [],
            }],
            "capabilities": [{
                "task_id": task_id, "name": "NINJAS", "required": True,
                "evidence_span": "provider-backed lookup", "depends_on": [],
                "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False,
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


class _FakeNinjas:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail = fail

    async def _record(self, name: str, *args: object):
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("provider down")

    async def get_joke(self) -> str:
        await self._record("joke")
        return "synthetic joke"

    async def get_dadjoke(self) -> str:
        await self._record("dadjoke")
        return "synthetic dad joke"

    async def get_advice(self) -> str:
        await self._record("advice")
        return "synthetic advice"

    async def whois(self, domain: str) -> dict[str, object]:
        await self._record("whois", domain)
        return {
            "registrar": "Synthetic Registrar", "creation_date": "2001-01-01",
            "expiration_date": "2031-01-01", "country": "XX",
            "name_servers": [f"ns{i}.example.test" for i in range(20)],
        }

    async def unit_conversion(self, amount: float, unit: str):
        await self._record("unit_conversion", amount, unit)
        return {"kilometer": 1.5, "mile": 0.9321}


class NinjasCatalogTests(unittest.TestCase):

    def test_operands_are_typed_bounded_and_reject_foreign_fields(self) -> None:
        valid = (
            {"ninjas_action": "joke"},
            {"ninjas_action": "whois", "domain": "example.com"},
            {"ninjas_action": "unit_conversion", "amount": 1.5, "from_unit": "kilometer", "to_unit": "mile"},
            {"ninjas_action": "unit_conversion", "amount": 3, "from_unit": "meter", "to_unit": "foot"},
        )
        for operands in valid:
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("NINJAS", operands))
        invalid = (
            ({"ninjas_action": "joke", "address": "example.test"}, "unknown_task_operand"),
            ({"ninjas_action": "unit_conversion", "amount": "1.5"}, "task_operand_type_or_bounds_invalid"),
            ({"ninjas_action": "unit_conversion", "amount": True}, "task_operand_type_or_bounds_invalid"),
            ({"ninjas_action": "unit_conversion", "amount": math.nan}, "task_operand_type_or_bounds_invalid"),
            ({"ninjas_action": "unit_conversion", "amount": math.inf}, "task_operand_type_or_bounds_invalid"),
            ({"ninjas_action": "whois", "domain": "d" * 300}, "task_operand_type_or_bounds_invalid"),
            ({"ninjas_action": "not_an_operation"}, "task_operand_enum_invalid"),
        )
        for operands, predicate in invalid:
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("NINJAS", operands), predicate)


class NinjasSharedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_whois_epoch_dates_are_returned_as_readable_dates(self) -> None:
        class _EpochNinjas(_FakeNinjas):
            async def whois(self, domain: str) -> dict[str, object]:
                return {"registrar": "R", "creation_date": 808372800, "expiration_date": [1818129600], "name_servers": []}

        whois = await execute_operation(_EpochNinjas(), "whois", domain="example.com")
        self.assertEqual((whois["creation_date"], whois["expiration_date"]), ("1995-08-14", ["2027-08-13"]))

    async def test_every_operation_returns_a_bounded_typed_payload(self) -> None:
        client = _FakeNinjas()
        self.assertEqual(await execute_operation(client, "joke"), {"operation": "joke", "text": "synthetic joke"})
        self.assertEqual(await execute_operation(client, "dadjoke"), {"operation": "dadjoke", "text": "synthetic dad joke"})
        self.assertEqual(await execute_operation(client, "advice"), {"operation": "advice", "text": "synthetic advice"})
        whois = await execute_operation(client, "whois", domain="example.com")
        self.assertEqual(whois["registrar"], "Synthetic Registrar")
        self.assertEqual(whois["domain"], "example.com")
        self.assertEqual(len(whois["name_servers"]), 12)
        conversion = await execute_operation(
            client, "unit_conversion", amount=1.0, from_unit="meter", to_unit="kilometer",
        )
        self.assertEqual(conversion["converted_value"], 1.5)
        self.assertEqual(conversion["to_unit"], "kilometer")
        self.assertEqual(client.calls[-1], ("unit_conversion", (1.0, "meter")))

    async def test_missing_operands_and_unknown_operation_never_reach_the_provider(self) -> None:
        client = _FakeNinjas()
        for operation, kwargs in (
            ("whois", {}),
            ("unit_conversion", {"amount": 1.0, "from_unit": "meter"}),
            ("unit_conversion", {"from_unit": "meter", "to_unit": "foot"}),
            ("run_shell", {}),
        ):
            with self.subTest(operation=operation, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await execute_operation(client, operation, **kwargs)
        self.assertEqual(client.calls, [])


class NinjasCommandTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, client: object):
        from cogs.ninjas import NinjasCog

        db = SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))
        return NinjasCog(SimpleNamespace(db=db, ninjas_client=client))

    @staticmethod
    def _ctx():
        sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def send(*args, **kwargs):
            sent.append((args, kwargs))

        return SimpleNamespace(guild=None, interaction=None, send=send), sent

    async def test_text_commands_send_provider_text_or_the_failure(self) -> None:
        from cogs.ninjas import NinjasCog

        ok_ctx, ok_sent = self._ctx()
        client = _FakeNinjas()
        cog = self._cog(client)
        await NinjasCog.joke.callback(cog, ok_ctx)
        await NinjasCog.dadjoke.callback(cog, ok_ctx)
        await NinjasCog.advice.callback(cog, ok_ctx)
        self.assertEqual(
            [args[0] for args, _ in ok_sent], ["synthetic joke", "synthetic dad joke", "synthetic advice"],
        )

        bad_ctx, bad_sent = self._ctx()
        await NinjasCog.joke.callback(self._cog(_FakeNinjas(fail=True)), bad_ctx)
        self.assertEqual(bad_sent[0][0][0], "Failed to get joke: provider down")

    async def test_whois_embed_uses_shared_payload_and_blank_domain_skips_provider(self) -> None:
        from cogs.ninjas import NinjasCog

        client = _FakeNinjas()
        cog = self._cog(client)
        ctx, sent = self._ctx()
        await NinjasCog.whois.callback(cog, ctx, "  ")
        self.assertEqual(sent[0][0][0], "Please provide a domain.")
        self.assertEqual(client.calls, [])

        ctx, sent = self._ctx()
        await NinjasCog.whois.callback(cog, ctx, "example.com")
        embed = sent[0][1]["embed"]
        self.assertEqual(embed.title, "WHOIS: example.com")
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Registrar"], "Synthetic Registrar")
        self.assertEqual(values["Country"], "XX")
        self.assertEqual(len(values["Name Servers"].split(", ")), 12)

    async def test_convert_reports_result_or_available_units(self) -> None:
        from cogs.ninjas import NinjasCog

        client = _FakeNinjas()
        cog = self._cog(client)
        ctx, sent = self._ctx()
        await NinjasCog.convert.callback(cog, ctx, 1.0, "meter", to_unit="kilometer")
        self.assertEqual(sent[0][0][0], "Conversion result: `1 meter = 1.5 kilometer`")
        self.assertEqual(client.calls[-1], ("unit_conversion", (1.0, "meter")))

        ctx, sent = self._ctx()
        await NinjasCog.convert.callback(cog, ctx, 1.0, "meter", to_unit="km")
        self.assertEqual(sent[0][0][0], "Conversion result: `1 meter = 1.5 km`")

        ctx, sent = self._ctx()
        await NinjasCog.convert.callback(cog, ctx, 1.0, "meter", to_unit="parsec")
        self.assertIn("couldn't find that target unit", sent[0][0][0])
        self.assertIn("Available: `kilometer, mile`.", sent[0][0][0])


class NinjasLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, route: dict[str, object], *, ninjas: object | None):
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            def __init__(self) -> None:
                self.route_calls = 0
                self.chat_calls: list[dict[str, object]] = []

            async def route_ai_interaction(self, **_kwargs):
                self.route_calls += 1
                return route

            async def chat(self, **kwargs):
                self.chat_calls.append(kwargs)
                return "Here is the provider-backed answer."

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
        bot = SimpleNamespace(
            user=bot_user, application_id=1234, db=db, llm_client=llm,
            is_owner_user=lambda _user: False, ninjas_client=ninjas,
        )
        cog = AIChatCog(bot)
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message = _DummyMessage(
            attachments=[], author_id=99, guild_id=1, channel_id=10,
            content="<@42> do the lookup", mentions=[bot_user], message_id=8100,
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        await cog.on_message(message)
        return llm, message, contexts[-1].bundle.by_task["ninjas_1"] if contexts else None

    async def test_native_whois_task_executes_once_and_reaches_synthesis(self) -> None:
        route = _ninjas_route({"ninjas_action": "whois", "domain": "example.com"})
        self.assertTrue(route["valid"], route.get("failure_reason"))
        ninjas = _FakeNinjas()

        llm, message, result = await self._run(route, ninjas=ninjas)

        self.assertEqual(ninjas.calls, [("whois", ("example.com",))])
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)
        self.assertEqual(result.payload["operation"], "whois")
        self.assertEqual(result.payload["registrar"], "Synthetic Registrar")
        self.assertEqual(llm.route_calls, 1)
        self.assertEqual(len(llm.chat_calls), 1)
        trusted = str(llm.chat_calls[0]["trusted_capability_context"])
        self.assertIn("Synthetic Registrar", trusted)
        self.assertIn("example.com", trusted)
        self.assertEqual([reply for reply, _mention in message.replies], ["Here is the provider-backed answer."])


    async def test_provider_error_is_typed_and_not_satisfied(self) -> None:
        ninjas = _FakeNinjas(fail=True)

        _llm, _message, result = await self._run(_ninjas_route({"ninjas_action": "joke"}), ninjas=ninjas)

        self.assertEqual(len(ninjas.calls), 1)
        self.assertEqual(result.status, CapabilityTerminalStatus.PROVIDER_FAILURE)
        self.assertEqual(result.reason, "RuntimeError")
        self.assertFalse(result.satisfies_request)


if __name__ == "__main__":
    unittest.main()
