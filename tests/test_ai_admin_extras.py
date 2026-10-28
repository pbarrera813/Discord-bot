from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import discord

from cogs.ai_chat import AIChatCog
from cogs.moderation import ModerationCog
from services.admin_actions import AdminActionService
from services.ai_capability_orchestrator import CapabilityExecutionPlan, CapabilityRequest
from services.ai_operation_catalog import ADMIN_MUTATING_OPERATIONS, native_operand_schema, operation_effect, validate_operands
from services.xai_client import ROUTER_OUTPUT_SCHEMA
from tests.test_ai_tagging import _CaptureChannel, _DummyMessage, _async_return, _typed_admin_plan

NEW_MUTATIONS = ("set_slowmode", "set_nickname", "say")
NEW_ACTIONS = (*NEW_MUTATIONS, "user_info")
DESTRUCTIVE = ("clear_channel", "clone_channel", "purge_user_messages")


class _Perms:
    def __init__(self, **granted: bool) -> None:
        self.__dict__["_granted"] = dict(granted)

    def __getattr__(self, name: str) -> bool:
        return bool(self._granted.get(name, False))

    def __iter__(self):
        return iter(self._granted.items())


class _Role:
    color = SimpleNamespace(value=0)

    def __init__(self, role_id: int, position: int, name: str = "role", *, default: bool = False) -> None:
        self.id, self.position, self.name, self.default = role_id, position, name, default
        self.mention = f"<@&{role_id}>"

    def is_default(self) -> bool:
        return self.default

    def __le__(self, other: object) -> bool:
        return self.position <= getattr(other, "position", -1)

    def __ge__(self, other: object) -> bool:
        return self.position >= getattr(other, "position", -1)


class _Member:
    def __init__(self, member_id: int, name: str, *, perms: dict[str, bool] | None = None, position: int = 1,
                 fail: Exception | None = None) -> None:
        self.id, self.name, self.display_name, self.mention = member_id, name, name, f"<@{member_id}>"
        self.top_role = _Role(member_id, position, f"top-{name}")
        self.roles = [_Role(1, 0, "@everyone", default=True), self.top_role]
        self.guild_permissions = _Perms(**(perms or {}))
        self.joined_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        self.created_at = datetime(2020, 3, 4, tzinfo=timezone.utc)
        self.premium_since = None
        self.bot = False
        self.display_avatar = SimpleNamespace(url="https://cdn.example/a.png")
        self.fail = fail
        self.edits: list[dict[str, object]] = []

    def __str__(self) -> str:
        return f"{self.name}#0"

    async def edit(self, **kwargs: object) -> None:
        if self.fail is not None:
            raise self.fail
        self.edits.append(kwargs)


class _Msg:
    def __init__(self, message_id: int, author_id: int, *, age_days: int = 0, fail: Exception | None = None) -> None:
        self.id, self.author, self.fail, self.deleted = message_id, SimpleNamespace(id=author_id, bot=False), fail, False
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)

    async def delete(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.deleted = True


class _Channel(_CaptureChannel):
    def __init__(self, channel_id: int = 10, name: str = "general", *, fail: Exception | None = None,
                 clone_fail: Exception | None = None, clone_edit_fail: Exception | None = None) -> None:
        super().__init__()
        self.id, self.name, self.mention, self.fail, self.clone_fail = channel_id, name, f"<#{channel_id}>", fail, clone_fail
        self.clone_edit_fail = clone_edit_fail
        self.position, self.category, self.guild = 0, None, None
        self.edits: list[dict[str, object]] = []
        self.clones: list[_Channel] = []
        self.bulk_batches: list[list[_Msg]] = []
        self.history_calls: list[int | None] = []

    def permissions_for(self, member: object) -> object:
        return member.guild_permissions

    async def edit(self, **kwargs: object) -> None:
        if self.fail is not None:
            raise self.fail
        self.edits.append(kwargs)

    async def set_permissions(self, *_args: object, **_kwargs: object) -> None:
        return None

    def history(self, *, limit: int | None = None, oldest_first: bool = True):
        self.history_calls.append(limit)

        async def _iter():
            for item in self.history_messages[:limit]:
                yield item

        return _iter()

    async def delete_messages(self, messages: list[_Msg]) -> None:
        self.bulk_batches.append(list(messages))
        for message in messages:
            message.deleted = True

    async def clone(self, *, reason: str | None = None) -> "_Channel":
        if self.clone_fail is not None:
            raise self.clone_fail
        clone = _Channel(300 + len(self.clones), self.name, fail=self.clone_edit_fail)
        clone.guild = self.guild
        self.guild.text_channels.append(clone)
        self.clones.append(clone)
        return clone


class _Guild:
    def __init__(self, me: _Member, *members: _Member, channels: tuple[_Channel, ...] = (), owner_id: int = 999) -> None:
        self.id, self.owner_id, self.me = 1, owner_id, me
        self.members = list(members)
        self.text_channels = self.channels = list(channels)
        for channel in channels:
            channel.guild = self
        self.roles: list[_Role] = []
        self.emojis: list[object] = []

    def get_thread(self, _thread_id: int):
        return None

    def get_member(self, user_id: int):
        return next((member for member in self.members if member.id == user_id), None)

    def get_member_named(self, _token: str):
        return None

    def get_channel(self, channel_id: int):
        return next((channel for channel in self.text_channels if channel.id == channel_id), None)


class _Modlog:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []

    async def __call__(self, _guild: object, _db: object, embed: discord.Embed, **_kwargs: object) -> None:
        self.embeds.append(embed)

    def fields(self, index: int = 0) -> list[tuple[str, str]]:
        return [(field.name, field.value) for field in self.embeds[index].fields]


def _service() -> AdminActionService:
    return AdminActionService(SimpleNamespace(is_owner_user=lambda _user: False, db=None))


def _staff(**perms: bool) -> _Member:
    return _Member(99, "mod", perms=perms, position=10)


def _bot(**perms: bool) -> _Member:
    return _Member(42, "nitori", perms=perms, position=30)


class _ModlogCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.modlog = _Modlog()
        for target in ("services.admin_actions.send_modlog_embed", "cogs.moderation.send_modlog_embed"):
            patcher = patch(target, new=self.modlog)
            patcher.start()
            self.addCleanup(patcher.stop)


class AdminExtrasCatalogTests(unittest.TestCase):
    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            {"admin_action": "set_slowmode", "target_channel": "general", "slowmode_seconds": 0},
            {"admin_action": "set_slowmode", "target_channel": "general", "slowmode_seconds": 21600},
            {"admin_action": "set_nickname", "target_user_id": 5, "nickname": "n" * 32},
            {"admin_action": "set_nickname", "target_user_candidates": ["ana"], "nickname": "Ana"},
            {"admin_action": "user_info", "target_user_candidates": ["ana"]},
            {"admin_action": "say", "say_text": "s" * 2000},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("ADMIN", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        for operands, predicate in (
            ({"admin_action": "set_slowmode", "slowmode_seconds": 21601}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "set_slowmode", "slowmode_seconds": -1}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "set_slowmode", "slowmode_seconds": "30"}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "set_slowmode", "slowmode_seconds": True}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "set_nickname", "nickname": "n" * 33}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "set_nickname", "nickname": 5}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "say", "say_text": "s" * 2001}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "say", "say_text": 5}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "say", "channel_id": 5}, "unknown_task_operand"),
            ({"admin_action": "purge_channel"}, "task_operand_enum_invalid"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("ADMIN", operands), predicate)


    def test_destructive_operands_are_validated_and_their_effects_mutate(self) -> None:
        for operands in (
            {"admin_action": "clear_channel", "target_channel": "general"},
            {"admin_action": "clone_channel", "target_channel": "general"},
            {"admin_action": "purge_user_messages", "target_user_id": 5, "message_count": 500},
            {"admin_action": "purge_user_messages", "target_user_candidates": ["ana"], "message_count": 1},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("ADMIN", operands))
        for operands, predicate in (
            ({"admin_action": "purge_user_messages", "message_count": -1}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "purge_user_messages", "message_count": "5"}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "purge_user_messages", "message_count": True}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "clear_channel", "target_channel": 5}, "task_operand_type_or_bounds_invalid"),
            ({"admin_action": "clone_channel", "amount": 5}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("ADMIN", operands), predicate)
        self.assertTrue(set(DESTRUCTIVE) <= ADMIN_MUTATING_OPERATIONS)
        for action in DESTRUCTIVE:
            with self.subTest(action=action):
                self.assertEqual(operation_effect(SimpleNamespace(name="ADMIN", operands={"admin_action": action})), "mutate")
        legacy = ROUTER_OUTPUT_SCHEMA["properties"]["admin"]["properties"]["admin_action"]["enum"]
        self.assertTrue(set(DESTRUCTIVE) <= set(legacy) and set(DESTRUCTIVE) <= set(native_operand_schema()["admin_action"]["enum"]))


class SetSlowmodeServiceTests(_ModlogCase):
    async def test_sets_slowmode_and_writes_one_modlog_entry(self) -> None:
        channel = _Channel()
        actor, me = _staff(manage_channels=True), _bot(manage_channels=True)
        result = await _service().set_slowmode(_Guild(me, actor), actor, channel, 30)
        self.assertEqual((result.success, result.code), (True, "slowmode_set"))
        self.assertEqual(channel.edits, [{"slowmode_delay": 30, "reason": "Slowmode changed by mod#0"}])
        self.assertEqual(len(self.modlog.embeds), 1)
        self.assertEqual(self.modlog.embeds[0].title, "Slowmode Updated")


    async def test_out_of_range_seconds_never_reach_discord(self) -> None:
        for seconds in (-1, 21601):
            with self.subTest(seconds=seconds):
                channel = _Channel()
                actor, me = _staff(manage_channels=True), _bot(manage_channels=True)
                result = await _service().set_slowmode(_Guild(me, actor), actor, channel, seconds)
                self.assertEqual((result.success, result.code), (False, "invalid_slowmode"))
                self.assertEqual(channel.edits, [])

    async def test_denials_have_no_effect(self) -> None:
        for name, actor, me, code in (
            ("actor lacks manage_channels", _staff(manage_guild=True), _bot(manage_channels=True), "author_missing_manage_channels"),
            ("bot lacks manage_channels", _staff(manage_channels=True), _bot(), "bot_missing_manage_channels"),
        ):
            with self.subTest(case=name):
                channel = _Channel()
                result = await _service().set_slowmode(_Guild(me, actor), actor, channel, 30)
                self.assertEqual((result.success, result.code), (False, code))
                self.assertEqual((channel.edits, self.modlog.embeds), ([], []))


class SetNicknameServiceTests(_ModlogCase):
    def _world(self, *, actor_perms=None, bot_perms=None, target_position: int = 1, target_fail=None):
        actor = _staff(**({"manage_nicknames": True} if actor_perms is None else actor_perms))
        me = _bot(**({"manage_nicknames": True} if bot_perms is None else bot_perms))
        target = _Member(5, "ana", position=target_position, fail=target_fail)
        return _Guild(me, actor, target), actor, target


    async def test_denials_have_no_effect(self) -> None:
        cases = (
            ("actor lacks manage_nicknames", {"actor_perms": {"manage_guild": True}}, "author_missing_manage_nicknames"),
            ("bot lacks manage_nicknames", {"bot_perms": {}}, "bot_missing_manage_nicknames"),
            ("target is above the actor", {"target_position": 10}, "actor_hierarchy"),
            ("target is above the bot", {"target_position": 30}, "actor_hierarchy"),
        )
        for name, kwargs, code in cases:
            with self.subTest(case=name):
                guild, actor, target = self._world(**kwargs)
                if name == "target is above the bot":
                    actor.top_role = _Role(99, 40)
                    code = "bot_hierarchy"
                result = await _service().set_nickname(guild, actor, target, "Anita")
                self.assertEqual((result.success, result.code), (False, code))
                self.assertEqual((target.edits, self.modlog.embeds), ([], []))


class UserInfoServiceTests(_ModlogCase):
    async def test_returns_the_commands_data_and_writes_one_modlog_entry(self) -> None:
        actor = _staff(moderate_members=True)
        target = _Member(5, "ana", perms={"send_messages": True, "kick_members": True, "ban_members": False})
        result = await _service().user_info(_Guild(_bot(), actor, target), actor, target)
        self.assertEqual((result.success, result.code), (True, "user_info"))
        data = result.data
        self.assertEqual((data["user_id"], data["display_name"], data["booster"]), (5, "ana", False))
        self.assertEqual([role["name"] for role in data["roles"]], ["top-ana"])
        self.assertEqual(data["permissions"], ["Send Messages", "Kick Members"])
        self.assertEqual((data["joined_at"], data["created_at"]), (1704153600, 1583280000))
        self.assertEqual(self.modlog.embeds[0].title, "User Info Lookup")
        self.assertEqual(self.modlog.fields(), [("Moderator", "<@99>"), ("Target", "<@5> (5)")])


class SayServiceTests(_ModlogCase):
    async def test_posts_once_with_every_mention_disabled(self) -> None:
        channel = _Channel()
        actor, me = _staff(manage_messages=True), _bot(send_messages=True)
        text = "hello @everyone <@5> <@&7>"
        result = await _service().say(_Guild(me, actor), actor, channel, f"  {text}  ")
        self.assertEqual((result.success, result.code), (True, "say_sent"))
        self.assertEqual(channel.sent, [text])
        mentions = channel.sent_kwargs[0]["allowed_mentions"]
        self.assertEqual((mentions.everyone, mentions.users, mentions.roles, mentions.replied_user), (False, False, False, False))

    async def test_invalid_text_and_denials_post_nothing(self) -> None:
        for name, text, actor, me, code in (
            ("empty", "  ", _staff(manage_messages=True), _bot(send_messages=True), "missing_say_text"),
            ("too long", "s" * 2001, _staff(manage_messages=True), _bot(send_messages=True), "say_text_too_long"),
            ("actor lacks manage_messages", "hi", _staff(manage_guild=True), _bot(send_messages=True), "author_missing_manage_messages"),
            ("bot cannot send", "hi", _staff(manage_messages=True), _bot(), "bot_missing_send_messages"),
        ):
            with self.subTest(case=name):
                channel = _Channel()
                result = await _service().say(_Guild(me, actor), actor, channel, text)
                self.assertEqual((result.success, result.code), (False, code))
                self.assertEqual(channel.sent, [])


def _deleted(channel: _Channel) -> list[int]:
    return [message.id for message in channel.history_messages if message.deleted]


class ClearChannelServiceTests(_ModlogCase):
    def _world(self, *, actor_perms=None, bot_perms=None, **channel_kwargs):
        actor = _staff(**({"manage_channels": True} if actor_perms is None else actor_perms))
        me = _bot(**({"manage_messages": True} if bot_perms is None else bot_perms))
        channel = _Channel(**channel_kwargs)
        return _Guild(me, actor, channels=(channel,)), actor, channel


    async def test_denials_have_no_effect(self) -> None:
        for name, kwargs, code, text in (
            ("actor lacks manage_channels", {"actor_perms": {"manage_messages": True}}, "author_missing_manage_channels", "You do not have permission to manage channels."),
            ("bot lacks manage_messages", {"bot_perms": {"manage_channels": True}}, "bot_missing_manage_messages", "I do not have permission to clear that channel."),
        ):
            with self.subTest(case=name):
                guild, actor, channel = self._world(**kwargs)
                channel.history_messages = [_Msg(1, 5)]
                result = await _service().clear_channel(guild, actor, channel)
                self.assertEqual((result.success, result.code, result.message), (False, code, text))
                self.assertEqual((_deleted(channel), channel.history_calls, self.modlog.embeds), ([], [], []))


class CloneChannelServiceTests(_ModlogCase):
    def _world(self, *, actor_perms=None, bot_perms=None, **channel_kwargs):
        actor = _staff(**({"manage_channels": True} if actor_perms is None else actor_perms))
        me = _bot(**({"manage_channels": True} if bot_perms is None else bot_perms))
        channel = _Channel(**channel_kwargs)
        return _Guild(me, actor, channels=(channel,)), actor, channel


    async def test_denials_have_no_effect(self) -> None:
        for name, kwargs, code in (
            ("actor lacks manage_channels", {"actor_perms": {"manage_messages": True}}, "author_missing_manage_channels"),
            ("bot lacks manage_channels", {"bot_perms": {"manage_messages": True}}, "bot_missing_manage_channels"),
        ):
            with self.subTest(case=name):
                guild, actor, channel = self._world(**kwargs)
                result = await _service().clone_channel(guild, actor, channel)
                self.assertEqual((result.success, result.code, result.resource_handle), (False, code, None))
                self.assertEqual((channel.clones, self.modlog.embeds), ([], []))


class PurgeUserMessagesServiceTests(_ModlogCase):
    def _world(self, *, actor_perms=None, bot_perms=None, channel_cls=_Channel):
        actor = _staff(**({"manage_messages": True} if actor_perms is None else actor_perms))
        me = _bot(**({"manage_messages": True} if bot_perms is None else bot_perms))
        target = _Member(5, "ana")
        channel = channel_cls()
        return _Guild(me, actor, target, channels=(channel,)), actor, target, channel


    async def test_denials_have_no_effect(self) -> None:
        for name, kwargs, code, text in (
            ("actor lacks manage_messages", {"actor_perms": {"manage_channels": True}}, "author_missing_manage_messages", "You do not have permission to delete messages."),
            ("bot lacks manage_messages", {"bot_perms": {}}, "bot_missing_manage_messages", "No messages could be deleted due to permissions or age limits."),
        ):
            with self.subTest(case=name):
                guild, actor, target, channel = self._world(**kwargs)
                channel.history_messages = [_Msg(1, 5)]
                result = await _service().purge_user_messages(guild, actor, channel, target, 1)
                self.assertEqual((result.success, result.code, result.message), (False, code, text))
                self.assertEqual((channel.history_calls, _deleted(channel), self.modlog.embeds), ([], [], []))


def _cog_for_commands(cls, guild: _Guild) -> object:
    cog = object.__new__(cls)
    cog.bot = SimpleNamespace(is_owner_user=lambda _user: False, db=None)
    cog.admin_actions = AdminActionService(cog.bot)

    async def lang(_guild: object) -> str:
        return "en"

    async def defer(_ctx: object) -> None:
        return None

    cog._lang, cog._maybe_defer = lang, defer
    return cog


class _CommandCtx:
    def __init__(self, guild: _Guild, author: _Member) -> None:
        self.guild, self.author, self.interaction, self.message = guild, author, None, None
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.sent.append((args, kwargs))

    def texts(self) -> list[str]:
        out = []
        for args, kwargs in self.sent:
            embed = kwargs.get("embed")
            out.append(embed.description if embed is not None and embed.description else (args[0] if args else ""))
        return out


class CommandParityTests(_ModlogCase):
    def _slowmode(self, **channel_kwargs: object):
        actor, me = _staff(manage_channels=True), _bot(manage_channels=True)
        channel = _Channel(**channel_kwargs)
        guild = _Guild(me, actor, channels=(channel,))
        return _cog_for_commands(ModerationCog, guild), _CommandCtx(guild, actor), channel

    async def test_slowmode_set_and_disable_messages_and_modlog(self) -> None:
        cog, ctx, channel = self._slowmode()
        await ModerationCog.slowmode.callback(cog, ctx, channel, "30")
        await ModerationCog.slowmode.callback(cog, ctx, channel, "off")
        self.assertEqual(ctx.texts(), ["✅ Slowmode set to `30s` in <#10>.", "✅ Slowmode disabled in <#10>."])
        self.assertEqual([edit["slowmode_delay"] for edit in channel.edits], [30, 0])
        self.assertEqual(len(self.modlog.embeds), 2)
        self.assertEqual(
            self.modlog.fields(0),
            [("Moderator", "<@99>"), ("Target", "<#10>"), ("Reason", "Manual slowmode update"), ("Details", "Slowmode: 30s")],
        )


    def _setnick(self, *, target_position: int = 1, fail: Exception | None = None):
        actor, me = _staff(manage_nicknames=True), _bot(manage_nicknames=True)
        target = _Member(5, "ana", position=target_position, fail=fail)
        guild = _Guild(me, actor, target)
        return _cog_for_commands(ModerationCog, guild), _CommandCtx(guild, actor), target

    async def test_setnick_success_message_and_modlog(self) -> None:
        cog, ctx, target = self._setnick()
        await ModerationCog.setnick.callback(cog, ctx, target, nickname="  Anita ")
        self.assertEqual(ctx.texts(), ["✅ Nickname updated for <@5>: `Anita`."])
        self.assertEqual(target.edits, [{"nick": "Anita", "reason": "Nickname changed by mod#0"}])
        self.assertEqual(self.modlog.fields(), [("Moderator", "<@99>"), ("Target", "<@5>"), ("Reason", "New nickname: Anita")])
        self.assertEqual(len(self.modlog.embeds), 1)


    async def test_userinfo_embed_and_single_modlog_entry(self) -> None:
        actor = _staff(moderate_members=True)
        target = _Member(5, "ana", perms={"send_messages": True})
        guild = _Guild(_bot(), actor, target)
        cog, ctx = _cog_for_commands(ModerationCog, guild), _CommandCtx(guild, actor)
        await ModerationCog.userinfo.callback(cog, ctx, target)
        (args, kwargs), = ctx.sent
        self.assertEqual(args, ("User information",))
        fields = {field.name: field.value for field in kwargs["embed"].fields}
        self.assertEqual(fields, {
            "Name": "ana", "ID": "5", "Server Booster": "No", "Roles": "<@&5>", "Permissions": "Send Messages",
            "Joined Server": "<t:1704153600:F>", "Joined Discord": "<t:1583280000:F>",
        })
        self.assertEqual(self.modlog.fields(), [("Moderator", "<@99>"), ("Target", "<@5> (5)")])
        self.assertEqual(len(self.modlog.embeds), 1)


class DestructiveCommandParityTests(_ModlogCase):
    def _ctx(self, *, actor_perms: dict[str, bool], bot_perms: dict[str, bool], **channel_kwargs: object):
        actor, me = _staff(**actor_perms), _bot(**bot_perms)
        channel, target = _Channel(**channel_kwargs), _Member(5, "ana")
        guild = _Guild(me, actor, target, channels=(channel,))
        ctx = _CommandCtx(guild, actor)
        ctx.channel = channel
        return _cog_for_commands(ModerationCog, guild), ctx, channel, target

    async def test_clearchannel_success_message_and_modlog(self) -> None:
        cog, ctx, channel, _target = self._ctx(actor_perms={"manage_channels": True}, bot_perms={"manage_messages": True})
        channel.history_messages = [_Msg(1, 5), _Msg(2, 5), _Msg(3, 5, age_days=20)]
        await ModerationCog.clearchannel.callback(cog, ctx, channel)
        self.assertEqual(ctx.texts(), ["✅ Cleared messages in <#10>."])
        self.assertEqual(_deleted(channel), [1, 2, 3])
        self.assertEqual(self.modlog.embeds[0].title, "Channel Cleared")
        self.assertEqual(len(self.modlog.embeds), 1)
        self.assertEqual(self.modlog.fields(), [
            ("Moderator", "<@99>"), ("Target", "#general (10)"), ("Reason", "Manual channel clear"), ("Details", "Deleted messages: 3"),
        ])


    async def test_clonechannel_success_message_and_modlog(self) -> None:
        cog, ctx, channel, _target = self._ctx(actor_perms={"manage_channels": True}, bot_perms={"manage_channels": True})
        await ModerationCog.clonechannel.callback(cog, ctx, channel)
        self.assertEqual(ctx.texts(), ["✅ Cloned channel <#10> into <#300>."])
        self.assertEqual(len(channel.clones), 1)
        self.assertEqual(len(self.modlog.embeds), 1)
        self.assertEqual(self.modlog.embeds[0].title, "Channel Cloned")
        self.assertEqual(self.modlog.fields(), [
            ("Moderator", "<@99>"), ("Target", "#general (10)"), ("Reason", "Manual channel clone"), ("Details", "Clone created: #general (300)"),
        ])


    async def _purge(self, cog, ctx, target, amount):
        with patch.object(discord, "TextChannel", _Channel):
            await ModerationCog.purgeuser.callback(cog, ctx, target, amount)


def _cog() -> AIChatCog:
    cog = AIChatCog.__new__(AIChatCog)
    cog.bot = SimpleNamespace(db=None, is_owner_user=lambda _user: False)
    cog._admin_actions = AdminActionService(cog.bot)
    return cog


def _world(*, actor_perms: dict[str, bool], bot_perms: dict[str, bool], target_position: int = 1):
    actor, me = _staff(**actor_perms), _bot(**bot_perms)
    quiet = _Channel(200, "quiet")
    target = _Member(5, "ana", position=target_position)
    here = _Channel()
    guild = _Guild(me, actor, target, channels=(quiet, here))
    message = _DummyMessage(attachments=[], author_id=99, guild_id=1, channel_id=10, content="<@42> do it", message_id=8500)
    message.guild, message.channel, message.author = guild, here, actor
    quiet.history_messages = [_Msg(1, 5), _Msg(2, 77)]
    here.history_messages = [_Msg(8500, 5), _Msg(3, 5), _Msg(4, 77), _Msg(5, 5)]
    return message, guild, actor, target, quiet, here


def _plan(*tasks: CapabilityRequest) -> CapabilityExecutionPlan:
    semantic_plan = SimpleNamespace(
        semantic_source="router", semantic_status="SUCCESS", semantic_failure_reason=None,
        valid=True, capabilities=tasks, requirements=(),
    )
    return CapabilityExecutionPlan(semantic_plan=semantic_plan, ordered_capabilities=tasks, dispatch_action="ADMIN_ACTION")


def _task(task_id: str, action: str, **operands: object) -> CapabilityRequest:
    return CapabilityRequest(
        "ADMIN", task_id=task_id, owner_action="ADMIN_ACTION", scope="DISCORD_ACTION",
        operands={"admin_action": action, **operands}, requested_outputs=("result",),
    )


OPERANDS = {
    "set_slowmode": {"target_channel": "quiet", "slowmode_seconds": 45},
    "set_nickname": {"target_user_id": 5, "nickname": "Anita"},
    "user_info": {"target_user_id": 5},
    "say": {"say_text": "hello @everyone"},
    "clear_channel": {"target_channel": "quiet"},
    "clone_channel": {"target_channel": "quiet"},
    "purge_user_messages": {"target_user_id": 5, "message_count": 2},
}
STAFF_PERMS = {
    "set_slowmode": {"manage_channels": True},
    "set_nickname": {"manage_nicknames": True},
    "user_info": {"moderate_members": True},
    "say": {"manage_messages": True},
    "clear_channel": {"manage_channels": True},
    "clone_channel": {"manage_channels": True},
    "purge_user_messages": {"manage_messages": True},
}
BOT_PERMS = {
    "set_slowmode": {"manage_channels": True},
    "set_nickname": {"manage_nicknames": True},
    "user_info": {},
    "say": {"send_messages": True},
    "clear_channel": {"manage_messages": True},
    "clone_channel": {"manage_channels": True},
    "purge_user_messages": {"manage_messages": True},
}


class TypedBatchTests(_ModlogCase):
    async def _run(self, message, *tasks):
        return await _cog()._execute_live_media_composition_result_only(
            message, "", _plan(*tasks), settings=SimpleNamespace(server_context=""),
        )


def _route(action: str, operands: dict[str, object]) -> dict[str, object]:
    return {
        "participation": "RESPOND", "action": "ADMIN_ACTION", "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "ADMIN_ACTION_REQUEST", "resolved_request": None,
        "admin": {"valid": True, "admin_action": action, **operands, "confidence": 1.0},
        "capability_plan": _typed_admin_plan(admin_action=action, operands=operands),
        "valid": True, "failure": False,
    }


class TypedEndToEndTests(_ModlogCase):
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

        llm = _LLM()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(user=bot_user, application_id=1234, db=db, llm_client=llm, is_owner_user=lambda _user: False))
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message, _guild, _actor, target, quiet, here = _world(
            actor_perms=STAFF_PERMS[action] if staff else {}, bot_perms=BOT_PERMS[action],
        )
        message.mentions = [bot_user]
        await cog.on_message(message)
        return llm, message, contexts[-1].bundle.by_task["admin_operation_1"] if contexts else None, (target, quiet, here)


if __name__ == "__main__":
    unittest.main()
