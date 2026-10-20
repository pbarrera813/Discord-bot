from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord

from cogs.ai_chat import AIChatCog
from cogs.moderation import COLOR_NUMBER_EMOJIS, DEFAULT_COLOR_ROLES, ModerationCog
from services.admin_actions import AdminActionService
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import validate_operands
from tests.test_ai_admin_config import _decision
from tests.test_ai_admin_extras import _bot, _Channel, _CommandCtx, _Guild, _ModlogCase, _plan, _Role, _staff, _task
from tests.test_ai_tagging import _DummyMessage

COLOR_ACTIONS = ("color_setup", "color_list", "color_publish", "color_reload", "color_add", "color_remove")
COLOR_MUTATIONS = tuple(action for action in COLOR_ACTIONS if action != "color_list")
OPERANDS = {
    "color_setup": {},
    "color_list": {},
    "color_publish": {"target_channel": "quiet"},
    "color_reload": {},
    "color_add": {"role_name": "Teal", "role_color_hex": "#1abc9c"},
    "color_remove": {"role_name": "Red"},
}
ADMIN = {"administrator": True}
ROLES_BOT = {"manage_roles": True}
PANEL_BOT = {"send_messages": True, "add_reactions": True, "embed_links": True, "attach_files": True, "read_message_history": True}
BOT_PERMS = {"color_setup": ROLES_BOT, "color_list": {}, "color_publish": PANEL_BOT, "color_reload": PANEL_BOT, "color_add": ROLES_BOT, "color_remove": ROLES_BOT}
NOT_ADMIN = (
    ("no permission", {}),
    ("manage_roles only", {"manage_roles": True}),
    ("manage_guild only", {"manage_guild": True}),
    ("other staff permissions", {"manage_roles": True, "manage_guild": True, "manage_channels": True, "manage_messages": True, "ban_members": True}),
)
SEEDED = (("Red", 500, "#FF0000"), ("Blue", 501, "#0000FF"))
TITLES = {
    "color_setup": "Color Roles Setup", "color_publish": "Color Panel Published", "color_reload": "Color Panel Reloaded",
    "color_add": "Color Role Added", "color_remove": "Color Role Removed",
}
EXPECTED_WRITES = {
    "color_setup": [("upsert_color_role", 1, 600 + index, name, hex_code.upper(), index + 1) for index, (name, hex_code) in enumerate(DEFAULT_COLOR_ROLES)],
    "color_list": [],
    "color_publish": [("upsert_color_panel", 1, 200, 7001)],
    "color_reload": [("upsert_color_panel", 1, 200, 7001)],
    "color_add": [("upsert_color_role", 1, 600, "Teal", "#1ABC9C", None)],
    "color_remove": [("delete_color_role_by_name", 1, "Red")],
}


class _ColorRole(_Role):
    managed = False

    def __init__(self, role_id: int, name: str, *, position: int = 2, color: int = 0, guild: object = None,
                 fail: Exception | None = None, edit_fail: Exception | None = None) -> None:
        super().__init__(role_id, position, name)
        self.color, self.guild, self.fail, self.edit_fail = SimpleNamespace(value=color), guild, fail, edit_fail
        self.edits: list[dict[str, object]] = []
        self.deleted: list[object] = []

    def __lt__(self, other: object) -> bool:
        return self.position < getattr(other, "position", -1)

    def __gt__(self, other: object) -> bool:
        return self.position > getattr(other, "position", -1)

    async def edit(self, **kwargs: object) -> None:
        if self.edit_fail is not None:
            raise self.edit_fail
        self.edits.append(kwargs)

    async def delete(self, *, reason: str | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.deleted.append(reason)
        self.guild.roles.remove(self)


class _PanelMessage:
    def __init__(self, message_id: int, *, react_fail: Exception | None = None) -> None:
        self.id, self.react_fail = message_id, react_fail
        self.reactions: list[str] = []
        self.deleted = False

    async def add_reaction(self, emoji: str) -> None:
        if self.react_fail is not None:
            raise self.react_fail
        self.reactions.append(emoji)

    async def delete(self) -> None:
        self.deleted = True


class _PanelChannel(_Channel):
    def __init__(self, channel_id: int = 200, name: str = "quiet", *, send_fail: Exception | None = None,
                 react_fail: Exception | None = None) -> None:
        super().__init__(channel_id, name)
        self.send_fail, self.react_fail = send_fail, react_fail
        self.posts: list[dict[str, object]] = []
        self.messages: dict[int, _PanelMessage] = {}
        self._next_post = 7000

    async def send(self, *args: object, **kwargs: object) -> _PanelMessage:
        if self.send_fail is not None:
            raise self.send_fail
        self._next_post += 1
        message = _PanelMessage(self._next_post, react_fail=self.react_fail)
        self.posts.append(dict(kwargs))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> _PanelMessage:
        if message_id in self.messages:
            return self.messages[message_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


class _ColorGuild(_Guild):
    def __init__(self, me: object, *members: object, channels: tuple[_Channel, ...] = (), owner_id: int = 999) -> None:
        super().__init__(me, *members, channels=channels, owner_id=owner_id)
        self.name = "Test Guild"
        self.created: list[dict[str, object]] = []
        self.create_fail: Exception | None = None
        self.create_fail_after = 0
        self.hidden = False
        self._next_role_id = 600

    def get_role(self, role_id: int):
        return None if self.hidden else next((role for role in self.roles if role.id == role_id), None)

    async def create_role(self, *, name: str, color: discord.Color, mentionable: bool, reason: str) -> _ColorRole:
        if self.create_fail is not None and len(self.created) >= self.create_fail_after:
            raise self.create_fail
        role = _ColorRole(self._next_role_id, name, color=color.value, guild=self)
        self._next_role_id += 1
        self.created.append({"name": name, "color": color.value, "mentionable": mentionable, "reason": reason})
        self.roles.append(role)
        return role

    async def create_text_channel(self, name: str, *, reason: str | None = None) -> _PanelChannel:
        channel = _PanelChannel(500, name)
        channel.guild = self
        self.text_channels.append(channel)
        return channel


class _ColorDb:
    def __init__(self) -> None:
        self.colors: list[dict[str, object]] = []
        self.panel: dict[str, object] | None = None
        self.calls: list[tuple[object, ...]] = []
        self.reads: list[str] = []

    def writes(self) -> list[tuple[object, ...]]:
        return self.calls

    async def get_or_create_guild_settings(self, guild_id: int) -> object:
        return SimpleNamespace(prefix="!", language_code="en", server_context="")

    async def is_ai_channel_allowed(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def get_ai_conversation_history(self, *_args: object, **_kwargs: object) -> list[object]:
        return []

    async def add_ai_conversation_turn(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def list_color_roles(self, guild_id: int) -> list[dict[str, object]]:
        self.reads.append("list_color_roles")
        return [dict(row) for row in self.colors]

    async def count_color_roles(self, guild_id: int) -> int:
        self.reads.append("count_color_roles")
        return len(self.colors)

    async def get_color_role_by_name(self, guild_id: int, color_name: str) -> dict[str, object] | None:
        self.reads.append("get_color_role_by_name")
        return next((dict(row) for row in self.colors if str(row["color_name"]).lower() == color_name.strip().lower()), None)

    async def upsert_color_role(self, guild_id: int, role_id: int, color_name: str, hex_code: str, *, display_order: int | None = None) -> None:
        self.calls.append(("upsert_color_role", guild_id, role_id, color_name, hex_code.strip().upper(), display_order))
        row = next((row for row in self.colors if str(row["color_name"]).lower() == color_name.lower()), None)
        if row is None:
            row = {"color_name": color_name, "display_order": display_order or len(self.colors) + 1}
            self.colors.append(row)
        row.update(role_id=role_id, hex_code=hex_code.strip().upper())

    async def delete_color_role_by_name(self, guild_id: int, color_name: str) -> None:
        self.calls.append(("delete_color_role_by_name", guild_id, color_name))
        self.colors = [row for row in self.colors if str(row["color_name"]).lower() != color_name.strip().lower()]

    async def delete_color_role_by_role_id(self, guild_id: int, role_id: int) -> bool:
        self.calls.append(("delete_color_role_by_role_id", guild_id, role_id))
        self.colors = [row for row in self.colors if row["role_id"] != role_id]
        return True

    async def get_color_panel(self, guild_id: int) -> dict[str, object] | None:
        self.reads.append("get_color_panel")
        return dict(self.panel) if self.panel is not None else None

    async def upsert_color_panel(self, guild_id: int, channel_id: int, message_id: int | None) -> None:
        self.calls.append(("upsert_color_panel", guild_id, channel_id, message_id))
        self.panel = {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id}

    async def clear_color_panel(self, guild_id: int) -> None:
        self.calls.append(("clear_color_panel", guild_id))
        self.panel = None


class _Env:
    def __init__(self, *, owner: bool = False, moderation_cog: bool = True) -> None:
        self.db = _ColorDb()
        cogs: dict[str, object] = {}
        self.bot = SimpleNamespace(db=self.db, is_owner_user=lambda _user: owner, get_cog=cogs.get, llm_client=None)
        self.mod = object.__new__(ModerationCog)
        self.mod.bot = self.bot
        self.mod.admin_actions = AdminActionService(self.bot)

        async def lang(_guild: object) -> str:
            return "en"

        async def defer(_ctx: object) -> None:
            return None

        self.mod._lang, self.mod._maybe_defer = lang, defer
        if moderation_cog:
            cogs["ModerationCog"] = self.mod
        self.ai = AIChatCog.__new__(AIChatCog)
        self.ai.bot = self.bot
        self.ai._admin_actions = AdminActionService(self.bot)

    async def run(self, message, *tasks):
        return await self.ai._execute_live_media_composition_result_only(
            message, "", _plan(*tasks), settings=SimpleNamespace(server_context=""),
        )

    async def execute(self, message, action: str, *, native: bool = True, **operands: object):
        return await self.ai._execute_admin_action(
            message, _decision(action, **operands), lang="en", replied_message=None, reply_to_trigger=False, result_only=True, native_task=native,
        )


def _seed(db: _ColorDb, guild: _ColorGuild, *rows: tuple[str, int, str], position: int = 2) -> list[_ColorRole]:
    roles = []
    for index, (name, role_id, hex_code) in enumerate(rows, start=1):
        role = _ColorRole(role_id, name, position=position, color=int(hex_code[1:], 16), guild=guild)
        guild.roles.append(role)
        db.colors.append({"role_id": role_id, "color_name": name, "hex_code": hex_code, "display_order": index})
        roles.append(role)
    return roles


def _scene(action: str | None = None, *, actor_perms: dict[str, bool] | None = None, bot_perms: dict[str, bool] | None = None,
           owner: bool = False, colors: tuple[tuple[str, int, str], ...] | None = None, panel: bool = True, **env_kwargs: object):
    env = _Env(owner=owner, **env_kwargs)
    actor = _staff(**(ADMIN if actor_perms is None else actor_perms))
    me = _bot(**(BOT_PERMS.get(action, {}) if bot_perms is None else bot_perms))
    quiet, here = _PanelChannel(200, "quiet"), _Channel()
    guild = _ColorGuild(me, actor, channels=(quiet, here))
    message = _DummyMessage(attachments=[], author_id=99, guild_id=1, channel_id=10, content="<@42> do it", message_id=8500)
    message.guild, message.channel, message.author = guild, here, actor
    if colors is None:
        colors = () if action in {"color_setup", "color_add"} else SEEDED
    _seed(env.db, guild, *colors)
    if panel and action in {"color_publish", "color_reload"}:
        env.db.panel = {"guild_id": 1, "channel_id": 200, "message_id": 7000}
        quiet.messages[7000] = _PanelMessage(7000)
    return env, message, guild, actor, quiet


def _untouched(env: _Env, guild: _ColorGuild, quiet: _PanelChannel) -> tuple[object, ...]:
    return env.db.calls, env.db.reads, guild.created, quiet.posts


def _roles(guild: _ColorGuild) -> list[tuple[int, str]]:
    return [(role.id, role.name) for role in guild.roles]


class _PatchedCase(_ModlogCase):
    """The panel code asks isinstance(channel, discord.TextChannel): the fixture channels stand in for it."""

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(discord, "TextChannel", _Channel)
        patcher.start()
        self.addCleanup(patcher.stop)


class ColorCommandParityTests(_PatchedCase):
    def _ctx(self, *, in_guild: bool = True, **scene: object):
        env, _message, guild, actor, quiet = _scene(**scene)
        return env, _CommandCtx(guild if in_guild else None, actor), guild, quiet

    async def test_every_command_refuses_outside_a_server_without_touching_anything(self) -> None:
        for name, args, kwargs in (
            ("colorsetup", (), {}), ("colors", (), {}), ("colorchannel", ("channel",), {}), ("colorreload", (), {}),
            ("coloradd", ("#1abc9c",), {"name": "Teal"}), ("colorremove", (), {"color_name": "Red"}),
        ):
            with self.subTest(command=name):
                env, ctx, _guild, quiet = self._ctx(in_guild=False)
                args = tuple(quiet if arg == "channel" else arg for arg in args)
                await getattr(ModerationCog, name).callback(env.mod, ctx, *args, **kwargs)
                self.assertEqual(ctx.texts(), ["This command can only be used in a server."])
                self.assertEqual((env.db.calls, env.db.reads, self.modlog.embeds), ([], [], []))

    async def test_setup_creates_the_default_roles_upserts_them_replies_with_the_panel_and_logs_once(self) -> None:
        env, ctx, guild, _quiet = self._ctx(bot_perms=ROLES_BOT, colors=())
        await ModerationCog.colorsetup.callback(env.mod, ctx)
        self.assertEqual(
            guild.created,
            [{"name": name, "color": int(hex_code[1:], 16), "mentionable": False, "reason": "Default color role setup by mod#0"} for name, hex_code in DEFAULT_COLOR_ROLES],
        )
        self.assertEqual(env.db.calls, EXPECTED_WRITES["color_setup"])
        (args, sent), = ctx.sent
        embed = sent["embed"]
        self.assertEqual((args, embed.title), ((), "✅ Color setup completed"))
        self.assertEqual(embed.description, "Current configured colors:\nPreview below. React with a number to select one color role.")
        self.assertEqual((sent["file"].filename, embed.image.url), ("color_panel_preview.png", "attachment://color_panel_preview.png"))
        self.assertEqual(len(self.modlog.embeds), 1)
        self.assertEqual(self.modlog.embeds[0].title, "Color Roles Setup")
        self.assertEqual(self.modlog.fields(), [
            ("Moderator", "<@99>"), ("Target", "Test Guild (1)"), ("Reason", "Default color role setup"), ("Details", "Configured colors: 9"),
        ])


    async def test_channel_publishes_the_panel_replaces_the_old_one_and_logs_once(self) -> None:
        env, ctx, _guild, quiet = self._ctx(action="color_publish")
        old = quiet.messages[7000]
        await ModerationCog.colorchannel.callback(env.mod, ctx, quiet)
        self.assertTrue(old.deleted)
        (post,) = quiet.posts
        self.assertEqual((post["embed"].title, post["file"].filename), ("Choose Your Name Color", "color_panel_preview.png"))
        self.assertEqual(quiet.messages[7001].reactions, COLOR_NUMBER_EMOJIS[:2])
        self.assertEqual(env.db.calls, [("upsert_color_panel", 1, 200, 7001)])
        self.assertEqual(ctx.texts(), ["✅ Color panel published in <#200>."])
        self.assertEqual(len(self.modlog.embeds), 1)
        self.assertEqual(self.modlog.embeds[0].title, "Color Panel Published")
        self.assertEqual(self.modlog.fields(), [
            ("Moderator", "<@99>"), ("Target", "<#200> (200)"), ("Reason", "Color panel publish/update"), ("Details", "Message ID: 7001"),
        ])


    async def test_add_creates_the_role_upserts_it_replies_and_logs_once(self) -> None:
        env, ctx, guild, _quiet = self._ctx(colors=())
        await ModerationCog.coloradd.callback(env.mod, ctx, " 1ABC9c ", name="  Teal ")
        self.assertEqual(guild.created, [{"name": "Teal", "color": 0x1ABC9C, "mentionable": False, "reason": "Color role created by mod#0"}])
        self.assertEqual(env.db.calls, [("upsert_color_role", 1, 600, "Teal", "#1ABC9C", None)])
        self.assertEqual(ctx.texts(), ["✅ Color role added: <@&600> (`#1ABC9C`). Run `/color reload` to refresh the public panel."])
        self.assertEqual(self.modlog.embeds[0].title, "Color Role Added")
        self.assertEqual(self.modlog.fields(), [
            ("Moderator", "<@99>"), ("Target", "Teal (600)"), ("Reason", "Manual color role add"), ("Details", "Hex: #1ABC9C"),
        ])
        self.assertEqual(len(self.modlog.embeds), 1)


    async def test_remove_deletes_the_role_and_the_entry_replies_and_logs_once(self) -> None:
        env, ctx, guild, _quiet = self._ctx()
        red = guild.get_role(500)
        await ModerationCog.colorremove.callback(env.mod, ctx, color_name=" red ")
        self.assertEqual((red.deleted, _roles(guild)), (["Color role removed by mod#0"], [(501, "Blue")]))
        self.assertEqual(env.db.calls, [("delete_color_role_by_name", 1, "Red")])
        self.assertEqual(ctx.texts(), ["✅ Color role `Red` removed. Run `/color reload` to refresh the public panel."])
        self.assertEqual(self.modlog.embeds[0].title, "Color Role Removed")
        self.assertEqual(self.modlog.fields(), [("Moderator", "<@99>"), ("Target", "Red (500)"), ("Reason", "Manual color role removal")])
        self.assertEqual(len(self.modlog.embeds), 1)


class ColorCatalogTests(unittest.TestCase):

    def test_invalid_operands_are_rejected(self) -> None:
        bounds = "task_operand_type_or_bounds_invalid"
        for operands, predicate in (
            ({"admin_action": "color_add", "role_color_hex": "#1abc9c00"}, bounds),
            ({"admin_action": "color_add", "role_name": 5}, bounds),
            ({"admin_action": "color_publish", "target_channel": 5}, bounds),
            ({"admin_action": "color_setup", "color_name": "Red"}, "unknown_task_operand"),
            ({"admin_action": "color_remove", "hex": "#fff000"}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("ADMIN", operands), predicate)


class ColorMethodTests(_PatchedCase):
    async def _call(self, env: _Env, name: str, guild, actor, quiet, **kwargs: object):
        return await {
            "color_setup": lambda: env.mod.color_setup(guild, actor, **kwargs),
            "color_list": lambda: env.mod.color_list(guild, actor, **kwargs),
            "color_publish": lambda: env.mod.publish_color_panel(guild, actor, quiet, **kwargs),
            "color_reload": lambda: env.mod.publish_color_panel(guild, actor, None, reload=True, **kwargs),
            "color_add": lambda: env.mod.color_add(guild, actor, "#1abc9c", "Teal", **kwargs),
            "color_remove": lambda: env.mod.color_remove(guild, actor, "Red", **kwargs),
        }[name]()


    async def test_strict_remove_respects_the_role_hierarchy_and_managed_roles(self) -> None:
        for name, changes, code in (
            ("above the bot", {"position": 40}, "bot_role_hierarchy"),
            ("managed", {"managed": True}, "role_is_managed"),
        ):
            with self.subTest(case=name):
                self.modlog.embeds.clear()
                env, _message, guild, actor, quiet = _scene("color_remove")
                red = guild.get_role(500)
                for key, value in changes.items():
                    setattr(red, key, value)
                result = await env.mod.color_remove(guild, actor, "Red", strict=True)
                self.assertEqual((result.success, result.code), (False, code))
                self.assertEqual((env.db.calls, red.deleted, self.modlog.embeds), ([], [], []))
                result = await env.mod.color_remove(guild, actor, "Red")
                self.assertTrue(result.success)
                self.assertEqual(red.deleted, ["Color role removed by mod#0"])

    async def test_strict_setup_refuses_to_adopt_a_role_the_actor_or_the_bot_cannot_manage(self) -> None:
        for name, position, code in (("above the actor", 20, "actor_role_hierarchy"), ("above the bot", 40, "bot_role_hierarchy")):
            with self.subTest(case=name):
                self.modlog.embeds.clear()
                env, message, guild, actor, quiet = _scene("color_setup")
                red = _ColorRole(900, "red", position=position, color=0x123456, guild=guild)
                guild.roles.append(red)
                result = await env.mod.color_setup(guild, actor, strict=True)
                self.assertEqual((result.success, result.code), (False, code))
                self.assertEqual((env.db.calls, guild.created, red.edits, self.modlog.embeds), ([], [], [], []))
                typed = await env.execute(message, "color_setup")
                self.assertNotEqual(typed.status, CapabilityTerminalStatus.SUCCESS)
                self.assertEqual((typed.payload["outcome_code"], env.db.calls, guild.created, red.edits), (code, [], [], []))
        env, _message, guild, actor, quiet = _scene("color_setup")
        guild.roles.append(_ColorRole(900, "red", position=2, color=0x123456, guild=guild))
        self.assertTrue((await env.mod.color_setup(guild, actor, strict=True)).success)
        env, _message, guild, actor, quiet = _scene("color_setup")
        red = _ColorRole(900, "red", position=20, color=0x123456, guild=guild)
        guild.roles.append(red)
        self.assertTrue((await env.mod.color_setup(guild, actor)).success)
        self.assertEqual(len(red.edits), 1)


class ColorAuthorityTests(_PatchedCase):
    async def test_authorized_mutations_run_once_with_their_effect_and_one_modlog_entry(self) -> None:
        for action in COLOR_MUTATIONS:
            with self.subTest(action=action):
                self.modlog.embeds.clear()
                env, message, _guild, _actor, _quiet = _scene(action)
                context = await env.run(message, _task("one", action, **OPERANDS[action]))
                result = context.results["one"]
                self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)
                self.assertTrue(result.satisfies_request)
                self.assertEqual((env.db.writes(), [embed.title for embed in self.modlog.embeds]), (EXPECTED_WRITES[action], [TITLES[action]]))


    async def test_a_user_without_administrator_is_blocked_by_preflight_with_no_effect(self) -> None:
        for action in COLOR_MUTATIONS:
            for name, perms in NOT_ADMIN:
                with self.subTest(action=action, actor=name):
                    env, message, guild, _actor, quiet = _scene(action, actor_perms=perms)
                    context = await env.run(message, _task("one", action, **OPERANDS[action]))
                    self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.NOT_EXECUTED)
                    self.assertEqual(context.results["one"].reason, "mutation_batch_preflight_failed")
                    self.assertEqual((_untouched(env, guild, quiet), self.modlog.embeds), (([], [], [], []), []))


class ColorBatchTests(_PatchedCase):
    async def test_a_failing_color_task_blocks_an_independent_mutation(self) -> None:
        for bad in (
            _task("bad", "color_add", role_name="Teal"),
            _task("bad", "color_add", role_color_hex="#1abc9c"),
            _task("bad", "color_add", role_name="Teal", role_color_hex="teal"),
            _task("bad", "color_remove"),
            _task("bad", "color_publish"),
            _task("bad", "color_publish", target_channel="missing-channel"),
            _task("bad", "color_reload", target_channel="quiet"),
        ):
            with self.subTest(bad=bad.operands):
                env, message, guild, _actor, quiet = _scene("color_setup", bot_perms={**ROLES_BOT, **PANEL_BOT})
                context = await env.run(message, bad, _task("ok", "color_setup"))
                for task_id in ("bad", "ok"):
                    self.assertEqual(context.results[task_id].status, CapabilityTerminalStatus.NOT_EXECUTED)
                self.assertEqual(_untouched(env, guild, quiet), ([], [], [], []))


    async def test_a_valid_batch_executes_each_task_once_and_needs_no_database_state_at_preflight(self) -> None:
        env, message, guild, _actor, quiet = _scene("color_setup", bot_perms={**ROLES_BOT, **PANEL_BOT})
        context = await env.run(
            message,
            _task("setup", "color_setup"),
            _task("add", "color_add", **OPERANDS["color_add"]),
            _task("publish", "color_publish", target_channel="quiet"),
            _task("list", "color_list"),
        )
        self.assertTrue(all(result.status == CapabilityTerminalStatus.SUCCESS for result in context.results.values()), context.results)
        self.assertEqual(len(guild.created), len(DEFAULT_COLOR_ROLES) + 1)
        self.assertEqual([call[0] for call in env.db.writes()], ["upsert_color_role"] * (len(DEFAULT_COLOR_ROLES) + 1) + ["upsert_color_panel"])
        self.assertEqual(len(quiet.posts), 1)
        self.assertEqual(context.results["list"].payload["color_count"], len(DEFAULT_COLOR_ROLES) + 1)
        self.assertEqual([embed.title for embed in self.modlog.embeds], ["Color Roles Setup", "Color Role Added", "Color Panel Published"])


class ColorChannelTargetTests(_PatchedCase):
    async def test_the_channel_is_never_defaulted_to_the_current_or_a_mentioned_one(self) -> None:
        env, message, guild, _actor, quiet = _scene("color_publish")
        message.channel_mentions = [quiet]
        context = await env.run(message, _task("one", "color_publish"))
        self.assertEqual(context.results["one"].status, CapabilityTerminalStatus.NOT_EXECUTED)
        for native in (True, False):
            result = await env.execute(message, "color_publish", native=native, confidence=1.0)
            self.assertNotEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertEqual((_untouched(env, guild, quiet), message.channel.sent), (([], [], [], []), []))


if __name__ == "__main__":
    unittest.main()
