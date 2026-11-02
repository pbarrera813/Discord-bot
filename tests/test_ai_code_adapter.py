from __future__ import annotations


import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from services.glot import execute_run
from services.xai_client import XAIClient
from tests.test_ai_tagging import _async_return


def _code_route(operands: dict[str, object], *, task_id: str = "code_1") -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "run the supplied program",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "run the supplied program", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "run", "objective": "run the supplied program", "required": True,
                "satisfaction_criterion": "The program output is reported.",
                "capability_owner": "CODE", "owner_task_id": task_id, "depends_on": [],
            }],
            "capabilities": [{
                "task_id": task_id, "name": "CODE", "required": True,
                "evidence_span": "run the supplied program", "depends_on": [],
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


class _FakeGlot:
    def __init__(self, *results: object) -> None:
        self.calls: list[dict[str, object]] = []
        self.results = list(results) or [{"stdout": "hello\n", "stderr": "", "error": ""}]

    async def run_code(self, *, language, code, filename, stdin=None, command=None):
        self.calls.append({"language": language, "code": code, "filename": filename, "stdin": stdin, "command": command})
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class CodeCatalogTests(unittest.TestCase):

    def test_operands_are_typed_bounded_and_reject_foreign_fields(self) -> None:
        for operands in (
            {"code_action": "languages"},
            {"code_action": "run", "code_language": "python", "code": "print(1)"},
            {"code_action": "run", "code_language": "csharp", "code": "x" * 4000},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("CODE", operands))
        for operands, predicate in (
            ({"code_action": "shell"}, "task_operand_enum_invalid"),
            ({"code_action": "run", "code_language": "elixir", "code": "1"}, "task_operand_enum_invalid"),
            ({"code_action": "run", "code_language": "c#", "code": "1"}, "task_operand_enum_invalid"),
            ({"code_action": "run", "code_language": "python", "code": "x" * 12001}, "task_operand_type_or_bounds_invalid"),
            ({"code_action": "run", "code_language": "python", "code": 1}, "task_operand_type_or_bounds_invalid"),
            ({"code_action": "run", "code_language": "python", "code": "1", "command": "rm -rf /"}, "unknown_task_operand"),
            ({"code_action": "run", "code_language": "python", "code": "1", "stdin": "x"}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("CODE", operands), predicate)


class CodeSharedRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_language_runs_once_with_its_default_filename(self) -> None:
        client = _FakeGlot()
        outcome = await execute_run(client, "python", "print('hello')")
        self.assertEqual(outcome, {"stdout": "hello\n", "stderr": "", "error": ""})
        self.assertEqual(
            client.calls,
            [{"language": "python", "code": "print('hello')", "filename": "main.py", "stdin": None, "command": None}],
        )


    async def test_cpp_runs_once_with_the_runtimes_own_compiler_and_no_command(self) -> None:
        # The runner's clang image compiles and runs C++ itself; there is no g++ to ask for, so no second attempt exists.
        client = _FakeGlot({"stdout": "ok", "stderr": "", "error": ""})
        outcome = await execute_run(client, "cpp", "int main(){}")
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(client.calls[0]["command"])
        self.assertEqual(outcome, {"stdout": "ok", "stderr": "", "error": ""})

    async def test_a_cpp_failure_is_the_programs_result_and_is_not_retried(self) -> None:
        client = _FakeGlot({"stdout": "", "stderr": "main.cpp:1: error: expected ';'", "error": "Exit code: 1"})
        outcome = await execute_run(client, "cpp", "int main(){")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("expected ';'", outcome["stderr"])

    async def test_provider_error_propagates(self) -> None:
        for language in ("python", "cpp"):
            with self.subTest(language=language):
                client = _FakeGlot(RuntimeError("down"))
                with self.assertRaisesRegex(RuntimeError, "down"):
                    await execute_run(client, language, "1")
                self.assertEqual(len(client.calls), 1)


class CodeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, client: object | None) -> AIChatCog:
        db = SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))
        return AIChatCog(SimpleNamespace(
            user=SimpleNamespace(id=42, name="Nitori", display_name="Nitori"),
            application_id=1234, db=db, llm_client=None, glot_client=client,
        ))

    async def test_success_payload_is_typed_and_satisfies(self) -> None:
        client = _FakeGlot()
        result = await self._cog(client)._execute_code_result_only(
            {"code_action": "run", "code_language": "python", "code": "print('hello')"},
        )
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)
        self.assertEqual(
            result.payload,
            {"operation": "run", "language": "python", "stdout": "hello\n", "stderr": "", "error": ""},
        )
        self.assertEqual(dict(result.scope), {"operation": "run", "language": "python"})
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(client.calls[0]["stdin"])


    async def test_invalid_operands_never_reach_the_provider(self) -> None:
        client = _FakeGlot()
        for operands in (
            {"code_action": "run", "code_language": "elixir", "code": "1"},
            {"code_action": "run", "code": "1"},
            {"code_action": "run", "code_language": "python"},
            {"code_action": "run", "code_language": "python", "code": "   "},
            {"code_action": "shell"},
            {},
        ):
            with self.subTest(operands=operands):
                result = await self._cog(client)._execute_code_result_only(operands)
                self.assertEqual(result.status, CapabilityTerminalStatus.VALIDATION_FAILED)
                self.assertEqual(result.reason, "invalid_code_operand")
                self.assertFalse(result.satisfies_request)
        self.assertEqual(client.calls, [])



if __name__ == "__main__":
    unittest.main()
