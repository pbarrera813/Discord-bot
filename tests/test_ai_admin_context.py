from __future__ import annotations

import unittest
from types import SimpleNamespace


from cogs.admin import AdminCog
from cogs.ai_chat import AIChatCog
from services.admin_actions import AdminActionService
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from services.database import AI_INTERACTIONS_CONTEXT_CHANNEL_ID
from tests.test_ai_admin_config import _Db, _decision
from tests.test_ai_admin_config import _world as _config_world
from tests.test_ai_admin_extras import _bot, _Channel, _CommandCtx, _Guild, _plan, _staff, _task

NEW_ACTIONS = ("server_context_view", "server_context_reset", "server_context_learn", "help")
NEW_MUTATIONS = ("server_context_reset", "server_context_learn")
OPERANDS = {
    "server_context_view": {},
    "server_context_reset": {"context_scope": "summaries"},
    "server_context_learn": {"target_channel": "quiet"},
    "help": {},
}
GUILD_ADMIN = {"manage_guild": True}
READER = {"manage_guild": True, "view_channel": True, "read_message_history": True}
BOT_READ = {"view_channel": True, "read_message_history": True}


def _world(*, actor_perms: dict[str, bool], bot_perms: dict[str, bool] | None = None, **kwargs: object):
    return _config_world(actor_perms=actor_perms, bot_perms=BOT_READ if bot_perms is None else bot_perms, **kwargs)
SCOPE_EFFECTS = {
    "all": [("reset_ai_server_context", 1), ("clear_server_memories", 1)],
    "summaries": [("reset_server_context_summaries", 1)],
    "memory": [("clear_server_memories", 1)],
    "ai_history": [("reset_ai_conversation_turns", 1)],
}
LEARNED = ("upsert_server_context_entry", {"guild_id": 1, "channel_id": 200, "channel_name": "quiet", "summary": "Learned summary.", "max_entries": 2})
VIEW_READS = ["get_guild_settings", "get_server_context_entries", "count_server_memories_by_status"]


class _CtxDb(_Db):
    def __init__(self, *, server_context: str = "", entries: list[dict[str, object]] | None = None,
                 counts: list[dict[str, object]] | None = None, language: str = "en") -> None:
        super().__init__()
        self.server_context, self.entries, self.counts, self.language = server_context, list(entries or []), list(counts or []), language

    async def get_guild_settings(self, guild_id: int) -> object:
        self.reads.append("get_guild_settings")
        return SimpleNamespace(prefix="!", language_code=self.language, server_context=self.server_context)

    async def get_server_context_entries(self, guild_id: int) -> list[dict[str, object]]:
        self.reads.append("get_server_context_entries")
        return [dict(entry) for entry in self.entries]

    async def count_server_memories_by_status(self, guild_id: int) -> list[dict[str, object]]:
        self.reads.append("count_server_memories_by_status")
        return [dict(row) for row in self.counts]

    async def reset_ai_server_context(self, guild_id: int) -> None:
        self.calls.append(("reset_ai_server_context", guild_id))

    async def clear_server_memories(self, guild_id: int) -> None:
        self.calls.append(("clear_server_memories", guild_id))

    async def reset_server_context_summaries(self, guild_id: int) -> None:
        self.calls.append(("reset_server_context_summaries", guild_id))

    async def reset_ai_conversation_turns(self, guild_id: int) -> None:
        self.calls.append(("reset_ai_conversation_turns", guild_id))

    async def upsert_server_context_entry(self, **kwargs: object) -> None:
        self.calls.append(("upsert_server_context_entry", kwargs))


class _History:
    def __init__(self) -> None:
        self.cleared: list[int] = []

    def clear_guild_history(self, guild_id: int) -> None:
        self.cleared.append(guild_id)


class _Env:
    def __init__(self, db: _CtxDb | None = None, *, summary: str | None = "Learned summary.", raises: Exception | None = None,
                 owner: bool = False, admin_cog: bool = True) -> None:
        self.db, self.history, self.summaries = db or _CtxDb(), _History(), []
        cogs: dict[str, object] = {"AIChatCog": self.history}
        self.bot = SimpleNamespace(db=self.db, is_owner_user=lambda _user: owner, get_cog=cogs.get, llm_client=None)
        self.admin = object.__new__(AdminCog)
        self.admin.bot = self.bot
        if admin_cog:
            cogs["AdminCog"] = self.admin

        async def summarize(*, channel: object, lang: str) -> str | None:
            self.summaries.append((channel.id, lang))
            if raises is not None:
                raise raises
            return summary

        self.admin._summarize_channel_context = summarize
        self.ai = AIChatCog.__new__(AIChatCog)
        self.ai.bot = self.bot
        self.ai._admin_actions = AdminActionService(self.bot)

    def untouched(self) -> tuple[list[object], list[str], list[object], list[int]]:
        return self.db.writes(), self.db.reads, self.summaries, self.history.cleared

    async def run(self, message, *tasks):
        return await self.ai._execute_live_media_composition_result_only(
            message, "", _plan(*tasks), settings=SimpleNamespace(server_context=""),
        )

    async def execute(self, message, action: str, *, native: bool = True, **operands: object):
        return await self.ai._execute_admin_action(
            message, _decision(action, **operands), lang="en", replied_message=None, reply_to_trigger=False, result_only=True, native_task=native,
        )


class CommandParityTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, *, language: str = "en", in_guild: bool = True, **env_kwargs: object):
        env = _Env(_CtxDb(language=language), **env_kwargs)
        actor, channel = _staff(manage_guild=True), _Channel(200, "quiet")
        guild = _Guild(_bot(), actor, channels=(channel,))
        return env, _CommandCtx(guild if in_guild else None, actor), channel

    async def test_every_command_refuses_outside_a_server_without_touching_anything(self) -> None:
        for name, args in (
            ("view_server_context", ()), ("reset_server_context", ("all",)), ("server_context_reset", ("all",)),
            ("set_server_context", ("channel",)),
        ):
            with self.subTest(command=name):
                env, ctx, channel = self._setup(in_guild=False)
                args = tuple(channel if arg == "channel" else arg for arg in args)
                await getattr(AdminCog, name).callback(env.admin, ctx, *args)
                self.assertEqual(ctx.texts(), ["This command can only be used in a server."])
                self.assertEqual(env.untouched(), ([], [], [], []))

    async def test_view_command_formats_what_is_stored(self) -> None:
        env, ctx, _channel = self._setup()
        env.db.server_context = "Tone: relaxed"
        env.db.entries = [
            {"channel_id": 10, "channel_name": "general", "summary": "SECRET-SUMMARY", "updated_at": "2026-01-01T00:00:00"},
            {"channel_id": AI_INTERACTIONS_CONTEXT_CHANNEL_ID, "channel_name": "ai", "summary": "x", "updated_at": "2026-01-02T00:00:00"},
        ]
        env.db.counts = [{"memory_type": "SERVER_RULE", "status": "active", "count": 2}]
        await AdminCog.view_server_context.callback(env.admin, ctx)
        self.assertEqual(ctx.texts(), [
            "AI server context currently stored:\n\nStructured memory:\n- SERVER_RULE / active: 2\n\nSources:\n"
            "- #general (10), updated 2026-01-01T00:00:00\n"
            f"- AI interactions ({AI_INTERACTIONS_CONTEXT_CHANNEL_ID}), updated 2026-01-02T00:00:00\n\nContext:\nTone: relaxed"
        ])
        self.assertEqual((env.db.reads, env.db.calls), (VIEW_READS, []))


    async def test_reset_commands_clear_exactly_the_scope(self) -> None:
        for command in (AdminCog.reset_server_context, AdminCog.server_context_reset):
            for scope, calls in SCOPE_EFFECTS.items():
                with self.subTest(command=command.name, scope=scope):
                    env, ctx, _channel = self._setup()
                    await command.callback(env.admin, ctx, f" {scope.upper()} ")
                    self.assertEqual(ctx.texts(), [f"AI server context reset complete for scope `{scope}`."])
                    self.assertEqual(env.db.calls, calls)
                    self.assertEqual(env.history.cleared, [1] if scope in {"all", "ai_history"} else [])


    async def test_learn_command_stores_the_summary_and_confirms(self) -> None:
        for language, text in (
            ("en", "I now understand how the people in the server behave, I will talk like you from now!"),
            ("es", "Ahora entiendo cómo se comporta la gente en el servidor, ¡hablaré como ustedes de ahora en adelante!"),
        ):
            with self.subTest(language=language):
                env, ctx, channel = self._setup(language=language)
                await AdminCog.set_server_context.callback(env.admin, ctx, channel)
                self.assertEqual(ctx.texts(), [text])
                self.assertEqual((env.summaries, env.db.calls), ([(200, language)], [LEARNED]))


class ContextCatalogTests(unittest.TestCase):
    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            {"admin_action": "server_context_view"},
            {"admin_action": "server_context_reset", "context_scope": "summaries"},
            {"admin_action": "server_context_reset", "context_scope": "memory"},
            {"admin_action": "server_context_reset", "context_scope": "ai_history"},
            {"admin_action": "server_context_reset", "context_scope": "all"},
            {"admin_action": "server_context_learn", "target_channel": "general"},
            {"admin_action": "help"},
            {"admin_action": "help", "help_section": "admin"},
            {"admin_action": "help", "help_section": "x" * 40},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("ADMIN", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        bounds = "task_operand_type_or_bounds_invalid"
        for operands, predicate in (
            ({"admin_action": "server_context_reset", "context_scope": "everything"}, "task_operand_enum_invalid"),
            ({"admin_action": "server_context_reset", "context_scope": "ALL"}, "task_operand_enum_invalid"),
            ({"admin_action": "server_context_reset", "context_scope": " all"}, "task_operand_enum_invalid"),
            ({"admin_action": "server_context_reset", "context_scope": 1}, bounds),
            ({"admin_action": "help", "help_section": "x" * 41}, bounds),
            ({"admin_action": "help", "help_section": 5}, bounds),
            ({"admin_action": "server_context_view", "context_scope": ["all"]}, bounds),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("ADMIN", operands), predicate)


class ContextTypedAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_mutations_run_once(self) -> None:
        for action in NEW_MUTATIONS:
            with self.subTest(action=action):
                env = _Env()
                message, *_rest = _world(actor_perms=READER)
                context = await env.run(message, _task("one", action, **OPERANDS[action]))
                result = context.results["one"]
                self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)
                self.assertTrue(result.satisfies_request)
                if action == "server_context_reset":
                    self.assertEqual((env.db.calls, env.history.cleared, env.summaries), ([("reset_server_context_summaries", 1)], [], []))
                else:
                    self.assertEqual((env.db.writes(), env.summaries, env.history.cleared), ([LEARNED], [(200, "en")], []))


    async def test_unauthorized_mutation_is_blocked_by_preflight_with_no_effect(self) -> None:
        for action in NEW_MUTATIONS:
            for name, perms in (
                ("no permission", {}),
                ("manage_channels only", {"manage_channels": True}),
                ("other moderation permissions", {"manage_messages": True, "manage_nicknames": True, "ban_members": True, "view_channel": True, "read_message_history": True}),
            ):
                with self.subTest(action=action, actor=name):
                    env = _Env()
                    message, *_rest = _world(actor_perms=perms)
                    context = await env.run(message, _task("one", action, **OPERANDS[action]))
                    self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.NOT_EXECUTED)
                    self.assertEqual(context.results["one"].reason, "mutation_batch_preflight_failed")
                    self.assertEqual(env.untouched(), ([], [], [], []))


    async def test_help_is_open_to_everyone_but_filtered_by_the_authors_own_permissions(self) -> None:
        for perms, count in (({}, 9), ({"manage_messages": True}, 10), (GUILD_ADMIN, 13)):
            with self.subTest(perms=perms):
                env = _Env()
                message, *_rest = _world(actor_perms=perms)
                context = await env.run(message, _task("one", "help"))
                self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.SUCCESS, context.results["one"].reason)
                self.assertEqual(len(context.results["one"].payload["sections"]), count)
                self.assertEqual(env.untouched(), ([], [], [], []))


    async def test_authority_is_rechecked_at_execution_without_preflight(self) -> None:
        for action in NEW_MUTATIONS:
            with self.subTest(action=action):
                env = _Env()
                message, *_rest = _world(actor_perms={"manage_channels": True, "view_channel": True, "read_message_history": True})
                result = await env.execute(message, action, **OPERANDS[action])
                self.assertNotEqual(result.status, CapabilityTerminalStatus.SUCCESS)
                self.assertFalse(result.satisfies_request)
                self.assertEqual((result.payload["outcome_code"], env.untouched()), ("author_missing_manage_guild", ([], [], [], [])))
                env = _Env()
                message, *_rest = _world(actor_perms=READER)
                allowed = await env.execute(message, action, **OPERANDS[action])
                self.assertEqual(allowed.status, CapabilityTerminalStatus.SUCCESS, allowed.reason)
        env = _Env()
        message, *_rest = _world(actor_perms={"manage_channels": True})
        result = await env.execute(message, "server_context_view")
        self.assertEqual((result.payload["outcome_code"], env.untouched()), ("author_missing_manage_guild", ([], [], [], [])))


class ContextTypedBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_task_blocks_an_independent_mutation(self) -> None:
        for bad in (
            _task("bad", "server_context_reset"),
            _task("bad", "server_context_reset", context_scope="everything"),
            _task("bad", "server_context_learn"),
            _task("bad", "server_context_learn", target_channel="missing-channel"),
        ):
            with self.subTest(bad=bad.operands):
                env = _Env()
                message, *_rest = _world(actor_perms=READER)
                good = _task("ok", "server_context_reset", context_scope="all") if bad.operands["admin_action"] == "server_context_learn" \
                    else _task("ok", "server_context_learn", target_channel="quiet")
                context = await env.run(message, bad, good)
                for task_id in ("bad", "ok"):
                    self.assertEqual(context.results[task_id].status, CapabilityTerminalStatus.NOT_EXECUTED)
                self.assertEqual(env.untouched(), ([], [], [], []))


    async def test_a_valid_batch_executes_each_task_once(self) -> None:
        env = _Env()
        message, *_rest = _world(actor_perms=READER, bot_perms=BOT_READ)
        context = await env.run(
            message,
            _task("reset", "server_context_reset", context_scope="summaries"),
            _task("learn", "server_context_learn", target_channel="quiet"),
            _task("help", "help", help_section="basic"),
        )
        self.assertTrue(all(result.status == CapabilityTerminalStatus.SUCCESS for result in context.results.values()), context.results)
        self.assertEqual(env.db.writes(), [("reset_server_context_summaries", 1), LEARNED])
        self.assertEqual(env.summaries, [(200, "en")])


if __name__ == "__main__":
    unittest.main()
