from __future__ import annotations


import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import (
    CapabilityExecutionPlan,
    CapabilityRequest,
    CapabilityTerminalStatus,
)
from services.ai_operation_catalog import known_output_fields, operation_effect, validate_operands
from services.server_memory import ServerMemoryService
from services.xai_client import XAIClient

LIFECYCLE = ("SERVER_MEMORY_LIST", "SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT")


class _MemoryDb:
    def __init__(self, rows: list[dict[str, object]] | None = None, *, fail: Exception | None = None) -> None:
        self.rows = {row["id"]: dict(row) for row in rows or []}
        self.fail = fail
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[tuple[int, int]] = []
        self.status_updates: list[dict[str, object]] = []
        self.deleted: list[int] = []

    async def expire_server_memories(self, guild_id, now_iso=None):
        return 0

    async def list_server_memories(
        self, guild_id, *, memory_type=None, status=None, subject_user_id=None, subject_channel_id=None, limit=100,
    ):
        if self.fail:
            raise self.fail
        self.list_calls.append({"guild_id": guild_id, "memory_type": memory_type, "status": status, "limit": limit})
        rows = [
            dict(row) for row in self.rows.values()
            if row["guild_id"] == guild_id
            and (memory_type is None or row["memory_type"] == memory_type)
            and (status is None or row["status"] == status)
        ]
        return rows[:limit]

    async def get_server_memory(self, guild_id, memory_id):
        if self.fail:
            raise self.fail
        self.get_calls.append((guild_id, memory_id))
        row = self.rows.get(memory_id)
        return dict(row) if row is not None and row["guild_id"] == guild_id else None

    async def update_server_memory_status(self, memory_id, *, guild_id, status, approved_by_user_id=None):
        row = self.rows.get(memory_id)
        if row is None or row["guild_id"] != guild_id:
            return False
        self.status_updates.append(
            {"memory_id": memory_id, "guild_id": guild_id, "status": status, "approved_by_user_id": approved_by_user_id}
        )
        row["status"] = status
        return True

    async def delete_server_memory(self, memory_id, *, guild_id):
        row = self.rows.get(memory_id)
        if row is None or row["guild_id"] != guild_id:
            return False
        self.deleted.append(memory_id)
        del self.rows[memory_id]
        return True

    def touched(self) -> int:
        return len(self.list_calls) + len(self.get_calls) + len(self.status_updates) + len(self.deleted)


def _row(memory_id: int, *, status: str = "pending", memory_type: str = "USER_NICKNAME", guild_id: int = 1,
         value: str = "Pepe", subject_user_id: int | None = 55) -> dict[str, object]:
    return {
        "id": memory_id, "guild_id": guild_id, "memory_type": memory_type, "key": f"key_{memory_id}", "value": value,
        "status": status, "subject_user_id": subject_user_id, "subject_channel_id": None, "metadata_json": "{}",
    }


def _message(*, manage_guild: bool = False, author_id: int = 99, guild_owner_id: int | None = None,
             administrator: bool = False, guild: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(
            id=author_id,
            guild_permissions=SimpleNamespace(manage_guild=manage_guild, administrator=administrator),
        ),
        guild=SimpleNamespace(id=1, owner_id=guild_owner_id) if guild else None,
        channel=SimpleNamespace(id=10),
    )


def _cog(db: _MemoryDb, *, bot_owner_ids: tuple[int, ...] = ()) -> AIChatCog:
    cog = AIChatCog.__new__(AIChatCog)
    cog.bot = SimpleNamespace(db=db, is_owner_user=lambda user: user.id in bot_owner_ids)
    cog._admin_actions = SimpleNamespace()
    cog._server_memory = ServerMemoryService(db)
    return cog


def _operands(action: str, **extra: object) -> dict[str, object]:
    return {"memory_action": action, **extra}


class MemoryLifecycleCatalogTests(unittest.TestCase):
    def test_valid_operands_are_accepted(self) -> None:
        for operands in (
            _operands("SERVER_MEMORY_LIST"),
            _operands("SERVER_MEMORY_LIST", memory_status="pending", memory_type="USER_NICKNAME"),
            _operands("SERVER_MEMORY_APPROVE", memory_id=3),
            _operands("SERVER_MEMORY_REJECT", memory_id=1),
        ):
            with self.subTest(operands=operands):
                self.assertIsNone(validate_operands("MEMORY", operands))

    def test_invalid_operands_are_rejected(self) -> None:
        for operands, predicate in (
            (_operands("SERVER_MEMORY_PURGE"), "task_operand_enum_invalid"),
            (_operands("SERVER_MEMORY_LIST", memory_status="deleted"), "task_operand_enum_invalid"),
            (_operands("SERVER_MEMORY_APPROVE", memory_id=0), "task_operand_type_or_bounds_invalid"),
            (_operands("SERVER_MEMORY_APPROVE", memory_id="3"), "task_operand_type_or_bounds_invalid"),
            (_operands("SERVER_MEMORY_APPROVE", memory_id=True), "task_operand_type_or_bounds_invalid"),
            (_operands("SERVER_MEMORY_APPROVE", memory_id=3, approver_user_id=5), "unknown_task_operand"),
            (_operands("SERVER_MEMORY_LIST", guild_id=1), "unknown_task_operand"),
        ):
            with self.subTest(operands=operands):
                self.assertEqual(validate_operands("MEMORY", operands), predicate)

    def test_effects_and_outputs(self) -> None:
        self.assertEqual(known_output_fields("MEMORY"), {"result"})
        for action, effect in (("SERVER_MEMORY_LIST", "read"), ("SERVER_MEMORY_APPROVE", "mutate"), ("SERVER_MEMORY_REJECT", "mutate")):
            with self.subTest(action=action):
                self.assertEqual(operation_effect(SimpleNamespace(name="MEMORY", operands=_operands(action))), effect)


class MemoryLifecycleAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    OPERANDS = {
        "SERVER_MEMORY_LIST": _operands("SERVER_MEMORY_LIST", memory_status="pending"),
        "SERVER_MEMORY_APPROVE": _operands("SERVER_MEMORY_APPROVE", memory_id=5),
        "SERVER_MEMORY_REJECT": _operands("SERVER_MEMORY_REJECT", memory_id=5),
    }

    async def test_non_admin_is_denied_before_any_database_effect(self) -> None:
        for action in LIFECYCLE:
            with self.subTest(action=action):
                db = _MemoryDb([_row(5)])
                result = await _cog(db)._execute_memory_lifecycle_result_only(_message(), self.OPERANDS[action])
                self.assertEqual(result.status, CapabilityTerminalStatus.PERMISSION_DENIED)
                self.assertFalse(result.satisfies_request)
                self.assertEqual((db.list_calls, db.status_updates), ([], []))

    async def test_manage_guild_administrator_guild_owner_and_bot_owner_are_allowed(self) -> None:
        cases = (
            ("manage_guild", _message(manage_guild=True), ()),
            ("administrator", _message(administrator=True), ()),
            ("guild_owner", _message(guild_owner_id=99), ()),
            ("bot_owner", _message(), (99,)),
        )
        for name, message, owners in cases:
            for action in LIFECYCLE:
                with self.subTest(author=name, action=action):
                    db = _MemoryDb([_row(5)])
                    result = await _cog(db, bot_owner_ids=owners)._execute_memory_lifecycle_result_only(
                        message, self.OPERANDS[action],
                    )
                    self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)

    async def test_the_subject_may_decide_a_personal_memory_about_themselves(self) -> None:
        for memory_type in ('USER_NICKNAME', 'USER_ALIAS', 'USER_PREFERENCE', 'FOOTBALL_PREFERENCE', 'SERVER_FACT', 'INSIDE_JOKE'):
            for action in ("SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT"):
                with self.subTest(memory_type=memory_type, action=action):
                    db = _MemoryDb([_row(5, memory_type=memory_type, subject_user_id=99)])
                    result = await _cog(db)._execute_memory_lifecycle_result_only(_message(), self.OPERANDS[action])
                    self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS, result.reason)
                    expected = "active" if action == "SERVER_MEMORY_APPROVE" else "rejected"
                    self.assertEqual(
                        [(update["status"], update["approved_by_user_id"]) for update in db.status_updates], [(expected, 99)],
                    )


class MemoryLifecycleExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, db: _MemoryDb, operands: dict[str, object], **message_kwargs: object):
        return await _cog(db)._execute_memory_lifecycle_result_only(_message(manage_guild=True, **message_kwargs), operands)

    async def test_list_is_bounded_filtered_and_truncated(self) -> None:
        rows = [_row(index, value="v" * 900) for index in range(1, 41)]
        rows += [_row(100, status="active"), _row(101, memory_type="SERVER_RULE")]
        db = _MemoryDb(rows)
        result = await self._run(db, _operands("SERVER_MEMORY_LIST", memory_status="pending", memory_type="USER_NICKNAME"))
        self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(result.satisfies_request)
        memories = result.payload["memories"]
        self.assertEqual(result.payload["operation"], "SERVER_MEMORY_LIST")
        self.assertEqual(len(memories), 25)
        self.assertTrue(all(item["status"] == "pending" and item["memory_type"] == "USER_NICKNAME" for item in memories))
        self.assertTrue({"id", "memory_type", "key", "value", "status"} <= set(memories[0]))
        self.assertLessEqual(len(memories[0]["value"]), 200)
        self.assertEqual(db.list_calls, [{"guild_id": 1, "memory_type": "USER_NICKNAME", "status": "pending", "limit": 25}])
        self.assertEqual(db.status_updates, [])


    async def test_approve_and_reject_happy_paths(self) -> None:
        for action, status_after in (("SERVER_MEMORY_APPROVE", "active"), ("SERVER_MEMORY_REJECT", "rejected")):
            with self.subTest(action=action):
                db = _MemoryDb([_row(5)])
                result = await self._run(db, _operands(action, memory_id=5))
                self.assertEqual(result.status, CapabilityTerminalStatus.SUCCESS)
                self.assertTrue(result.satisfies_request)
                self.assertEqual(result.payload, {"operation": action, "memory_id": 5, "status_after": status_after})
                self.assertEqual(
                    db.status_updates,
                    [{"memory_id": 5, "guild_id": 1, "status": status_after, "approved_by_user_id": 99}],
                )


    async def test_expired_pending_memory_cannot_be_approved_or_rejected(self) -> None:
        # Expiry is only swept lazily by LIST, so a pending row past its TTL must still be refused here.
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        db = _MemoryDb([{**_row(6), "expires_at": past}, {**_row(7), "expires_at": future}])
        for action in ("SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT"):
            with self.subTest(action=action):
                expired = await self._run(db, _operands(action, memory_id=6))
                self.assertEqual((expired.status, expired.reason), (CapabilityTerminalStatus.NO_DATA, "memory_not_pending"))
        self.assertEqual(db.status_updates, [])
        live = await self._run(db, _operands("SERVER_MEMORY_APPROVE", memory_id=7))
        self.assertEqual(live.status, CapabilityTerminalStatus.SUCCESS)


class MemoryLifecyclePreflightTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plan(*tasks: CapabilityRequest) -> CapabilityExecutionPlan:
        semantic_plan = SimpleNamespace(
            semantic_source="router", semantic_status="SUCCESS", semantic_failure_reason=None,
            valid=True, capabilities=tasks, requirements=(),
        )
        return CapabilityExecutionPlan(semantic_plan=semantic_plan, ordered_capabilities=tasks, dispatch_action="SERVER_MEMORY_APPROVE")

    @staticmethod
    def _task(task_id: str, action: str, **operands: object) -> CapabilityRequest:
        return CapabilityRequest(
            "MEMORY", task_id=task_id, owner_action=action, scope="STRUCTURED_DATA",
            operands=_operands(action, **operands), requested_outputs=("result",),
        )

    async def _run(self, db: _MemoryDb, message: SimpleNamespace, *tasks: CapabilityRequest):
        return await _cog(db)._execute_live_media_composition_result_only(
            message, "", self._plan(*tasks), settings=SimpleNamespace(server_context=""),
        )

    async def test_unauthorized_author_blocks_the_whole_batch_before_any_database_effect(self) -> None:
        db = _MemoryDb([_row(5), _row(6)])
        context = await self._run(
            db, _message(),
            self._task("one", "SERVER_MEMORY_APPROVE", memory_id=5),
            self._task("two", "SERVER_MEMORY_REJECT", memory_id=6),
        )
        self.assertEqual((db.list_calls, db.status_updates), ([], []))
        for task_id in ("one", "two"):
            self.assertEqual(context.results[task_id].status, CapabilityTerminalStatus.NOT_EXECUTED)
            self.assertEqual(context.results[task_id].reason, "mutation_batch_preflight_failed")


    async def test_valid_batch_executes_each_mutation_once(self) -> None:
        db = _MemoryDb([_row(5), _row(6)])
        context = await self._run(
            db, _message(manage_guild=True),
            self._task("one", "SERVER_MEMORY_APPROVE", memory_id=5),
            self._task("two", "SERVER_MEMORY_REJECT", memory_id=6),
        )
        self.assertEqual(
            [(update["memory_id"], update["status"]) for update in db.status_updates], [(5, "active"), (6, "rejected")],
        )
        self.assertTrue(all(result.status == CapabilityTerminalStatus.SUCCESS for result in context.results.values()))


def _memory_route(action: str, operands: dict[str, object]) -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": action,
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "SERVER_MEMORY_REQUEST", "resolved_request": "manage the pending memory",
        "send_text": True, "response_delivery": delivery, "memory": None,
        "capability_plan": {
            "objective": "manage the pending memory", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": action,
            "requirements": [{
                "requirement_id": "memory", "objective": "manage the pending memory", "required": True,
                "satisfaction_criterion": "The memory lifecycle step is done.",
                "capability_owner": "MEMORY", "owner_task_id": "memory_1", "depends_on": [],
            }],
            "capabilities": [{
                "task_id": "memory_1", "name": "MEMORY", "required": True,
                "evidence_span": "pending memory", "depends_on": [],
                "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False,
                "owner_action": action, "operands": {"memory_action": action, **operands}, "input_bindings": [],
                "source_refs": [], "requested_outputs": ["result"],
            }],
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    return XAIClient("test-key", "test-model")._validate_route_decision("", has_supported_image=False, parsed=route)



class MemorySubjectLinkTests(unittest.TestCase):
    def _link(self, subject, members, mentions=()):
        guild = SimpleNamespace(members=[
            SimpleNamespace(id=uid, name=name, display_name=display, global_name=None, bot=False)
            for uid, name, display in members
        ])
        message = SimpleNamespace(guild=guild, mentions=list(mentions), author=SimpleNamespace(id=1))
        return AIChatCog.__new__(AIChatCog)._memory_subject_user_id(message, None, {"subject": subject})

    def test_a_subject_that_is_exactly_one_member_links_to_that_member(self) -> None:
        members = [(500001, "jokester", "Joke"), (500002, "ana", "Ana")]

        self.assertEqual(self._link("Jokester", members), 500001)
        self.assertEqual(self._link("joke", members), 500001)
        self.assertEqual(self._link("<@500002>", members), 500002)
        self.assertEqual(self._link("500002", members), 500002)

    def test_unknown_ambiguous_or_missing_subjects_stay_unlinked(self) -> None:
        members = [(500001, "sam", "Sam"), (500002, "other", "sam")]

        self.assertIsNone(self._link("sam", members))
        self.assertIsNone(self._link("nobody", members))
        self.assertIsNone(self._link(None, members))
        self.assertIsNone(self._link("999999", members))


if __name__ == "__main__":
    unittest.main()
