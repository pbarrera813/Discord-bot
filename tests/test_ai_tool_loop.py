from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_operation_catalog import TOOL_DESCRIPTIONS, tool_definitions
from services.external_sources import ExternalSourceScope, GroundedExternalSource
from services.xai_client import TOOL_MAX_STEPS, XAIClient
from tests.test_ai_code_adapter import _FakeGlot
from tests.test_ai_tagging import (
    _CaptureChannel, _DummyGuild, _DummyMessage, _FakeResponse, _FakeSession, _FakeTTSClient, _async_return,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _response(response_id: str, *, calls: list[dict[str, object]] = (), text: str = "") -> _FakeResponse:
    output: list[dict[str, object]] = [
        {"type": "function_call", "call_id": call["call_id"], "name": call["name"], "arguments": json.dumps(call["args"])}
        for call in calls
    ]
    if text:
        output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    return _FakeResponse(status=200, text=json.dumps({"id": response_id, "status": "completed", "output": output}))


class _LoopClient(XAIClient):
    def __init__(self, session: _FakeSession) -> None:
        super().__init__("key", "grok-test")
        self.fake_session = session

    async def _get_session(self):
        return self.fake_session


async def _chat(client: XAIClient, run_tools) -> str:
    return await client.chat(
        server_context="", user_message="square 1..5 in python, run it, explain in French", author_name="Pablo",
        channel_name="general", tools=tool_definitions(), run_tools=run_tools,
    )


class ToolLoopTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_return_to_grok_and_the_final_text_is_exact(self) -> None:
        code = "for i in range(1, 6):\n    print(i * i)\n"
        final = "Résultat :\n```\n1\n4\n9\n16\n25\n```\n  trailing  "
        session = _FakeSession([
            _response("r1", calls=[{"call_id": "c1", "name": "code", "args": {"code_action": "run", "code_language": "python", "code": code, "purpose": "run"}}]),
            _response("r2", text=final),
        ])
        seen: list[list[dict[str, object]]] = []

        async def run_tools(calls):
            seen.append(calls)
            return [{"status": "SUCCESS", "stdout": "1\n4\n9\n16\n25\n"}]

        reply = await _chat(_LoopClient(session), run_tools)

        self.assertEqual(reply, final)
        self.assertEqual(json.loads(seen[0][0]["arguments"])["code"], code)
        first, second = session.calls[0]["json"], session.calls[1]["json"]
        self.assertIn("code", {tool["name"] for tool in first["tools"]})
        self.assertEqual(second["previous_response_id"], "r1")
        self.assertEqual(second["input"][0]["type"], "function_call_output")
        self.assertEqual(second["input"][0]["call_id"], "c1")
        self.assertEqual(json.loads(second["input"][0]["output"])["stdout"], "1\n4\n9\n16\n25\n")

    async def test_the_last_step_answers_without_functions(self) -> None:
        call = {"call_id": "c", "name": "web", "args": {"research_question": "q", "purpose": "p"}}
        session = _FakeSession([_response(f"r{i}", calls=[{**call, "call_id": f"c{i}"}]) for i in range(1, TOOL_MAX_STEPS)] + [_response("last", text="done")])

        async def run_tools(calls):
            return [{"status": "SUCCESS"} for _ in calls]

        self.assertEqual(await _chat(_LoopClient(session), run_tools), "done")
        self.assertEqual(len(session.calls), TOOL_MAX_STEPS)
        self.assertEqual(session.calls[-1]["json"]["tool_choice"], "none")
        self.assertEqual(session.calls[-2]["json"]["tool_choice"], "auto")

    def test_a_football_family_alone_selects_its_operation(self) -> None:
        # Production 2026-10-31: Grok sent operation_family=standings without data_focus and got operation=unknown.
        from services.football_query_service import compile_football_operation

        for family in ("standings", "fixture_next", "fixture_last", "team_profile", "top_scorers", "team_injuries", "team_squad", "fixture_live"):
            plan = {"semantic_source": "router", "operation_family": family, "team_candidates": ["Tigres UANL"]}
            self.assertEqual(compile_football_operation("FOOTBALL_LOOKUP", "", plan).operation_type, family)
        finer = {"semantic_source": "router", "operation_family": "competition_structure", "data_focus": "season_start"}
        self.assertEqual(compile_football_operation("FOOTBALL_LOOKUP", "", finer).data_focus, "season_start")

    def test_every_tool_has_its_own_fields_only(self) -> None:
        tools = {tool["name"]: tool for tool in tool_definitions()}
        self.assertEqual(set(tools), {family.lower() for family in TOOL_DESCRIPTIONS} | {"deliver", "stay_silent"})
        self.assertNotIn("memory_action", tools["code"]["parameters"]["properties"])
        self.assertIn("code", tools["code"]["parameters"]["properties"])
        self.assertNotIn("SERVER_MEMORY_CLARIFY", tools["memory"]["parameters"]["properties"]["memory_action"]["enum"])


class _ToolLLM(_FakeTTSClient):
    """Plays Grok: issues the scripted function calls through the real run_tools, then answers."""

    def __init__(self, steps: list[list[dict[str, object]]], answer: str = "Listo.") -> None:
        self.steps = steps
        self.answer = answer
        self.outputs: list[list[object]] = []
        self.vision_calls: list[dict[str, object]] = []
        self.generated: list[str] = []

    async def chat(self, **kwargs):
        run_tools = kwargs.get("run_tools")
        if run_tools is None:
            self.vision_calls.append(kwargs)
            return "A red bicycle beside a blue door."
        for step in self.steps:
            self.outputs.append(await run_tools([
                {"call_id": call["call_id"], "name": call["name"], "arguments": json.dumps(call["args"])} for call in step
            ]))
        return self.answer

    async def generate_image(self, prompt: str) -> bytes:
        self.generated.append(prompt)
        return PNG

    async def route_ai_interaction(self, **_kwargs):
        raise AssertionError("the router must not run")


class ToolTurnTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, llm: _ToolLLM, *, content: str = "<@42> hi", glot: object | None = None, **bot_extra):
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        bot = SimpleNamespace(
            user=bot_user, application_id=1234, db=db, llm_client=llm,
            is_owner_user=lambda _user: False, glot_client=glot, **bot_extra,
        )
        cog = AIChatCog(bot)
        message = _DummyMessage(
            attachments=[], author_id=99, guild_id=1, channel_id=10, content=content, mentions=[bot_user], message_id=8300,
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()
        await cog.on_message(message)
        return cog, message

    async def test_code_grok_wrote_runs_unchanged_and_the_answer_is_delivered(self) -> None:
        code = "for i in range(1, 6):\n    print(i * i)\n"
        glot = _FakeGlot({"stdout": "1\n4\n9\n16\n25\n", "stderr": "", "error": ""})
        llm = _ToolLLM([[{"call_id": "c1", "name": "code", "args": {
            "code_action": "run", "code_language": "python", "code": code, "purpose": "run the squares program",
        }}]], answer="Les carrés :\n```\n1\n4\n```")

        _cog, message = await self._run(llm, content="<@42> write and run squares, explain in French", glot=glot)

        self.assertEqual([call["code"] for call in glot.calls], [code])
        self.assertIn("SUCCESS", str(llm.outputs[0][0]))
        self.assertEqual([reply for reply, _mention in message.replies], ["Les carrés :\n```\n1\n4\n```"])

    async def test_bad_arguments_come_back_precisely_and_nothing_runs(self) -> None:
        glot = _FakeGlot()
        llm = _ToolLLM([[{"call_id": "c1", "name": "code", "args": {
            "code_action": "run", "code_language": "elixir", "code": "1", "purpose": "run",
        }}]])

        await self._run(llm, glot=glot)

        self.assertEqual(glot.calls, [])
        self.assertEqual(llm.outputs[0][0]["status"], "VALIDATION_FAILED")
        self.assertIn("code_language", llm.outputs[0][0]["reason"])

    async def test_a_generated_image_reaches_vision_from_its_own_bytes(self) -> None:
        llm = _ToolLLM([
            [{"call_id": "g1", "name": "image_generate", "args": {"prompt": "a red bicycle beside a blue door", "purpose": "draw"}}],
            [{"call_id": "v1", "name": "image_analyze", "args": {
                "selection_count": 1, "purpose": "describe what is actually present",
                "inputs": [{"task_id": "g1", "output_field": "artifact"}],
            }}],
        ])

        await self._run(llm, content="<@42> generate a red bicycle then inspect it")

        self.assertEqual(llm.generated, ["a red bicycle beside a blue door"])
        self.assertEqual(len(llm.vision_calls), 1)
        self.assertEqual([item.data for item in llm.vision_calls[0]["image_inputs"]], [PNG])
        self.assertIn("SUCCESS", str(llm.outputs[1][0]))
        # Production 2026-10-31: both tasks succeeded but reads had no review, so the chain read as unsatisfied.
        self.assertIn("Request satisfied: True", str(llm.outputs[1][0]))

    async def test_memory_lookup_dispatches_its_own_operation(self) -> None:
        llm = _ToolLLM([[{"call_id": "m1", "name": "memory", "args": {
            "memory_action": "SERVER_MEMORY_LOOKUP", "subject": "Pablo", "purpose": "what do we know about Pablo",
        }}]])
        seen: list[str] = []

        async def fake_memory(_cog, _message, decision, **_kwargs):
            seen.append(decision.action)
            return "FOUND"

        original = AIChatCog._execute_server_memory_action
        AIChatCog._execute_server_memory_action = fake_memory
        try:
            await self._run(llm, content="<@42> what do you remember about Pablo?")
        finally:
            AIChatCog._execute_server_memory_action = original

        self.assertEqual(seen, ["SERVER_MEMORY_LOOKUP"])

    async def test_only_the_selected_link_is_read(self) -> None:
        first, second = "https://docs.python.org/3/library/json.html", "https://docs.python.org/3/library/pathlib.html"
        retrieved: list[tuple[str, ...]] = []

        class _Sources:
            max_sources = 3

            def ground_sources(self, **_kwargs):
                return [
                    GroundedExternalSource(index=i, original_url=url, source_scope=ExternalSourceScope.CURRENT_MESSAGE, provenance="current_message")
                    for i, url in enumerate((first, second))
                ]

            async def retrieve_bundle(self, *, sources, **_kwargs):
                retrieved.append(tuple(source.original_url for source in sources))
                raise RuntimeError("stop after selection")

        llm = _ToolLLM([[{"call_id": "s1", "name": "external_source", "args": {
            "url": second, "source_scope": "CURRENT_MESSAGE", "expected_content": "TEXT", "operation": "SUMMARIZE",
            "purpose": "summarize the second link",
        }}]])
        cog_holder: dict[str, AIChatCog] = {}
        original_init = AIChatCog.__init__

        def init(cog, bot):
            original_init(cog, bot)
            cog._external_sources = _Sources()
            cog_holder["cog"] = cog

        AIChatCog.__init__ = init
        try:
            await self._run(llm, content=f"<@42> Read only the second URL and summarize it: {first} {second}")
        finally:
            AIChatCog.__init__ = original_init

        self.assertEqual(retrieved, [(second,)])

    async def test_stay_silent_sends_nothing(self) -> None:
        llm = _ToolLLM([[{"call_id": "q1", "name": "stay_silent", "args": {}}]], answer="")

        _cog, message = await self._run(llm, content="<@42> lol they were talking about Nitori")

        self.assertEqual(message.replies, [])
        self.assertEqual(message.channel.sent, [])


if __name__ == "__main__":
    unittest.main()
