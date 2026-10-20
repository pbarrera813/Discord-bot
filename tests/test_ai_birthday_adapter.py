from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import discord

import cogs.birthdays as birthdays
from cogs.ai_chat import AIChatCog
from cogs.birthdays import BirthdaysCog
from services.ai_capability_orchestrator import CapabilityTerminalStatus
from services.ai_operation_catalog import operation_effect, validate_operands
from services.xai_client import XAIClient
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class _Member:
    def __init__(self, user_id: int, *, bot: bool = False, roles: tuple[object, ...] = ()) -> None:
        self.id = user_id
        self.bot = bot
        self.roles = list(roles)
        self.name = self.display_name = f"user{user_id}"
        self.mention = f"<@{user_id}>"
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")

    def __str__(self) -> str:
        return self.name


class _Guild:
    def __init__(self, members: list[_Member] | None = None, roles: list[object] | None = None) -> None:
        self.id = 1
        self.members = {member.id: member for member in members or []}
        self.roles = {role.id: role for role in roles or []}

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_role(self, role_id: int):
        return self.roles.get(role_id)

    def get_member_named(self, name: str):
        return next((member for member in self.members.values() if member.name == name), None)


class _FakeDb:
    def __init__(
        self, *, profiles: list[dict[str, object]] | None = None, enabled: int = 1, disable_ages: int = 0,
        blacklist_users: tuple[int, ...] = (), blacklist_roles: tuple[int, ...] = (),
        trusted_role_id: int | None = None, trusted_prevent_list: int = 0, fail: Exception | None = None,
        templates: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.profiles = {int(row["user_id"]): dict(row) for row in profiles or []}
        self.settings = {
            "enabled": enabled, "disable_ages": disable_ages, "server_timezone": "UTC",
            "trusted_role_id": trusted_role_id, "trusted_prevent_list": trusted_prevent_list,
            "channel_id": None, "role_id": None,
        }
        self.blacklist_users = list(blacklist_users)
        self.blacklist_roles = list(blacklist_roles)
        self.fail = fail
        self.upserts: list[dict[str, object]] = []
        self.deletes: list[tuple[int, int]] = []
        self.settings_updates: list[dict[str, object]] = []
        self.event_updates: list[tuple[str, dict[str, object]]] = []
        self.events: dict[str, dict[str, object]] = {}
        self.templates = {key: [dict(row) for row in rows] for key, rows in (templates or {}).items()}
        self.template_ops: list[tuple[str, str, object]] = []
        self.blacklist_ops: list[tuple[str, int]] = []
        self.call_count = 0

    def _touch(self) -> None:
        self.call_count += 1
        if self.fail:
            raise self.fail

    async def get_or_create_birthday_guild_settings(self, guild_id):
        self._touch()
        return dict(self.settings)

    async def get_birthday_profile(self, guild_id, user_id):
        self._touch()
        return self.profiles.get(user_id)

    async def list_birthday_profiles(self, guild_id):
        self._touch()
        return list(self.profiles.values())

    async def list_birthday_blacklist_users(self, guild_id):
        self._touch()
        return self.blacklist_users

    async def list_birthday_blacklist_roles(self, guild_id):
        self._touch()
        return self.blacklist_roles

    async def upsert_birthday_profile(self, **kwargs):
        self._touch()
        self.upserts.append(kwargs)

    async def delete_birthday_profile(self, guild_id, user_id):
        self._touch()
        self.deletes.append((guild_id, user_id))
        return self.profiles.pop(user_id, None) is not None

    async def update_birthday_guild_settings(self, guild_id, **kwargs):
        self._touch()
        self.settings_updates.append(kwargs)
        self.settings.update({key: value for key, value in kwargs.items() if value is not Ellipsis})
        return dict(self.settings)

    async def get_or_create_birthday_event_settings(self, guild_id, event_type):
        self._touch()
        return dict(self.events.setdefault(event_type, {
            "enabled": 1, "ping_setting": "none", "message_mode": "embed", "embed_title": "", "embed_color": "",
            "embed_image_url": "", "birthday_message_no_year": "", "birthday_message_with_age": "",
        }))

    async def update_birthday_event_settings(self, guild_id, event_type, **kwargs):
        self.event_updates.append((event_type, kwargs))
        cfg = await self.get_or_create_birthday_event_settings(guild_id, event_type)
        self.events[event_type] = {**cfg, **{key: value for key, value in kwargs.items() if value is not None}}
        return dict(self.events[event_type])

    def _templates(self, event_type: str) -> list[dict[str, object]]:
        key = event_type.strip().lower()  # like the real database, an unknown event type is a ValueError
        if key not in {"birthday", "member_anniversary", "server_anniversary"}:
            raise ValueError(event_type)
        return self.templates.setdefault(key, [])

    async def count_birthday_templates(self, guild_id, event_type):
        self._touch()
        return len(self._templates(event_type))

    async def add_birthday_template(self, guild_id, event_type, template_text):
        self._touch()
        rows = self._templates(event_type)
        template_id = max((int(row["id"]) for row in rows), default=0) + 1
        rows.append({"id": template_id, "template_text": template_text, "enabled": 1})
        self.template_ops.append(("add", event_type, template_text))
        return template_id

    async def list_birthday_templates(self, guild_id, event_type):
        self._touch()
        return list(self._templates(event_type))

    async def delete_birthday_template(self, guild_id, event_type, template_id):
        self._touch()
        rows = self._templates(event_type)
        kept = [row for row in rows if row["id"] != template_id]
        self.templates[event_type.strip().lower()] = kept
        self.template_ops.append(("delete", event_type, template_id))
        return len(kept) != len(rows)

    async def add_birthday_blacklist_user(self, guild_id, user_id):
        self._touch()
        self.blacklist_ops.append(("add_user", user_id))

    async def remove_birthday_blacklist_user(self, guild_id, user_id):
        self._touch()
        self.blacklist_ops.append(("remove_user", user_id))

    async def add_birthday_blacklist_role(self, guild_id, role_id):
        self._touch()
        self.blacklist_ops.append(("add_role", role_id))

    async def remove_birthday_blacklist_role(self, guild_id, role_id):
        self._touch()
        self.blacklist_ops.append(("remove_role", role_id))

    async def get_guild_settings(self, guild_id):
        return SimpleNamespace(language_code="en")

    def writes(self) -> int:
        return (
            len(self.upserts) + len(self.deletes) + len(self.settings_updates) + len(self.event_updates)
            + len(self.template_ops) + len(self.blacklist_ops)
        )


def _profile(user_id: int, month: int, day: int, birth_year: int | None = None) -> dict[str, object]:
    return {"guild_id": 1, "user_id": user_id, "month": month, "day": day, "birth_year": birth_year}


def _message(*, guild: _Guild | None | bool = True, mentions: list[_Member] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(id=99),
        guild=_Guild([_Member(7), _Member(8)]) if guild is True else guild,
        channel=SimpleNamespace(id=10),
        mentions=mentions or [],
    )


def _cog(db: object) -> AIChatCog:
    cog = AIChatCog.__new__(AIChatCog)
    cog.bot = SimpleNamespace(db=db)
    cog._admin_actions = SimpleNamespace()
    return cog


def _fixed_clock():
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    # The fake server zone is UTC; a Windows venv without tzdata cannot build ZoneInfo("UTC").
    return patch.multiple(birthdays, datetime=Fixed, ZoneInfo=lambda _name: timezone.utc)


def _birthday_route(operands: dict[str, object], *, task_id: str = "birthday_1") -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "set my birthday",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "set my birthday", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "birthday", "objective": "set my birthday", "required": True,
                "satisfaction_criterion": "The birthday is saved.",
                "capability_owner": "BIRTHDAY", "owner_task_id": task_id, "depends_on": [],
            }],
            "capabilities": [{
                "task_id": task_id, "name": "BIRTHDAY", "required": True,
                "evidence_span": "set my birthday", "depends_on": [],
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


class BirthdayCatalogTests(unittest.TestCase):

    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            {"birthday_action": "show"},
            {"birthday_action": "set", "birth_month": 12, "birth_day": 31},
            {"birthday_action": "set", "birth_month": 3, "birth_day": 14, "birth_year": 1900},
            {"birthday_action": "remove"},
            {"birthday_action": "view", "target_user_id": 5},
            {"birthday_action": "view", "target_user_candidates": ["someone"]},
            {"birthday_action": "next", "birthday_count": 1},
            {"birthday_action": "next", "birthday_count": 25},
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("BIRTHDAY", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        for operands, predicate in (
            ({"birthday_action": "channel"}, "task_operand_enum_invalid"),
            ({"birthday_action": "timezone"}, "task_operand_enum_invalid"),
            ({"birthday_action": None}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_month": 0}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_month": 13}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_day": 0}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_day": 32}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_year": 1899}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_month": "3"}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "set", "birth_month": True}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "next", "birthday_count": 0}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "next", "birthday_count": 26}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "show", "user_id": 5}, "unknown_task_operand"),
            ({"birthday_action": "show", "birthday_channel": 5}, "unknown_task_operand"),
            ({"birthday_action": "show", "trusted_role": "x"}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("BIRTHDAY", operands), predicate)


class BirthdaySharedFunctionTests(unittest.IsolatedAsyncioTestCase):
    def test_validate_birthday(self) -> None:
        this_year = datetime.now(timezone.utc).year
        for month, day, year in ((3, 14, None), (2, 29, None), (12, 31, 1900), (1, 1, this_year)):
            with self.subTest(month=month, day=day, year=year):
                self.assertIsNone(birthdays.validate_birthday(month, day, year))
        for month, day, year, reason in (
            (2, 30, None, "invalid_date"), (13, 1, None, "invalid_date"), (4, 31, None, "invalid_date"),
            (0, 1, None, "invalid_date"), (3, 14, 1899, "invalid_year"), (3, 14, this_year + 1, "invalid_year"),
        ):
            with self.subTest(month=month, day=day, year=year):
                with self.assertRaises(birthdays.BirthdayError) as caught:
                    birthdays.validate_birthday(month, day, year)
                self.assertEqual(str(caught.exception), reason)

    async def test_set_and_remove(self) -> None:
        db = _FakeDb(profiles=[_profile(99, 1, 1)])
        await birthdays.set_birthday(db, 1, 99, 3, 14, 1990)
        self.assertEqual(
            db.upserts,
            [{"guild_id": 1, "user_id": 99, "month": 3, "day": 14, "birth_year": 1990, "timezone_name": None}],
        )
        self.assertTrue(await birthdays.remove_birthday(db, 1, 99))
        self.assertFalse(await birthdays.remove_birthday(db, 1, 99))
        self.assertEqual(db.deletes, [(1, 99), (1, 99)])


    async def test_next_is_bounded_sorted_and_filtered_like_the_command(self) -> None:
        role_blocked, role_other = SimpleNamespace(id=500), SimpleNamespace(id=501)
        trusted = SimpleNamespace(id=600)
        guild = _Guild(
            [
                _Member(1), _Member(2, bot=True), _Member(3), _Member(4, roles=(role_blocked,)),
                _Member(5), _Member(6, roles=(trusted,)), _Member(7, roles=(role_other,)),
            ],
            roles=[trusted],
        )
        profiles = [
            _profile(1, 6, 20, 1990), _profile(2, 6, 16), _profile(3, 6, 17), _profile(4, 6, 18),
            _profile(5, 6, 19), _profile(6, 6, 15), _profile(7, 1, 1), _profile(8, 2, 29),
        ]
        db = _FakeDb(profiles=profiles, blacklist_users=(3,), blacklist_roles=(500,), trusted_role_id=600)
        with _fixed_clock():
            rows = await birthdays.next_birthdays(db, guild, 10)
        self.assertEqual(
            [(row["member"].id, row["month"], row["day"]) for row in rows],
            [(6, 6, 15), (5, 6, 19), (1, 6, 20), (7, 1, 1)],
        )
        self.assertEqual([row["days_until"] for row in rows], [0, 4, 5, (date(2027, 1, 1) - NOW.date()).days])
        self.assertTrue(all(set(row) == {"member", "month", "day", "days_until"} for row in rows))
        trusted_only = _FakeDb(
            profiles=profiles, blacklist_users=(3,), blacklist_roles=(500,), trusted_role_id=600, trusted_prevent_list=1,
        )
        with _fixed_clock():
            rows = await birthdays.next_birthdays(trusted_only, guild, 10)
        self.assertEqual([row["member"].id for row in rows], [6])


class BirthdayCommandParityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cog(db: _FakeDb) -> BirthdaysCog:
        cog = object.__new__(BirthdaysCog)
        cog.bot = SimpleNamespace(
            db=SimpleNamespace(
                get_guild_settings=_async_return(SimpleNamespace(language_code="en")),
                get_or_create_birthday_guild_settings=db.get_or_create_birthday_guild_settings,
                upsert_birthday_profile=db.upsert_birthday_profile,
                delete_birthday_profile=db.delete_birthday_profile,
                get_birthday_profile=db.get_birthday_profile,
                list_birthday_profiles=db.list_birthday_profiles,
                list_birthday_blacklist_users=db.list_birthday_blacklist_users,
                list_birthday_blacklist_roles=db.list_birthday_blacklist_roles,
            )
        )
        return cog

    @staticmethod
    def _ctx(guild: _Guild | None = None):
        sent: list[dict[str, object]] = []

        async def send(content=None, **kwargs):
            sent.append({"content": content, **kwargs})

        ctx = SimpleNamespace(
            guild=guild if guild is not None else _Guild([_Member(7)]),
            author=SimpleNamespace(id=99), interaction=None, send=send,
        )
        return ctx, sent

    async def test_set_saves_through_the_shared_function(self) -> None:
        db = _FakeDb()
        ctx, sent = self._ctx()
        with patch.object(birthdays, "set_birthday", wraps=birthdays.set_birthday) as shared:
            await BirthdaysCog.birthday_set.callback(self._cog(db), ctx, date_text="03-14", birth_year=1990)
        shared.assert_called_once()
        self.assertEqual([(item["guild_id"], item["user_id"], item["month"], item["day"], item["birth_year"]) for item in db.upserts], [(1, 99, 3, 14, 1990)])
        self.assertEqual([item["content"] for item in sent], ["Birthday saved: `03-14`"])


    async def test_remove_deletes_through_the_shared_function(self) -> None:
        for present, text in ((True, "Your birthday data was removed."), (False, "You had no birthday data stored.")):
            with self.subTest(present=present):
                db = _FakeDb(profiles=[_profile(99, 3, 14)] if present else [])
                ctx, sent = self._ctx()
                with patch.object(birthdays, "remove_birthday", wraps=birthdays.remove_birthday) as shared:
                    await BirthdaysCog.birthday_remove.callback(self._cog(db), ctx)
                shared.assert_called_once()
                self.assertEqual([item["content"] for item in sent], [text])
                self.assertEqual(db.deletes, [(1, 99)])


    async def test_view_hides_a_blacklisted_member_but_not_the_requesters_own_profile(self) -> None:
        role = SimpleNamespace(id=500)
        for name, member, kwargs in (
            ("blacklisted user", _Member(7), {"blacklist_users": (7,)}),
            ("blacklisted role", _Member(7, roles=(role,)), {"blacklist_roles": (500,)}),
        ):
            with self.subTest(case=name):
                db = _FakeDb(profiles=[_profile(7, 3, 14, 1990)], **kwargs)
                ctx, sent = self._ctx()
                await BirthdaysCog.birthday_view.callback(self._cog(db), ctx, user=member)
                self.assertEqual([(item["content"], item.get("embed")) for item in sent], [("No birthday set for <@7>.", None)])
        db = _FakeDb(profiles=[_profile(99, 3, 14, 1990)], blacklist_users=(99,))
        ctx, sent = self._ctx()
        await BirthdaysCog.birthday_view.callback(self._cog(db), ctx, user=_Member(99))
        self.assertEqual(sent[0]["embed"].title, "Birthday profile")


class BirthdayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_set_task_executes_once_with_one_synthesis_and_one_reply(self) -> None:
        route = _birthday_route({"birthday_action": "set", "birth_month": 3, "birth_day": 14, "birth_year": 1990})
        self.assertTrue(route["valid"], route.get("failure_reason"))
        fake = _FakeDb()
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
                return "Your birthday is saved."

        llm = _LLM()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
            get_or_create_birthday_guild_settings=fake.get_or_create_birthday_guild_settings,
            upsert_birthday_profile=fake.upsert_birthday_profile,
        )
        bot = SimpleNamespace(user=bot_user, application_id=1234, db=db, llm_client=llm, is_owner_user=lambda _user: False)
        cog = AIChatCog(bot)
        contexts: list[object] = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        message = _DummyMessage(
            attachments=[], author_id=99, guild_id=1, channel_id=10,
            content="<@42> my birthday is March 14 1990", mentions=[bot_user], message_id=8400,
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        await cog.on_message(message)

        self.assertEqual(
            fake.upserts,
            [{"guild_id": 1, "user_id": 99, "month": 3, "day": 14, "birth_year": 1990, "timezone_name": None}],
        )
        result = contexts[-1].bundle.by_task["birthday_1"]
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)
        self.assertEqual(llm.route_calls, 1)
        self.assertEqual(len(llm.chat_calls), 1)
        self.assertIn("Trusted birthday result: ", str(llm.chat_calls[0]["trusted_capability_context"]))
        self.assertEqual([reply for reply, _mention in message.replies], ["Your birthday is saved."])


class _Channel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id, self.name = channel_id, name


class _TextChannel(discord.TextChannel):
    """A real discord.TextChannel type without a connection state; records what is sent."""

    def __init__(self, channel_id: int = 10) -> None:
        self.id = channel_id
        self.name = "general"
        self.sent: list[dict[str, object]] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class _SettingsGuild:
    def __init__(self, *, owner_id: int | None = 555, configured: _TextChannel | None = None) -> None:
        self.id = 1
        self.name = "Test Server"
        self.owner_id = owner_id
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.text_channels = [_Channel(200, "birthdays"), _Channel(201, "general")]
        self.roles = [
            SimpleNamespace(id=300, name="Birthday", mention="<@&300>"),
            SimpleNamespace(id=301, name="Birthday Extra", mention="<@&301>"),
            SimpleNamespace(id=302, name="Mods", mention="<@&302>"),
        ]
        self.configured = configured
        self.members = {7: _Member(7)}

    def get_channel(self, channel_id: int):
        if self.configured is not None and channel_id == self.configured.id:
            return self.configured
        return next((channel for channel in self.text_channels if channel.id == channel_id), None)

    def get_role(self, role_id: int):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_member_named(self, name: str):
        return next((member for member in self.members.values() if member.name == name), None)


def _preview_cog(db: object):
    posted: list[dict[str, object]] = []

    async def announce(**kwargs):
        posted.append(kwargs)

    return SimpleNamespace(bot=SimpleNamespace(db=db), _send_event_announcement=announce), posted


def _zones(*known: str):
    """The Windows venv has no tzdata: only the listed names exist as IANA zones."""

    def factory(name: str):
        if name in known:
            return timezone.utc
        raise ZoneInfoNotFoundError(name)

    return patch.object(birthdays, "ZoneInfo", factory)


SETTINGS_OPERANDS = (
    {"birthday_action": "setup", "target_channel": "birthdays", "target_role_candidates": ["Birthday"]},
    {"birthday_action": "set_channel", "target_channel": "birthdays"},
    {"birthday_action": "set_role", "target_role_candidates": ["Mods"]},
    {"birthday_action": "set_timezone", "birthday_timezone": "Europe/Madrid"},
    {"birthday_action": "set_mode", "birthday_mode": "server"},
    {"birthday_action": "set_ages", "birthday_ages": False},
    {"birthday_action": "event", "birthday_event": "birthday_year", "birthday_event_color": "#00ffaa", "birthday_event_message": "Happy {user}!"},
    {"birthday_action": "preview", "birthday_event": "server_anniversary"},
)


class BirthdaySettingsCatalogTests(unittest.TestCase):
    def test_every_settings_operation_mutates(self) -> None:
        for operands in SETTINGS_OPERANDS:
            with self.subTest(action=operands["birthday_action"]):
                self.assertEqual(operation_effect(SimpleNamespace(name="BIRTHDAY", operands=operands)), "mutate")


class BirthdaySettingsSharedTests(unittest.IsolatedAsyncioTestCase):
    async def test_timezone_is_validated_then_stored(self) -> None:
        db = _FakeDb()
        with _zones("Europe/Madrid"):
            self.assertEqual(await birthdays.set_birthday_timezone(db, 1, " Europe/Madrid "), "Europe/Madrid")
            self.assertEqual(db.settings_updates, [{"server_timezone": "Europe/Madrid"}])
            for raw in ("Mars/Base", "", "  ", None):
                with self.subTest(raw=raw):
                    with self.assertRaises(birthdays.BirthdayError) as caught:
                        await birthdays.set_birthday_timezone(db, 1, raw)
                    self.assertEqual(str(caught.exception), "invalid_timezone")
        self.assertEqual(len(db.settings_updates), 1)


    async def test_event_updates_the_variant_specific_message_and_normalizes_input(self) -> None:
        for key, event_type, variant, field in (
            ("birthday_default", "birthday", "default", "birthday_message_no_year"),
            ("birthday_year", "birthday", "year", "birthday_message_with_age"),
            ("member_anniversary", "member_anniversary", "default", "birthday_message_no_year"),
            ("server_anniversary", "server_anniversary", "default", "birthday_message_no_year"),
        ):
            with self.subTest(key=key):
                db = _FakeDb()
                result = await birthdays.set_birthday_event(db, 1, key, "00FFAA", "clear", "Hello {user}")
                self.assertEqual(result[:2], (event_type, variant))
                self.assertEqual(
                    db.event_updates,
                    [(event_type, {
                        "enabled": True, "message_mode": "embed", "embed_color": "#00ffaa", "embed_image_url": "", field: "Hello {user}",
                    })],
                )


    async def test_preview_posts_one_sample_without_mentions_and_rejects_an_unknown_type_first(self) -> None:
        db = _FakeDb()
        cog, posted = _preview_cog(db)
        channel = _TextChannel()
        await birthdays.preview_birthday_event(cog, _SettingsGuild(), SimpleNamespace(id=99), channel, "birthday_default", "en")
        self.assertEqual([(item["event_type"], item["channel"], item["age"]) for item in posted], [("birthday", channel, None)])
        mentions = posted[0]["allowed_mentions"]
        self.assertEqual((mentions.everyone, mentions.users, mentions.roles), (False, False, False))
        calls = db.call_count
        with self.assertRaises(birthdays.BirthdayError) as caught:
            await birthdays.preview_birthday_event(cog, _SettingsGuild(), SimpleNamespace(id=99), channel, "nope", "en")
        self.assertEqual(str(caught.exception), "invalid_preview")
        self.assertEqual((db.call_count, len(posted)), (calls, 1))


class BirthdaySettingsCommandParityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cog(db: _FakeDb) -> BirthdaysCog:
        cog = object.__new__(BirthdaysCog)
        cog.bot = SimpleNamespace(db=db)
        return cog

    @staticmethod
    def _ctx(channel: object | None = None, guild: _SettingsGuild | None = None):
        sent: list[dict[str, object]] = []

        async def send(content=None, **kwargs):
            sent.append({"content": content, **kwargs})

        return SimpleNamespace(
            guild=guild or _SettingsGuild(), author=SimpleNamespace(id=99), channel=channel or SimpleNamespace(id=10),
            interaction=None, send=send,
        ), sent


TEMPLATE_TEXT = "Cheers {user}!"
NEW_MUTATIONS = (
    {"birthday_action": "template_add", "birthday_template_type": "birthday", "birthday_template": TEMPLATE_TEXT},
    {"birthday_action": "template_remove", "birthday_template_type": "birthday", "birthday_template_id": 1},
    {"birthday_action": "blacklist_user", "target_user_id": 7, "birthday_blocked": True},
    {"birthday_action": "blacklist_role", "target_role_candidates": ["Mods"], "birthday_blocked": True},
    {"birthday_action": "trusted", "target_role_candidates": ["Mods"], "birthday_block_message": True},
)
TEMPLATE_LIST = {"birthday_action": "template_list", "birthday_template_type": "birthday"}
FULL_TEMPLATES = {"birthday": [{"id": index, "template_text": f"t{index}", "enabled": 1} for index in range(1, 101)]}


class BirthdayTemplateCatalogTests(unittest.TestCase):

    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            *NEW_MUTATIONS,
            TEMPLATE_LIST,
            {"birthday_action": "template_add", "birthday_template_type": "server_anniversary", "birthday_template": "x" * 1024},
            {"birthday_action": "template_remove", "birthday_template_type": "member_anniversary", "birthday_template_id": 2**40},
            {"birthday_action": "blacklist_user", "target_user_candidates": ["someone"], "birthday_blocked": False},
            {"birthday_action": "blacklist_role", "target_role_candidates": ["Mods"], "birthday_blocked": False},
            {"birthday_action": "trusted"},
            {
                "birthday_action": "trusted", "target_role_candidates": ["Mods"], "birthday_block_message": False,
                "birthday_block_role": True, "birthday_block_list": None,
            },
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("BIRTHDAY", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        for operands, predicate in (
            ({"birthday_action": "template_list", "birthday_template_type": "join"}, "task_operand_enum_invalid"),
            ({"birthday_action": "template_list", "birthday_template_type": "birthday_default"}, "task_operand_enum_invalid"),
            ({"birthday_action": "template_add", "birthday_template": "x" * 1025}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_add", "birthday_template": 5}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_remove", "birthday_template_id": 0}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_remove", "birthday_template_id": "1"}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_remove", "birthday_template_id": True}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_remove", "birthday_template_id": 1.5}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "blacklist_user", "birthday_blocked": "yes"}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "blacklist_role", "birthday_blocked": 1}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "trusted", "birthday_block_message": 1}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "trusted", "birthday_block_list": "false"}, "task_operand_type_or_bounds_invalid"),
            ({"birthday_action": "template_add", "birthday_template_text": "x"}, "unknown_task_operand"),
            ({"birthday_action": "trusted", "trusted_role_id": 3}, "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("BIRTHDAY", operands), predicate)


class BirthdayTemplateSharedTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_strips_stores_and_returns_the_id(self) -> None:
        db = _FakeDb()
        self.assertEqual(await birthdays.add_birthday_template(db, 1, "birthday", "  Cheers {user}!  "), 1)
        self.assertEqual(await birthdays.add_birthday_template(db, 1, "server_anniversary", "Cake"), 1)
        self.assertEqual(db.template_ops, [("add", "birthday", "Cheers {user}!"), ("add", "server_anniversary", "Cake")])


    async def test_blacklist_and_trusted_persist_through_the_database_methods(self) -> None:
        db = _FakeDb()
        await birthdays.set_birthday_blacklist_user(db, 1, 7, True)
        await birthdays.set_birthday_blacklist_user(db, 1, 7, False)
        await birthdays.set_birthday_blacklist_role(db, 1, 302, True)
        await birthdays.set_birthday_blacklist_role(db, 1, 302, False)
        self.assertEqual(db.blacklist_ops, [("add_user", 7), ("remove_user", 7), ("add_role", 302), ("remove_role", 302)])
        updated = await birthdays.set_birthday_trusted(db, 1, 302, True, False, True)
        self.assertEqual(
            db.settings_updates,
            [{"trusted_role_id": 302, "trusted_prevent_message": True, "trusted_prevent_role": False, "trusted_prevent_list": True}],
        )
        self.assertEqual(updated["trusted_role_id"], 302)
        await birthdays.set_birthday_trusted(db, 1, None, False, False, False)
        self.assertIsNone(db.settings["trusted_role_id"])


class BirthdayTemplateCommandParityTests(unittest.IsolatedAsyncioTestCase):
    _cog = staticmethod(BirthdaySettingsCommandParityTests._cog)
    _ctx = staticmethod(BirthdaySettingsCommandParityTests._ctx)

    async def test_templateadd(self) -> None:
        db = _FakeDb()
        ctx, sent = self._ctx()
        with patch.object(birthdays, "add_birthday_template", wraps=birthdays.add_birthday_template) as shared:
            await BirthdaysCog.birthday_template_add.callback(self._cog(db), ctx, event_type="birthday", template_text=" Hi {user} ")
        shared.assert_called_once()
        self.assertEqual([item["content"] for item in sent], ["Template added (ID `1`) for `birthday`."])
        self.assertEqual(db.template_ops, [("add", "birthday", "Hi {user}")])
        for event_type, text, db, message in (
            ("birthday", "   ", _FakeDb(), "Template text cannot be empty."),
            ("join", "hello", _FakeDb(), "Invalid event type."),
            ("birthday", "hello", _FakeDb(templates=FULL_TEMPLATES), "Template limit reached (100)."),
        ):
            with self.subTest(message=message):
                ctx, sent = self._ctx()
                await BirthdaysCog.birthday_template_add.callback(self._cog(db), ctx, event_type=event_type, template_text=text)
                self.assertEqual([item["content"] for item in sent], [message])
                self.assertEqual(db.writes(), 0)


    async def test_trusted_renders_the_same_embed(self) -> None:
        db = _FakeDb()
        ctx, sent = self._ctx()
        with patch.object(birthdays, "set_birthday_trusted", wraps=birthdays.set_birthday_trusted) as shared:
            await BirthdaysCog.birthday_trusted.callback(
                self._cog(db), ctx, role=SimpleNamespace(id=302, mention="<@&302>"),
                prevent_message=True, prevent_role=False, prevent_list=True,
            )
        shared.assert_called_once()
        embed = sent[0]["embed"]
        self.assertEqual(embed.title, "Trusted role settings")
        self.assertEqual(
            [(field.name, field.value) for field in embed.fields],
            [("Role", "<@&302>"), ("Prevent message", "True"), ("Prevent role", "False"), ("Prevent list", "True")],
        )
        cleared_ctx, cleared_sent = self._ctx()
        await BirthdaysCog.birthday_trusted.callback(
            self._cog(db), cleared_ctx, role=None, prevent_message=False, prevent_role=False, prevent_list=False,
        )
        self.assertEqual(cleared_sent[0]["embed"].fields[0].value, "None")
        self.assertIsNone(db.settings["trusted_role_id"])


if __name__ == "__main__":
    unittest.main()
