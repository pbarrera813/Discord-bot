from __future__ import annotations


import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.ai_chat import AIChatCog
from services.admin_actions import AdminActionResult, AdminActionService
from services.ai_capability_orchestrator import (
    CapabilityExecutionPlan,
    CapabilityRequest,
    CapabilityTerminalStatus,
    CapabilityTerminalResult,
    InputBinding,
)


class _TaskExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _plan(self, *tasks: CapabilityRequest) -> CapabilityExecutionPlan:
        semantic_plan = SimpleNamespace(
            semantic_source="router",
            semantic_status="SUCCESS",
            semantic_failure_reason=None,
            valid=True,
            capabilities=tasks,
            requirements=(),
        )
        return CapabilityExecutionPlan(
            semantic_plan=semantic_plan,
            ordered_capabilities=tasks,
            dispatch_action="CHAT",
        )

    async def test_native_tasks_dispatch_their_own_operands(self) -> None:
        cog = AIChatCog.__new__(AIChatCog)
        seen: list[str] = []

        async def status(operands):
            seen.append(str(operands.get("address")))
            return CapabilityTerminalResult(
                "MINECRAFT",
                CapabilityTerminalStatus.SUCCESS,
                "test",
                "SUCCESS",
                payload={"address": operands["address"]},
                satisfies_request=True,
            )

        cog._execute_minecraft_status_result_only = status
        tasks = (
            CapabilityRequest("MINECRAFT", task_id="mc_a", operands={"address": "one.example"}),
            CapabilityRequest("MINECRAFT", task_id="mc_b", operands={"address": "two.example"}),
        )
        context = await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "global prompt must not select either task",
            self._plan(*tasks),
            settings=SimpleNamespace(server_context=""),
        )

        self.assertEqual(seen, ["one.example", "two.example"])
        self.assertEqual(set(context.results), {"mc_a", "mc_b"})


    async def test_missing_native_task_operands_fail_closed_without_global_projection(self) -> None:
        cog = AIChatCog.__new__(AIChatCog)
        called = False

        async def status(_operands):
            nonlocal called
            called = True
            return CapabilityTerminalResult("MINECRAFT", CapabilityTerminalStatus.SUCCESS, "test", "SUCCESS")

        cog._execute_minecraft_status_result_only = status
        task = CapabilityRequest("MINECRAFT", task_id="mc_a", operands={})
        plan = self._plan(task)
        plan.semantic_plan.minecraft = {"address": "legacy.example"}
        context = await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "legacy.example",
            plan,
            settings=SimpleNamespace(server_context=""),
        )

        self.assertFalse(called)
        self.assertEqual(context.results["mc_a"].reason, "missing_minecraft_address")


    def _admin_message(self, *, channels: list[object], members: list[object] | None = None):
        actor = SimpleNamespace(
            id=10,
            mention="<@10>",
            display_name="actor",
            guild_permissions=SimpleNamespace(administrator=True, manage_channels=True, manage_roles=True),
            top_role=100,
        )
        bot_member = SimpleNamespace(
            id=42,
            guild_permissions=SimpleNamespace(administrator=True, manage_channels=True, manage_roles=True),
            top_role=100,
        )
        guild = SimpleNamespace(
            id=1,
            owner_id=10,
            me=bot_member,
            roles=[],
            text_channels=channels,
            members=members or [],
            get_member=lambda user_id: next((item for item in members or () if item.id == user_id), None),
            get_role=lambda role_id: next((item for item in getattr(guild, "roles", ()) if item.id == role_id), None),
        )
        channel = SimpleNamespace(id=900, name="response")
        message = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=actor,
            id=500,
            content="typed admin task",
            mentions=[],
            role_mentions=[],
            channel_mentions=[],
        )
        return message, guild, actor, bot_member

    def _admin_cog(self, *, message, action_log: list[str], fail_on: str | None = None, authorized: bool = True):
        cog = AIChatCog.__new__(AIChatCog)

        class _Actions:
            @staticmethod
            def _bot_member(guild):
                return guild.me

            @staticmethod
            def _actor_has(_actor, _permission):
                return authorized

            @staticmethod
            def _has_perm(_member, _permission):
                return True

            @staticmethod
            def _has_channel_perm(_member, _channel, _permission):
                return True

            @staticmethod
            def _can_manage_role(_guild, _actor, _role, _lang):
                return None

            @staticmethod
            def _can_act_on_target(_guild, _actor, _target, _lang):
                return None

            async def delete_channel(self, _guild, _actor, channel, **_kwargs):
                action_log.append(channel.name)
                if channel.name == fail_on:
                    return AdminActionResult(False, "failed", "delete_channel_http_error")
                return AdminActionResult(True, "deleted", "channel_deleted")

        cog._admin_actions = _Actions()
        return cog

    def _admin_task_plan(self, tasks: tuple[CapabilityRequest, ...]) -> CapabilityExecutionPlan:
        semantic_plan = SimpleNamespace(
            semantic_source="router",
            semantic_status="SUCCESS",
            semantic_failure_reason=None,
            valid=True,
            capabilities=tasks,
            requirements=(),
        )
        return CapabilityExecutionPlan(semantic_plan=semantic_plan, ordered_capabilities=tasks, dispatch_action="ADMIN_ACTION")

    def _real_admin_cog(self, message):
        cog = AIChatCog.__new__(AIChatCog)
        cog._admin_actions = AdminActionService(
            SimpleNamespace(db=None, is_owner_user=lambda _user: False)
        )
        return cog

    async def test_admin_batch_holds_all_four_deletions_when_fourth_target_is_invalid(self) -> None:
        channels = [SimpleNamespace(id=1, name="one"), SimpleNamespace(id=2, name="two"), SimpleNamespace(id=3, name="three")]
        message, _guild, _actor, _bot = self._admin_message(channels=channels)
        executed: list[str] = []
        cog = self._admin_cog(message=message, action_log=executed)
        tasks = tuple(
            CapabilityRequest(
                "ADMIN",
                task_id=f"delete_{index}",
                owner_action="ADMIN_ACTION",
                scope="DISCORD_ACTION",
                operands={"admin_action": "delete_channel", "target_channel": name},
                source_refs=(),
                requested_outputs=("result",),
            )
            for index, name in enumerate(("one", "two", "three", "missing"), 1)
        )
        context = await cog._execute_live_media_composition_result_only(
            message, "", self._admin_task_plan(tasks), settings=SimpleNamespace(server_context="")
        )
        self.assertEqual(executed, [])
        self.assertEqual(set(context.results), {"delete_1", "delete_2", "delete_3", "delete_4"})
        self.assertTrue(all(result.status == CapabilityTerminalStatus.NOT_EXECUTED for result in context.results.values()))
        self.assertTrue(all(result.reason == "mutation_batch_preflight_failed" for result in context.results.values()))


    async def test_admin_runtime_failure_stops_later_mutations_without_replay(self) -> None:
        channels = [SimpleNamespace(id=1, name="one"), SimpleNamespace(id=2, name="two"), SimpleNamespace(id=3, name="three")]
        message, _guild, _actor, _bot = self._admin_message(channels=channels)
        executed: list[str] = []
        cog = self._admin_cog(message=message, action_log=executed, fail_on="two")
        tasks = tuple(
            CapabilityRequest(
                "ADMIN",
                task_id=f"delete_{index}",
                owner_action="ADMIN_ACTION",
                scope="DISCORD_ACTION",
                operands={"admin_action": "delete_channel", "target_channel": name},
                source_refs=(),
                requested_outputs=("result",),
            )
            for index, name in enumerate(("one", "two", "three"), 1)
        )
        context = await cog._execute_live_media_composition_result_only(
            message, "", self._admin_task_plan(tasks), settings=SimpleNamespace(server_context="")
        )
        self.assertEqual(executed, ["one", "two"])
        self.assertEqual(context.results["delete_1"].status, CapabilityTerminalStatus.SUCCESS)
        self.assertTrue(context.results["delete_1"].satisfies_request)
        self.assertEqual(context.results["delete_2"].status, CapabilityTerminalStatus.PROVIDER_FAILURE)
        self.assertEqual(context.results["delete_3"].status, CapabilityTerminalStatus.NOT_EXECUTED)
        self.assertEqual(context.results["delete_3"].reason, "mutation_batch_fail_stop")


    async def test_admin_create_role_binding_assigns_exact_returned_id(self) -> None:
        role = SimpleNamespace(id=777, name="Light Blue", mention="<@&777>", managed=False)
        decoy = SimpleNamespace(id=778, name="Light Blue", mention="<@&778>", managed=False)
        member = SimpleNamespace(
            id=99,
            mention="<@99>",
            roles=[],
            guild_permissions=SimpleNamespace(administrator=False),
            top_role=1,
        )
        channel = SimpleNamespace(id=1, name="general")
        message, guild, actor, bot_member = self._admin_message(channels=[channel], members=[member])
        guild.roles = [decoy]
        # The created role may not be in the local cache yet.  Assignment must
        # fetch the exact returned ID, never select the same-name decoy.
        guild.get_role = lambda _role_id: None

        async def fetch_role(role_id: int):
            return role if role_id == 777 else None

        guild.fetch_role = fetch_role
        assigned: list[int] = []
        cog = AIChatCog.__new__(AIChatCog)

        class _Actions:
            @staticmethod
            def _bot_member(_guild):
                return bot_member

            @staticmethod
            def _actor_has(_actor, _permission):
                return True

            @staticmethod
            def _has_perm(_member, _permission):
                return True

            @staticmethod
            def _has_channel_perm(_member, _channel, _permission):
                return True

            @staticmethod
            def _can_manage_role(_guild, _actor, _role, _lang):
                return None

            @staticmethod
            def _can_act_on_target(_guild, _actor, _target, _lang):
                return None

            async def create_role(self, *_args, **_kwargs):
                return AdminActionResult(True, "created", "role_created", resource_handle={"resource_type": "role", "guild_id": 1, "resource_id": 777})

            async def add_role(self, _guild, _actor, _target, selected_role, **_kwargs):
                assigned.append(selected_role.id)
                return AdminActionResult(True, "assigned", "role_added")

        cog._admin_actions = _Actions()
        create = CapabilityRequest(
            "ADMIN", task_id="create", owner_action="ADMIN_ACTION", scope="DISCORD_ACTION", operands={"admin_action": "create_role", "role_name": "Light Blue"}, requested_outputs=("resource_handle",)
        )
        assign = CapabilityRequest(
            "ADMIN", task_id="assign", owner_action="ADMIN_ACTION", scope="DISCORD_ACTION", depends_on=("create",), input_bindings=(InputBinding("create", "resource_handle"),), operands={"admin_action": "add_role", "target_user_id": 99}
        )
        context = await cog._execute_live_media_composition_result_only(
            message, "", self._admin_task_plan((create, assign)), settings=SimpleNamespace(server_context="")
        )
        self.assertEqual(assigned, [777])
        self.assertTrue(context.bundle.satisfied)


    async def test_real_permission_change_after_preflight_denies_next_mutation(self) -> None:
        channels = []
        for index, name in enumerate(("one", "two", "three"), 1):
            channel = SimpleNamespace(id=index, name=name, deleted=False)

            async def delete(*, reason=None, channel=channel):
                channel.deleted = True

            channel.delete = delete
            channels.append(channel)
        message, _guild, actor, _bot_member = self._admin_message(channels=channels)
        cog = self._real_admin_cog(message)
        actions = cog._admin_actions
        original_delete = actions.delete_channel
        calls = 0

        async def delete_then_revoke(guild, current_actor, channel, **kwargs):
            nonlocal calls
            result = await original_delete(guild, current_actor, channel, **kwargs)
            calls += 1
            if calls == 1:
                current_actor.guild_permissions = SimpleNamespace(administrator=False, manage_channels=False)
            return result

        actions.delete_channel = delete_then_revoke
        tasks = tuple(
            CapabilityRequest(
                "ADMIN", task_id=f"delete_{index}", owner_action="ADMIN_ACTION", scope="DISCORD_ACTION",
                operands={"admin_action": "delete_channel", "target_channel": name},
                requested_outputs=("result",),
            )
            for index, name in enumerate(("one", "two", "three"), 1)
        )
        with patch("services.admin_actions.send_modlog_embed", new=AsyncMock()):
            context = await cog._execute_live_media_composition_result_only(
                message, "", self._admin_task_plan(tasks), settings=SimpleNamespace(server_context="")
            )
        self.assertEqual([channel.name for channel in channels if channel.deleted], ["one"])
        self.assertEqual(context.results["delete_1"].status, CapabilityTerminalStatus.SUCCESS)
        self.assertEqual(context.results["delete_2"].status, CapabilityTerminalStatus.PERMISSION_DENIED)
        self.assertEqual(context.results["delete_3"].status, CapabilityTerminalStatus.NOT_EXECUTED)


class _AdminResourceHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_created_channel_returns_typed_handle_when_logging_fails(self) -> None:
        actor = SimpleNamespace(
            id=10,
            mention="<@10>",
            guild_permissions=SimpleNamespace(administrator=True, manage_channels=True),
        )
        bot_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=True, manage_channels=True),
        )
        channel = SimpleNamespace(id=77, name="ops", mention="#ops")

        async def create_text_channel(_name, **_kwargs):
            return channel

        guild = SimpleNamespace(id=1, text_channels=[], me=bot_member, create_text_channel=create_text_channel)
        service = AdminActionService(SimpleNamespace(db=None, is_owner_user=lambda _user: False))
        with patch("services.admin_actions.send_modlog_embed", new=AsyncMock(side_effect=RuntimeError("log down"))):
            result = await service.create_text_channel(guild, actor, "Ops")

        self.assertTrue(result.success)
        self.assertEqual(
            result.resource_handle,
            {"resource_type": "channel", "guild_id": 1, "resource_id": 77},
        )

    async def test_created_role_returns_typed_handle_when_logging_fails(self) -> None:
        actor = SimpleNamespace(
            id=10,
            mention="<@10>",
            guild_permissions=SimpleNamespace(administrator=True, manage_roles=True),
            top_role=100,
        )
        bot_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=True, manage_roles=True),
            top_role=100,
        )
        role = SimpleNamespace(id=55, name="Readers", mention="<@&55>", managed=False)

        async def create_role(**_kwargs):
            return role

        guild = SimpleNamespace(id=1, roles=[], me=bot_member, create_role=create_role)
        service = AdminActionService(SimpleNamespace(db=None, is_owner_user=lambda _user: False))
        with patch("services.admin_actions.send_modlog_embed", new=AsyncMock(side_effect=RuntimeError("log down"))):
            result = await service.create_role(guild, actor, "Readers")

        self.assertTrue(result.success)
        self.assertEqual(
            result.resource_handle,
            {"resource_type": "role", "guild_id": 1, "resource_id": 55},
        )


if __name__ == "__main__":
    unittest.main()
