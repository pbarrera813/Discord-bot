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
    build_capability_execution_plan,
    normalize_capability_plan,
    validate_native_capability_contract,
)
from tests.test_ai_tagging import _CaptureChannel, _DummyGuild, _DummyMessage, _async_return


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

    async def test_native_repeated_web_tasks_keep_task_local_queries_and_results(self) -> None:
        payload = {
            "objective": "research two independent questions",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "CHAT",
            "requirements": [
                {"requirement_id": "r_a", "objective": "alpha", "satisfaction_criterion": "answer", "required": True, "capability_owner": "WEB", "owner_task_id": "web_a", "depends_on": []},
                {"requirement_id": "r_b", "objective": "beta", "satisfaction_criterion": "answer", "required": True, "capability_owner": "WEB", "owner_task_id": "web_b", "depends_on": []},
            ],
            "capabilities": [
                {"name": "WEB", "task_id": "web_a", "required": True, "evidence_span": "alpha", "depends_on": [], "scope": "BROAD_RESEARCH", "independent": True, "independent_research": True, "selection_count": 1, "owner_action": None, "operands": {"query": "alpha"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["answer"]},
                {"name": "WEB", "task_id": "web_b", "required": True, "evidence_span": "beta", "depends_on": [], "scope": "BROAD_RESEARCH", "independent": True, "independent_research": True, "selection_count": 1, "owner_action": None, "operands": {"query": "beta"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["answer"]},
            ],
        }
        diagnostic: list[dict[str, object]] = []
        self.assertIsNone(validate_native_capability_contract({"capability_plan": payload}, diagnostic_sink=diagnostic), diagnostic)
        plan = normalize_capability_plan(payload, action="CHAT")
        self.assertTrue(plan.valid, plan.failure_reason)
        execution_plan = build_capability_execution_plan(plan, fallback_action="CHAT")
        cog = AIChatCog.__new__(AIChatCog)
        seen: list[str] = []

        async def web(_message, prompt, **_kwargs):
            seen.append(prompt)
            return CapabilityTerminalResult(
                "WEB", CapabilityTerminalStatus.SUCCESS, "web", "SUCCESS",
                payload={"answer": prompt}, satisfies_request=True,
            )

        cog._execute_web_result_only = web
        cog._admin_actions = SimpleNamespace()
        context = await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "global prompt must not select either task",
            execution_plan,
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual(seen, ["alpha", "beta"])
        self.assertEqual(context.results["web_a"].payload["answer"], "alpha")
        self.assertEqual(context.results["web_b"].payload["answer"], "beta")

    async def test_native_repeated_memory_tasks_keep_task_local_keys_and_subjects(self) -> None:
        payload = {
            "objective": "look up two independent memories",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "SERVER_MEMORY_LOOKUP",
            "requirements": [
                {"requirement_id": "r_a", "objective": "alpha memory", "satisfaction_criterion": "entry", "required": True, "capability_owner": "MEMORY", "owner_task_id": "mem_a", "depends_on": []},
                {"requirement_id": "r_b", "objective": "beta memory", "satisfaction_criterion": "entry", "required": True, "capability_owner": "MEMORY", "owner_task_id": "mem_b", "depends_on": []},
            ],
            "capabilities": [
                {"name": "MEMORY", "task_id": "mem_a", "required": True, "evidence_span": "alpha", "depends_on": [], "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "owner_action": "SERVER_MEMORY_LOOKUP", "operands": {"memory_action": "SERVER_MEMORY_LOOKUP", "memory_type": "SERVER_CONTEXT", "subject": "alpha", "key": "one"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
                {"name": "MEMORY", "task_id": "mem_b", "required": True, "evidence_span": "beta", "depends_on": [], "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "owner_action": "SERVER_MEMORY_LOOKUP", "operands": {"memory_action": "SERVER_MEMORY_LOOKUP", "memory_type": "SERVER_CONTEXT", "subject": "beta", "key": "two"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
            ],
        }
        diagnostic: list[dict[str, object]] = []
        self.assertIsNone(validate_native_capability_contract({"capability_plan": payload}, diagnostic_sink=diagnostic), diagnostic)
        plan = normalize_capability_plan(payload, action="SERVER_MEMORY_LOOKUP")
        self.assertTrue(plan.valid, plan.failure_reason)
        execution_plan = build_capability_execution_plan(plan, fallback_action="SERVER_MEMORY_LOOKUP")
        cog = AIChatCog.__new__(AIChatCog)
        seen: list[dict[str, object]] = []

        async def memory(_message, decision, **_kwargs):
            seen.append(dict(decision.memory or {}))
            return CapabilityTerminalResult(
                "MEMORY", CapabilityTerminalStatus.SUCCESS, "memory", "SUCCESS",
                payload=dict(decision.memory or {}), satisfies_request=True,
            )

        cog._execute_server_memory_action = memory
        cog._admin_actions = SimpleNamespace()
        context = await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "global memory prompt must not select either task",
            execution_plan,
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual([item["subject"] for item in seen], ["alpha", "beta"])
        self.assertEqual([item["key"] for item in seen], ["one", "two"])
        self.assertEqual(context.results["mem_a"].payload["subject"], "alpha")
        self.assertEqual(context.results["mem_b"].payload["subject"], "beta")


    async def test_image_edit_consumes_bound_generated_artifact(self) -> None:
        payload = {
            "objective": "generate and edit an image",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "EDIT_IMAGE",
            "requirements": [
                {"requirement_id": "r_generate", "objective": "generate source", "satisfaction_criterion": "artifact", "required": True, "capability_owner": "IMAGE_GENERATE", "owner_task_id": "generate", "depends_on": []},
                {"requirement_id": "r_edit", "objective": "edit source", "satisfaction_criterion": "artifact", "required": True, "capability_owner": "IMAGE_EDIT", "owner_task_id": "edit", "depends_on": ["r_generate"]},
            ],
            "capabilities": [
                {"name": "IMAGE_GENERATE", "task_id": "generate", "required": True, "evidence_span": "generate", "depends_on": [], "scope": "IMAGE_GENERATION", "independent": True, "independent_research": False, "owner_action": None, "operands": {"prompt": "a source"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["artifact"]},
                {"name": "IMAGE_EDIT", "task_id": "edit", "required": True, "evidence_span": "edit", "depends_on": ["generate"], "scope": "IMAGE_EDIT", "independent": False, "independent_research": False, "owner_action": None, "operands": {"prompt": "make it blue"}, "input_bindings": [{"upstream_task_id": "generate", "output_field": "artifact"}], "source_refs": [], "requested_outputs": ["artifact"]},
            ],
        }
        plan = normalize_capability_plan(payload, action="EDIT_IMAGE")
        self.assertTrue(plan.valid, plan.failure_reason)
        execution_plan = build_capability_execution_plan(plan, fallback_action="EDIT_IMAGE")
        cog = AIChatCog.__new__(AIChatCog)
        seen: list[str | None] = []

        async def generate(_prompt):
            return cog._image_artifact_terminal_result(
                "IMAGE_GENERATE", b"source-bytes", source="test", filename="source.png"
            )

        async def edit(_prompt, source_url, **_kwargs):
            seen.append(source_url)
            return CapabilityTerminalResult(
                "IMAGE_EDIT", CapabilityTerminalStatus.SUCCESS, "image", "SUCCESS",
                payload={"artifact": b"edited"}, satisfies_request=True,
            )

        cog._execute_image_generation_result_only = generate
        cog._execute_image_edit_result_only = edit
        cog._admin_actions = SimpleNamespace()
        context = await cog._execute_live_media_composition_result_only(
            SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2)),
            "global image prompt",
            execution_plan,
            settings=SimpleNamespace(server_context=""),
        )
        self.assertEqual(len(seen), 1)
        self.assertTrue(str(seen[0]).startswith("data:image/png;base64,"))
        self.assertEqual(context.results["edit"].status, CapabilityTerminalStatus.SUCCESS)

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

    async def test_on_message_native_tasks_keep_distinct_operands(self) -> None:
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM:
            def __init__(self) -> None:
                self.chat_calls: list[dict[str, object]] = []

            async def route_ai_interaction(self, **_kwargs):
                return {
                    "participation": "RESPOND",
                    "action": "MINECRAFT_SERVER_STATUS",
                    "participation_confidence": 1.0,
                    "action_confidence": 1.0,
                    "reason_code": "DIRECT_REQUEST",
                    "capability_plan": {
                        "objective": "check both typed servers",
                        "semantic_source": "router",
                        "semantic_status": "SUCCESS",
                        "primary_action": "MINECRAFT_SERVER_STATUS",
                        "requirements": [
                            {"requirement_id": "a", "objective": "one", "satisfaction_criterion": "status", "required": True, "capability_owner": "MINECRAFT", "owner_task_id": "mc_a"},
                            {"requirement_id": "b", "objective": "two", "satisfaction_criterion": "status", "required": True, "capability_owner": "MINECRAFT", "owner_task_id": "mc_b"},
                        ],
                        "capabilities": [
                            {"name": "MINECRAFT", "task_id": "mc_a", "required": True, "evidence_span": "one", "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "owner_action": "MINECRAFT_SERVER_STATUS", "operands": {"address": "one.example"}, "input_bindings": [], "depends_on": [], "source_refs": [], "requested_outputs": ["result"]},
                            {"name": "MINECRAFT", "task_id": "mc_b", "required": True, "evidence_span": "two", "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "owner_action": "MINECRAFT_SERVER_STATUS", "operands": {"address": "two.example"}, "input_bindings": [], "depends_on": [], "source_refs": [], "requested_outputs": ["result"]},
                        ],
                    },
                    "valid": True,
                    "failure": False,
                }

            async def chat(self, **kwargs):
                self.chat_calls.append(kwargs)
                return "combined"

        class _Minecraft:
            def __init__(self) -> None:
                self.addresses: list[str] = []

            async def get_status(self, address: str):
                self.addresses.append(address)
                return {"online": True, "version": "1.21", "players": {"online": 1, "max": 20}}

        llm = _LLM()
        minecraft = _Minecraft()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(user=bot_user, application_id=1234, db=db, llm_client=llm, mc_client=minecraft, is_owner_user=lambda _user: False))
        message = _DummyMessage(attachments=[], content="<@42> check both typed servers", mentions=[bot_user])
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        await cog.on_message(message)

        self.assertEqual(minecraft.addresses, ["one.example", "two.example"], message.replies)
        self.assertEqual(len(llm.chat_calls), 1)
        self.assertEqual(message.replies, [("combined", True)])

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
