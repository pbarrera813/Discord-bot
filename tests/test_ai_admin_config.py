from __future__ import annotations

import unittest
from types import SimpleNamespace


from cogs.admin import AdminCog
from cogs.ai_chat import AIChatCog, RouteDecision
from services.admin_actions import AdminActionService
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from tests.test_ai_admin_extras import _bot, _Channel, _CommandCtx, _Guild, _ModlogCase, _plan, _route, _staff, _task
from tests.test_ai_tagging import _DummyMessage

CONFIG_ACTIONS = (
    "set_modlog", "set_prefix", "set_language", "set_antispam", "set_antilink",
    "ai_channel_add", "ai_channel_remove", "ai_channel_list", "ai_channel_clear",
)
CONFIG_MUTATIONS = tuple(action for action in CONFIG_ACTIONS if action != "ai_channel_list")
OPERANDS = {
    "set_modlog": {"target_channel": "quiet"},
    "set_prefix": {"guild_prefix": "?"},
    "set_language": {"guild_language": "es"},
    "set_antispam": {"filter_enabled": True},
    "set_antilink": {"filter_enabled": False},
    "ai_channel_add": {"target_channel": "quiet"},
    "ai_channel_remove": {"target_channel": "quiet"},
    "ai_channel_list": {},
    "ai_channel_clear": {},
}
EXPECTED_WRITE = {
    "set_modlog": ("set_modlog_channel", 1, 200),
    "set_prefix": ("set_prefix", 1, "?"),
    "set_language": ("set_language", 1, "es"),
    "set_antispam": ("set_anti_spam", 1, True),
    "set_antilink": ("set_anti_link", 1, False),
    "ai_channel_add": ("add_ai_channel", 1, 200),
    "ai_channel_remove": ("remove_ai_channel", 1, 200),
    "ai_channel_clear": ("clear_ai_channels", 1),
}
GUILD_ADMIN = {"manage_guild": True}


class _Db:
    def __init__(self, allowed: tuple[int, ...] = (), *, none: bool = False) -> None:
        self.allowed, self.none = sorted(allowed), none
        self.calls: list[tuple[object, ...]] = []
        self.reads: list[str] = []

    def writes(self) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] != "get_or_create_guild_settings"]

    async def get_or_create_guild_settings(self, guild_id: int) -> object:
        self.calls.append(("get_or_create_guild_settings", guild_id))
        return SimpleNamespace(prefix="!", language_code="en", server_context="")

    async def set_modlog_channel(self, guild_id: int, channel_id: int | None) -> None:
        self.calls.append(("set_modlog_channel", guild_id, channel_id))

    async def set_prefix(self, guild_id: int, prefix: str) -> None:
        self.calls.append(("set_prefix", guild_id, prefix))

    async def set_language(self, guild_id: int, language: str) -> None:
        self.calls.append(("set_language", guild_id, language))

    async def set_anti_spam(self, guild_id: int, enabled: bool) -> None:
        self.calls.append(("set_anti_spam", guild_id, enabled))

    async def set_anti_link(self, guild_id: int, enabled: bool) -> None:
        self.calls.append(("set_anti_link", guild_id, enabled))

    async def add_ai_channel(self, guild_id: int, channel_id: int) -> None:
        self.calls.append(("add_ai_channel", guild_id, channel_id))
        self.allowed, self.none = sorted({*self.allowed, channel_id}), False

    async def remove_ai_channel(self, guild_id: int, channel_id: int) -> None:
        self.calls.append(("remove_ai_channel", guild_id, channel_id))
        self.allowed = [item for item in self.allowed if item != channel_id]
        self.none = self.none or not self.allowed

    async def clear_ai_channels(self, guild_id: int) -> None:
        self.calls.append(("clear_ai_channels", guild_id))
        self.allowed, self.none = [], False

    async def get_ai_channel_scope(self, guild_id: int) -> tuple[str, list[int]]:
        self.reads.append("get_ai_channel_scope")
        if self.allowed:
            return "restricted", list(self.allowed)
        return ("none" if self.none else "all"), []

    async def is_ai_channel_allowed(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def get_ai_conversation_history(self, *_args: object, **_kwargs: object) -> list[object]:
        return []

    async def add_ai_conversation_turn(self, *_args: object, **_kwargs: object) -> None:
        return None


def _service(db: _Db | None = None, *, owner: bool = False) -> AdminActionService:
    return AdminActionService(SimpleNamespace(db=db, is_owner_user=lambda _user: owner))


def _cog(db: _Db) -> AIChatCog:
    cog = AIChatCog.__new__(AIChatCog)
    cog.bot = SimpleNamespace(db=db, is_owner_user=lambda _user: False)
    cog._admin_actions = AdminActionService(cog.bot)
    return cog


def _world(*, actor_perms: dict[str, bool], bot_perms: dict[str, bool] | None = None, guild_cls: type[_Guild] = _Guild):
    actor, me = _staff(**actor_perms), _bot(**(bot_perms or {}))
    quiet, here = _Channel(200, "quiet"), _Channel()
    guild = guild_cls(me, actor, channels=(quiet, here))
    message = _DummyMessage(attachments=[], author_id=99, guild_id=1, channel_id=10, content="<@42> do it", message_id=8500)
    message.guild, message.channel, message.author = guild, here, actor
    return message, guild, actor, quiet, here


def _decision(action: str, **operands: object) -> RouteDecision:
    return RouteDecision("RESPOND", "ADMIN_ACTION", 1.0, 1.0, "ADMIN_ACTION_REQUEST", admin={"admin_action": action, **operands})


async def _execute(cog: AIChatCog, message, decision: RouteDecision, *, native: bool = True):
    return await cog._execute_admin_action(
        message, decision, lang="en", replied_message=None, reply_to_trigger=False, result_only=True, native_task=native,
    )


async def _run(db: _Db, message, *tasks):
    return await _cog(db)._execute_live_media_composition_result_only(
        message, "", _plan(*tasks), settings=SimpleNamespace(server_context=""),
    )


def _admin_cog(db: _Db) -> AdminCog:
    cog = object.__new__(AdminCog)
    cog.bot = SimpleNamespace(db=db, is_owner_user=lambda _user: False)
    return cog


class CommandParityTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, *, allowed: tuple[int, ...] = (), none: bool = False, in_guild: bool = True):
        db = _Db(allowed, none=none)
        actor, channel = _staff(manage_guild=True), _Channel(10, "general")
        guild = _Guild(_bot(), actor, channels=(channel,))
        return _admin_cog(db), _CommandCtx(guild if in_guild else None, actor), db, channel

    async def test_every_command_refuses_outside_a_server_without_touching_the_database(self) -> None:
        for name, args in (
            ("set_modlog", ()), ("set_prefix", ("!",)), ("language", ("en",)), ("antispam", (True,)), ("antilink", (True,)),
            ("aichanneladd", ("channel",)), ("aichannelremove", ("channel",)), ("aichannellist", ()), ("aichannelclear", ()),
        ):
            with self.subTest(command=name):
                cog, ctx, db, channel = self._setup(in_guild=False)
                args = tuple(channel if arg == "channel" else arg for arg in args)
                await getattr(AdminCog, name).callback(cog, ctx, *args)
                self.assertEqual(ctx.texts(), ["This command can only be used in a server."])
                self.assertEqual((db.calls, db.reads), ([], []))

    async def test_setmodlog_sets_and_clears(self) -> None:
        cog, ctx, db, channel = self._setup()
        await AdminCog.set_modlog.callback(cog, ctx, channel)
        await AdminCog.set_modlog.callback(cog, ctx, None)
        self.assertEqual(ctx.texts(), ["Mod-log channel set to <#10>.", "Mod-log channel cleared."])
        self.assertEqual(db.calls, [
            ("get_or_create_guild_settings", 1), ("set_modlog_channel", 1, 10),
            ("get_or_create_guild_settings", 1), ("set_modlog_channel", 1, None),
        ])

    async def test_setprefix_validation_and_update(self) -> None:
        for prefix, text, calls in (
            ("  !!  ", "Prefix updated to `!!`.", [("get_or_create_guild_settings", 1), ("set_prefix", 1, "!!")]),
            ("abcde", "Prefix updated to `abcde`.", [("get_or_create_guild_settings", 1), ("set_prefix", 1, "abcde")]),
            ("   ", "Prefix cannot be empty.", []),
            ("abcdef", "Prefix is too long. Use up to 5 characters.", []),
        ):
            with self.subTest(prefix=prefix):
                cog, ctx, db, _channel = self._setup()
                await AdminCog.set_prefix.callback(cog, ctx, prefix)
                self.assertEqual((ctx.texts(), db.calls), ([text], calls))


    async def test_aichannel_add(self) -> None:
        cog, ctx, db, channel = self._setup()
        await AdminCog.aichanneladd.callback(cog, ctx, channel)
        self.assertEqual(ctx.texts(), ["AI channel added: <#10>."])
        self.assertEqual(db.calls, [("add_ai_channel", 1, 10)])


class ConfigCatalogTests(unittest.TestCase):
    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            {"admin_action": "set_modlog", "target_channel": "logs"},
            {"admin_action": "set_prefix", "guild_prefix": "?????"},
            {"admin_action": "set_language", "guild_language": "en"},
            {"admin_action": "set_language", "guild_language": "es"},
            {"admin_action": "set_antispam", "filter_enabled": True},
            {"admin_action": "set_antilink", "filter_enabled": False},
            {"admin_action": "ai_channel_add", "target_channel": "ai"},
            {"admin_action": "ai_channel_remove", "target_channel": "ai"},
            {"admin_action": "ai_channel_list"},
            {"admin_action": "ai_channel_clear"},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("ADMIN", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        bounds = "task_operand_type_or_bounds_invalid"
        for operands, predicate in (
            ({"admin_action": "set_prefix", "guild_prefix": "abcdef"}, bounds),
            ({"admin_action": "set_prefix", "guild_prefix": 5}, bounds),
            ({"admin_action": "set_language", "guild_language": "fr"}, "task_operand_enum_invalid"),
            ({"admin_action": "set_language", "guild_language": "EN"}, "task_operand_enum_invalid"),
            ({"admin_action": "set_language", "guild_language": "eng"}, bounds),
            ({"admin_action": "set_antispam", "filter_enabled": "true"}, bounds),
            ({"admin_action": "set_antispam", "filter_enabled": 1}, bounds),
            ({"admin_action": "set_antilink", "filter_enabled": 0}, bounds),
            ({"admin_action": "set_modlog", "target_channel": 5}, bounds),
            ({"admin_action": "ai_channel_clear", "filter_enabled": True, "prefix": "x"}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("ADMIN", operands), predicate)


class TypedAuthorityTests(_ModlogCase):
    async def test_authorized_mutations_run_once_and_write_no_modlog_entry(self) -> None:
        for action in CONFIG_MUTATIONS:
            with self.subTest(action=action):
                db = _Db((77, 200))
                message, *_rest = _world(actor_perms=GUILD_ADMIN)
                context = await _run(db, message, _task("one", action, **OPERANDS[action]))
                self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.SUCCESS, context.results["one"].reason)
                self.assertTrue(context.results["one"].satisfies_request)
                self.assertEqual((db.writes(), self.modlog.embeds), ([EXPECTED_WRITE[action]], []))


    async def test_unauthorized_mutation_is_blocked_by_preflight_with_no_effect(self) -> None:
        for action in CONFIG_MUTATIONS:
            for name, perms in (
                ("no permission", {}),
                ("manage_channels only", {"manage_channels": True}),
                ("other moderation permissions", {"manage_messages": True, "manage_nicknames": True, "ban_members": True}),
            ):
                with self.subTest(action=action, actor=name):
                    db = _Db((77, 200))
                    message, *_rest = _world(actor_perms=perms)
                    context = await _run(db, message, _task("one", action, **OPERANDS[action]))
                    self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.NOT_EXECUTED)
                    self.assertEqual(context.results["one"].reason, "mutation_batch_preflight_failed")
                    self.assertEqual((db.calls, db.reads), ([], []))


    async def test_authority_is_rechecked_at_execution_without_preflight(self) -> None:
        for action in CONFIG_ACTIONS:
            with self.subTest(action=action):
                db = _Db((77, 200))
                message, *_rest = _world(actor_perms={"manage_channels": True})
                result = await _execute(_cog(db), message, _decision(action, **OPERANDS[action]))
                self.assertNotEqual(result.status, CapabilityTerminalStatus.SUCCESS)
                self.assertFalse(result.satisfies_request)
                self.assertEqual((result.payload["outcome_code"], db.calls, db.reads), ("author_missing_manage_guild", [], []))
                db = _Db((77, 200))
                message, *_rest = _world(actor_perms=GUILD_ADMIN)
                allowed = await _execute(_cog(db), message, _decision(action, **OPERANDS[action]))
                self.assertEqual(allowed.status, CapabilityTerminalStatus.SUCCESS, allowed.reason)
                self.assertEqual(db.writes(), [EXPECTED_WRITE[action]] if action in EXPECTED_WRITE else [])


class TypedBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_config_task_blocks_an_independent_mutation(self) -> None:
        for bad in (
            _task("bad", "set_prefix", guild_prefix="abcdef"),
            _task("bad", "set_language", guild_language="fr"),
            _task("bad", "set_antispam"),
            _task("bad", "set_modlog", target_channel="missing-channel"),
        ):
            with self.subTest(bad=bad.operands):
                db = _Db((77, 200))
                message, *_rest = _world(actor_perms=GUILD_ADMIN)
                context = await _run(db, message, bad, _task("ok", "set_antilink", filter_enabled=True))
                for task_id in ("bad", "ok"):
                    self.assertEqual(context.results[task_id].status, CapabilityTerminalStatus.NOT_EXECUTED)
                self.assertEqual(db.calls, [])


    async def test_a_valid_batch_executes_each_task_once(self) -> None:
        db = _Db((77, 200))
        message, *_rest = _world(actor_perms=GUILD_ADMIN)
        context = await _run(
            db, message,
            _task("prefix", "set_prefix", **OPERANDS["set_prefix"]),
            _task("spam", "set_antispam", **OPERANDS["set_antispam"]),
            _task("add", "ai_channel_add", target_channel="quiet"),
        )
        self.assertTrue(all(result.status == CapabilityTerminalStatus.SUCCESS for result in context.results.values()), context.results)
        self.assertEqual(db.writes(), [("set_prefix", 1, "?"), ("set_anti_spam", 1, True), ("add_ai_channel", 1, 200)])


class TypedEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, action: str, *, staff: bool, operands: dict[str, object] | None = None):
        operands = OPERANDS[action] if operands is None else operands
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            def __init__(self) -> None:
                self.route_calls = 0
                self.chat_calls: list[dict[str, object]] = []

            async def route_ai_interaction(self, **_kwargs):
                self.route_calls += 1
                return _route(action, operands)

            async def chat(self, **kwargs):
                self.chat_calls.append(dict(kwargs))
                return "Done."

        llm, db = _LLM(), _Db((77, 200))
        cog = AIChatCog(SimpleNamespace(user=bot_user, application_id=1234, db=db, llm_client=llm, is_owner_user=lambda _user: False))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message, *_rest = _world(actor_perms=GUILD_ADMIN if staff else {})
        message.mentions = [bot_user]
        await cog.on_message(message)
        return llm, message, contexts[-1].bundle.by_task["admin_operation_1"] if contexts else None, db


if __name__ == "__main__":
    unittest.main()
