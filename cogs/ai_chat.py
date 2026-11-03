from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from io import BytesIO
import hashlib
import json
import logging
import re
import time
from types import SimpleNamespace
import unicodedata
from typing import Any, Final
from urllib.parse import urlparse

import discord
from discord.ext import commands

from cogs.birthdays import (
    BIRTHDAY_TEMPLATE_LIMIT,
    BirthdayError,
    add_birthday_template,
    birthday_module_enabled,
    list_birthday_templates,
    next_birthdays,
    normalize_hex_color,
    preview_birthday_event,
    preview_channel,
    remove_birthday,
    remove_birthday_template,
    set_birthday,
    set_birthday_ages,
    set_birthday_blacklist_role,
    set_birthday_blacklist_user,
    set_birthday_event,
    set_birthday_mode,
    set_birthday_timezone,
    set_birthday_trusted,
    validate_birthday,
    validate_timezone,
    view_birthday,
    view_hidden_by_blacklist,
)
from cogs.fun import ReminderError, create_reminder, list_reminders, remove_reminder, validate_reminder
from services.admin_actions import AdminActionResult, AdminActionService, DISCORD_TIMEOUT_MAX_SECONDS
from services.ai_operation_catalog import (
    BIRTHDAY_EVENTS,
    BIRTHDAY_MODES,
    BIRTHDAY_SETTINGS_OPERANDS,
    BIRTHDAY_TEMPLATE_EVENTS,
    FOOTBALL_MUTATING_OPERATIONS,
    MEME_OPERATIONS,
    TOOL_DESCRIPTIONS,
    is_memory_lifecycle,
    canonical_operation_field,
    canonical_operations,
    known_output_fields,
    operand_descriptors,
    operation_effect,
    tool_definitions,
    validate_operands,
)
from services.api_ninjas import NinjasOperandError, execute_operation as execute_ninjas_operation
from services.glot import RUN_FILENAMES, execute_run, truncate_output
from services.football_analysis_context import _compact_fixture, _compact_standing
from services.football_formatter import fixture_score, fixture_status, fixture_teams
from services.football_live_match_service import FootballLiveMatchService
from services.football_operation_service import (
    FootballOperationService,
    FootballOutcome,
    FootballRetrievalResult,
)
from services.football_query_service import FootballQueryOperation, build_operation, compile_football_operation
from services.football_api_request_compiler import InvalidFootballApiRequest
from services.football_watch import (
    FootballWatchSnapshot,
    build_watch_updates,
    is_terminal_status,
    should_fetch_lineups,
    should_fetch_statistics,
    snapshot_from_fixture,
)
from services import football_resolver
from services.server_memory import MEMORY_TYPES, SELF_DECIDABLE_MEMORY_TYPES, ServerMemoryInput, ServerMemoryService
from services.server_memory_context import ServerMemoryContextBuilder
from services.voice_messages import (
    ALLOWED_TTS_TAGS,
    DiscordVoiceMessageSender,
    ResponseModality,
    VoiceAudioProcessor,
    VoiceResponseDecision,
    sanitize_tts_text,
)
from services.xai_client import ADMIN_ACTION_OPERATIONS, XAICompletionError, XAITTSAuthorizationError
from services.ai_capability_orchestrator import (
    CapabilityExecutionPlan,
    CapabilityExecutionContext,
    CapabilityPlan,
    CapabilityRequest,
    CapabilityRequirement,
    InputBinding,
    CapabilityResultBundle,
    CapabilityTerminalStatus,
    CapabilityTerminalResult,
    execute_ordered_capabilities,
    normalize_terminal_result,
    evidence_read_key,
    hash_text,
)
from utils.i18n import tr


MAX_RESULT_IMAGE_ARTIFACT_BYTES: Final[int] = 8 * 1024 * 1024
NOTHING_DONE_STATUSES = frozenset({
    CapabilityTerminalStatus.NOT_EXECUTED, CapabilityTerminalStatus.VALIDATION_FAILED,
    CapabilityTerminalStatus.PERMISSION_DENIED, CapabilityTerminalStatus.UNSUPPORTED,
})


def is_supported_image_url(url: object, content_type: object = None) -> bool:
    if str(content_type or "").split(";", 1)[0].strip().casefold() in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        return True
    parsed = urlparse(str(url or ""))
    if parsed.path.casefold().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return True
    return any(marker in parsed.query.casefold() for marker in ("format=jpg", "format=jpeg", "format=png", "format=webp", "name=large"))
ADMIN_CONFIG_CHANNEL_OPERATIONS = frozenset({"set_modlog", "ai_channel_add", "ai_channel_remove", "server_context_learn"})
ADMIN_CONFIG_OPERATIONS = ADMIN_CONFIG_CHANNEL_OPERATIONS | {"set_prefix", "set_language", "set_antispam", "set_antilink", "ai_channel_list", "ai_channel_clear", "server_context_view", "server_context_reset"}
ADMIN_COLOR_OPERATIONS = frozenset({"color_setup", "color_list", "color_publish", "color_reload", "color_add", "color_remove"})


@dataclass(frozen=True)
class ChatImageContext:
    urls: list[str]
    from_replied_message: bool
    reaction_target: object
    prompt_note: str = ""
    current_message_images: tuple[str, ...] = ()
    reply_target_images: tuple[str, ...] = ()
    prior_branch_images: tuple[str, ...] = ()
    prior_branch_source_kind: str = "none"
    prior_branch_source_message_id: int | None = None
    preferred_source_kind: str = "none"
    source_message_id: int | None = None
    current_message_direct_images: tuple[str, ...] = ()
    current_message_preview_images: tuple[str, ...] = ()
    reply_target_direct_images: tuple[str, ...] = ()
    reply_target_preview_images: tuple[str, ...] = ()
    current_message_id: int | None = None
    reply_target_message_id: int | None = None


@dataclass(frozen=True)
class RepliedMessageContext:
    note: str = ""
    image_urls: tuple[str, ...] = ()


@dataclass
class ContinuationLease:
    owner_user_id: int
    last_user_message_id: int | None
    last_bot_response_id: int | None
    expires_at: float
    last_action: str = "CHAT"
    resolved_request: str | None = None
    active_typed_context: tuple[CapabilityExecutionPlan, CapabilityResultBundle] | None = None


@dataclass
class FootballTurnContext:
    guild_id: int
    channel_id: int
    owner_user_id: int
    payload: dict[str, object]
    last_operation: str
    source_user_message_id: int | None
    source_assistant_message_id: int | None
    updated_at: float
    expires_at: float
    dormant: bool = False

    def to_prior_context(self) -> str:
        data = dict(self.payload)
        data["operation_type"] = data.get("operation_type") or self.last_operation
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:700]


@dataclass(frozen=True)
class RouteDecision:
    participation: str
    action: str
    participation_confidence: float
    action_confidence: float
    reason_code: str
    resolved_request: str | None = None
    target_message: int | None = None
    emoji: str | None = None
    emojis: tuple[str, ...] = ()
    send_text: bool = True
    response_delivery: dict[str, Any] | None = None
    pending_operation: str | None = None
    memory: dict[str, Any] | None = None
    admin: dict[str, Any] | None = None
    capability_plan: dict[str, Any] | None = None
    valid: bool = True
    failure: bool = False
    failure_reason: str | None = None


@dataclass
class PendingInteraction:
    guild_id: int
    channel_id: int
    owner_user_id: int
    target_message_id: int | None
    action: str
    route_decision: RouteDecision
    version: int
    created_at: float
    canceled: bool = False


@dataclass
class FootballLiveWatch:
    guild_id: int
    channel_id: int
    channel: object
    owner_user_id: int
    fixture_id: int
    fixture_label: str
    request: str
    started_at: float
    expires_at: float
    last_score: str
    last_status: str
    seen_event_keys: set[str]
    last_snapshot: FootballWatchSnapshot | None = None
    emitted_checkpoints: set[str] = field(default_factory=set)
    watch_message_ids: deque[int] = field(default_factory=lambda: deque(maxlen=80))
    lineups_fetched: bool = False
    task: asyncio.Task[None] | None = None
    canceled: bool = False


STRONG_ANCHORS: Final[set[str]] = {"DIRECT_MENTION", "REPLY_TO_AI", "NAME_AT_START", "REPLY_TO_WATCH"}
# Keep typed continuity available through ordinary pauses while retaining bounded freshness.
CONTINUATION_LEASE_SECONDS: Final[float] = 600.0
PENDING_INTERACTION_SECONDS: Final[float] = 90.0


class AIChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int, int], float] = {}
        self._cooldown_seconds = 4.0
        self._continuation_lease_seconds = CONTINUATION_LEASE_SECONDS
        self._continuation_leases: dict[tuple[int, int], ContinuationLease] = {}
        self._football_turn_contexts: dict[tuple[int, int, int], FootballTurnContext] = {}
        self._pending_interactions: dict[tuple[int, int, int, int | None], PendingInteraction] = {}
        self._pending_versions: dict[tuple[int, int, int, int | None], int] = {}
        self._football_live_watches: dict[tuple[int, int], FootballLiveWatch] = {}
        self._football_live_watch_poll_seconds = 30.0
        self._football_live_watch_max_seconds = 7200.0
        self._channel_human_activity: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(lambda: deque(maxlen=80))
        self._channel_ai_activity: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=40))
        self._channel_noisy_until: dict[tuple[int, int], float] = {}
        self._channel_noise_window_seconds = 20.0
        self._channel_noise_decay_seconds = 20.0
        self._conversation_history: dict[tuple[int, int], deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=120)
        )
        self._server_memory = (
            ServerMemoryService(self.bot.db)
            if hasattr(getattr(self.bot, "db", None), "list_server_memories")
            else None
        )
        self._server_memory_context = (
            ServerMemoryContextBuilder(self._server_memory)
            if self._server_memory is not None
            else None
        )
        self._admin_actions = AdminActionService(bot)
        settings = getattr(self.bot, "settings", None)
        self._history_entry_max_chars = 900
        self._chat_response_id_limit = 600
        self._chat_response_ids: deque[int] = deque(maxlen=self._chat_response_id_limit)
        self._chat_response_id_set: set[int] = set()
        self._voice_response_consumed_ids: deque[int] = deque(maxlen=600)
        self._voice_response_consumed_set: set[int] = set()
        self._voice_response_decision_ids: deque[int] = deque(maxlen=600)
        self._voice_response_decisions: dict[int, VoiceResponseDecision] = {}

    def cog_unload(self) -> None:
        for watch in list(self._football_live_watches.values()):
            watch.canceled = True
            if watch.task is not None:
                watch.task.cancel()
        self._football_live_watches.clear()

    async def _is_ai_allowed_in_channel(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> bool:
        if await self.bot.db.is_ai_channel_allowed(guild.id, channel.id):
            return True
        if isinstance(channel, discord.Thread) and channel.parent_id is not None:
            return await self.bot.db.is_ai_channel_allowed(guild.id, channel.parent_id)
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if getattr(message, "webhook_id", None) is not None:
            return
        self._record_channel_human_activity(message)

        lease_key = self._message_channel_key(message)
        self._invalidate_lease_for_intervening_human(message)
        self._invalidate_pending_for_intervening_human(message)

        settings = await self.bot.db.get_or_create_guild_settings(message.guild.id)
        prefix = settings.prefix
        lang = settings.language_code

        channel_allowed = await self._is_ai_allowed_in_channel(
            message.guild,
            message.channel,
        )
        if not channel_allowed:
            self._invalidate_owner_lease_for_rejection(message, "channel_not_allowed")
            return

        replied_message = await self._get_replied_message(message)

        if self._is_command_result_message(replied_message) and not self._message_mentions_bot_with_prompt(message):
            self._invalidate_owner_lease_for_rejection(message, "command_output_reply")
            return
        pending_interaction = self._get_pending_interaction(message)
        pending_context = self._is_pending_followup_candidate(message, pending_interaction)
        if message.content.startswith(prefix) and not pending_context:
            self._invalidate_owner_lease_for_rejection(message, "prefix_command")
            return
        is_command_context_message = self._is_slash_command_response_message(message)
        is_reply_to_ai = await self._is_reply_to_ai_message(message, replied_message)
        if pending_context:
            anchor_type = "PENDING_FOLLOWUP"
        else:
            anchor_type = await self._conversation_anchor_type(
                message,
                replied_message,
                is_reply_to_ai=is_reply_to_ai,
                prefix=prefix,
                settings=settings,
                is_command_context_message=is_command_context_message,
            )
        if anchor_type == "NONE":
            self._invalidate_owner_lease_for_rejection(message, "no_anchor")
            return
        if not self._allowed_by_throttle(message, anchor_type, pending_context=pending_context):
            self._invalidate_owner_lease_for_rejection(message, "cooldown")
            return

        user_prompt = self._extract_chat_prompt(message)
        if not user_prompt:
            self._invalidate_owner_lease_for_rejection(message, "empty_prompt")
            return
        convo_key = self._conversation_key(message.guild.id, message.channel.id)
        await self._ensure_history_loaded(convo_key, message.guild.id, message.channel.id)
        image_context = self._build_chat_image_context(message, replied_message)
        active_media_urls, active_source_kind, active_source_message_id = (
            self._authorized_active_media_for_message(message)
        )
        if active_media_urls:
            image_context = replace(
                image_context,
                prior_branch_images=active_media_urls,
                prior_branch_source_kind=active_source_kind,
                prior_branch_source_message_id=active_source_message_id,
                preferred_source_kind=(
                    "current_message"
                    if image_context.current_message_direct_images
                    else "reply_target"
                    if image_context.reply_target_direct_images
                    else active_source_kind
                ),
                source_message_id=(
                    image_context.current_message_id
                    if image_context.current_message_direct_images
                    else image_context.reply_target_message_id
                    if image_context.reply_target_direct_images
                    else active_source_message_id
                ),
            )
        await self._run_tool_turn(
            message, settings=settings, lang=lang, anchor_type=anchor_type, replied_message=replied_message,
            is_reply_to_ai=is_reply_to_ai, image_context=image_context, user_prompt=user_prompt,
            convo_key=convo_key, lease_key=lease_key,
        )

    async def _run_tool_turn(
        self, message, *, settings, lang: str, anchor_type: str, replied_message, is_reply_to_ai: bool,
        image_context: ChatImageContext, user_prompt: str, convo_key, lease_key,
    ) -> None:
        """Grok decides and answers in one native function-calling loop; code validates and executes each call."""
        replied_context = (
            self._build_replied_message_context(replied_message)
            if anchor_type in STRONG_ANCHORS and replied_message is not None
            else RepliedMessageContext()
        )
        llm_prompt = self._apply_image_context_note(self._apply_replied_message_context(user_prompt, replied_context), image_context)
        recent_messages = await self._channel_history_intent_context(message)
        if recent_messages:
            # Messages the bot was not part of are not in its conversation history; Grok needs them to know who is talking to whom.
            bot_id = getattr(self.bot.user, "id", None)
            llm_prompt += "\n\n[RECENT_CHANNEL_MESSAGES] oldest first, before this message:\n" + "\n".join(
                f"{'you' if item.get('author_id') == bot_id else item['author']}: {item['content']}" for item in recent_messages
            )
        image_sources = self._image_source_projection(image_context)
        if image_sources:
            llm_prompt += "\n\n[AVAILABLE_IMAGES] " + json.dumps(
                [{"ref": item["ref"], "source_kind": item["source_kind"]} for item in image_sources], ensure_ascii=False,
            )
        available_emojis = self._serialize_custom_emojis(message.guild)
        await self._remember_replied_ai_message(message, replied_message, convo_key, is_reply_to_ai=is_reply_to_ai)
        conversation_mode = self._conversation_mode_for_trigger(
            replied_message, is_active_followup=anchor_type == "SAME_USER_CONTINUATION", is_reply_to_ai=is_reply_to_ai,
        )
        if anchor_type == "SAME_USER_CONTINUATION":
            conversation_mode = "continuation"
        elif anchor_type == "NAME_REFERENCE":
            # Only the name appears: no mention, no reply. Grok decides whether it is being talked to.
            conversation_mode = "name"
        route_decision = RouteDecision("RESPOND", "CHAT", 1.0, 1.0, "DIRECT_REQUEST")
        pending_for_send = self._set_pending_interaction(
            message,
            route_decision=route_decision,
            target_message_id=self._route_target_message_id(message, route_decision, image_context, replied_message),
        )
        server_context = await self._server_context_with_memory(
            message, base_context=settings.server_context, route_action="CHAT", current_text=user_prompt,
            replied_message=replied_message, participants=recent_messages,
        )
        await message.channel.typing()
        reply_to_trigger = self._should_reply_to_trigger(message, is_direct_trigger=anchor_type in STRONG_ANCHORS)
        plan = CapabilityPlan(
            objective=user_prompt[:300], capabilities=(), primary_action="CHAT",
            response_delivery={}, football={},
        )
        execution_context = CapabilityExecutionContext(
            CapabilityExecutionPlan(semantic_plan=plan, ordered_capabilities=(), dispatch_action="CHAT")
        )
        control: dict[str, object] = {"silent": False, "artifacts": None}
        tools = tool_definitions() + self._native_search_tools()
        completed_changes: dict[str, str] = {}

        async def run_tools(calls: list[dict[str, Any]]) -> list[object]:
            outputs: dict[str, object] = {}
            batch: list[CapabilityRequest] = []
            requirements: list[CapabilityRequirement] = []
            for call in calls:
                call_id = str(call.get("call_id") or "")
                name = str(call.get("name") or "")
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except ValueError:
                    args = None
                if not isinstance(args, dict):
                    outputs[call_id] = {"status": "VALIDATION_FAILED", "reason": "arguments must be a JSON object"}
                elif name == "stay_silent":
                    control["silent"] = True
                    outputs[call_id] = {"status": "SUCCESS"}
                elif name == "deliver":
                    outputs[call_id] = self._apply_deliver_call(message, args, control)
                elif name.upper() not in TOOL_DESCRIPTIONS:
                    outputs[call_id] = {"status": "UNSUPPORTED", "reason": f"unknown function {name}"}
                else:
                    request, requirement, error = self._tool_call_request(name.upper(), call_id, args, execution_context)
                    if error is None and operation_effect(request) == "mutate":
                        # A change that already ran (or whose outcome is uncertain) is never repeated in the same turn.
                        key = hash_text(json.dumps([request.name, dict(request.operands)], sort_keys=True, default=str))
                        error = f"identical change already requested as task {completed_changes[key]}" if key in completed_changes else None
                        if error is None:
                            completed_changes[key] = call_id
                    if error is not None:
                        outputs[call_id] = {"task_id": call_id, "status": "VALIDATION_FAILED", "reason": error}
                    else:
                        batch.append(request)
                        requirements.append(requirement)
            if batch:
                current = execution_context.execution_plan
                semantic_plan = replace(
                    current.semantic_plan,
                    capabilities=current.semantic_plan.capabilities + tuple(batch),
                    requirements=current.semantic_plan.requirements + tuple(requirements),
                )
                execution_context.execution_plan = replace(
                    current, semantic_plan=semantic_plan, ordered_capabilities=current.ordered_capabilities + tuple(batch),
                )
                await self._execute_live_media_composition_result_only(
                    message, user_prompt, replace(execution_context.execution_plan, ordered_capabilities=tuple(batch)),
                    settings=settings, replied_message=replied_message, memory_lang=lang,
                    admin_lang=lang, football_anchor_type=anchor_type,
                    image_context=image_context, execution_context=execution_context,
                    can_continue=lambda: self._pending_can_send(pending_for_send),
                )
                # Grok judges fulfillment in its answer; a requirement is supported when its own task satisfied it.
                execution_context.requirement_assessments = {
                    task_id: {"status": "supported" if result.satisfies_request else "unsupported"}
                    for task_id, result in execution_context.results.items()
                }
                for request in batch:
                    result = execution_context.results.get(request.task_id)
                    if result is not None and result.status in NOTHING_DONE_STATUSES:
                        # Nothing was changed, so a corrected or repeated call may run.
                        for key in [key for key, task_id in completed_changes.items() if task_id == request.task_id]:
                            del completed_changes[key]
                    outputs[request.task_id] = (
                        self._tool_result_output(result)
                        if result is not None
                        else {"task_id": request.task_id, "status": "NOT_EXECUTED", "reason": "request superseded or cancelled"}
                    )
            return [outputs.get(str(call.get("call_id") or ""), {"status": "NOT_EXECUTED"}) for call in calls]

        try:
            reply = await self.bot.llm_client.chat(
                server_context=server_context,
                user_message=llm_prompt,
                author_name=message.author.display_name,
                channel_name=getattr(message.channel, "name", "unknown"),
                request_date=self._football_request_temporal_context(message)[0],
                channel_reference=self._channel_reference(message.channel),
                available_channels=self._serialize_text_channels(message.guild),
                available_emojis=available_emojis,
                conversation_history=await self._build_branch_conversation_history(
                    convo_key, message, replied_message,
                    lease=self._continuation_leases.get(lease_key), is_reply_to_ai=is_reply_to_ai,
                ),
                is_owner=self.bot.is_owner_user(message.author),
                conversation_mode=conversation_mode,
                image_urls=list(image_context.urls),
                reply_language=lang,
                author_permissions=self._author_permissions_text(message),
                tools=tools,
                run_tools=run_tools,
            )
        except Exception as exc:
            logging.exception("AI tool turn failure in guild=%s channel=%s", message.guild.id, message.channel.id)
            if self._pending_can_send(pending_for_send):
                await self._send_ai_failure_reply(
                    message, exc, lang=lang, image_urls=image_context.urls, reply_to_trigger=reply_to_trigger,
                )
            self._clear_pending_interaction(pending_for_send, event="error")
            return
        self._log_capability_terminal_checkpoint(message, execution_context)
        if not self._pending_can_send(pending_for_send):
            self._log_pending_event(message, "stale_suppressed", pending_for_send)
            return
        acted = any(result.satisfies_request for result in execution_context.results.values())
        if str(reply).strip() in {tool.get("name") for tool in tools}:
            # Grok wrote a function name as text instead of calling it; a bare function name is never a reply.
            reply, control["silent"] = "", True
        if control["silent"] and not acted:
            self._invalidate_owner_lease_for_rejection(message, "route_ignore")
            self._clear_pending_interaction(pending_for_send, event="stay_silent")
            return
        if not str(reply).strip():
            if not acted:
                await self._send_ai_failure_reply(
                    message, XAICompletionError("xAI API returned an empty completion."), lang=lang,
                    image_urls=image_context.urls, reply_to_trigger=reply_to_trigger,
                )
            self._clear_pending_interaction(pending_for_send, event="no_text_reply")
            return
        produced = [
            item.task_id for item in execution_context.execution_plan.ordered_capabilities
            if item.name in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"}
            and getattr(execution_context.results.get(item.task_id), "satisfies_request", False)
        ]
        chosen = produced if control["artifacts"] is None else [task_id for task_id in control["artifacts"] if task_id in produced]
        delivery_plan = replace(
            execution_context.execution_plan,
            semantic_plan=replace(
                execution_context.execution_plan.semantic_plan,
                response_delivery={"artifacts": [{"upstream_task_id": task_id, "output_field": "artifact"} for task_id in chosen]},
            ),
        )
        execution_context.execution_plan = delivery_plan
        await self._deliver_turn_reply(
            message, reply=str(reply), convo_key=convo_key, user_prompt=user_prompt, dispatch_action="CHAT",
            route_decision=route_decision, result_only_composition=True, execution_plan=delivery_plan,
            execution_context=execution_context, pending_for_send=pending_for_send, reply_to_trigger=reply_to_trigger,
            anchor_type=anchor_type,
        )

    def _native_search_tools(self) -> list[dict[str, str]]:
        """xAI runs these searches inside the same Grok request, so no second model call is made."""
        settings = getattr(self.bot, "settings", None)
        if not getattr(settings, "xai_web_search_enabled", False):
            return []
        return [{"type": "web_search"}] + ([{"type": "x_search"}] if getattr(settings, "xai_x_search_enabled", False) else [])

    @staticmethod
    def _tool_result_output(result: CapabilityTerminalResult) -> dict[str, object]:
        """What Grok reads back for one call: its status, why it failed, and its data without empty or internal fields."""
        def pruned(value):
            if isinstance(value, bytes):
                return None
            if isinstance(value, dict):
                value = {key: pruned(item) for key, item in value.items() if key != "request_plan"}
                return {key: item for key, item in value.items() if item not in (None, "", [], {})}
            if isinstance(value, (list, tuple)):
                return [item for item in map(pruned, value) if item not in (None, "", [], {})]
            return value

        payload, key, data = result.payload, "data", None
        if isinstance(payload, FootballRetrievalResult):
            plan = payload.requested_scope.get("request_plan")
            names = plan.get("output_payload") if isinstance(plan, dict) and plan.get("output_payload") else [
                item.name for item in dataclass_fields(payload) if item.name not in {
                    "outcome", "endpoints", "match_data", "football_entity_context",
                }
            ]
            data = {
                "ambiguity_candidates": payload.ambiguity_candidates[:5], "missing_inputs": payload.missing_inputs,
                **{name: getattr(payload, name) for name in names if isinstance(name, str) and hasattr(payload, name)},
                "entity_context": payload.football_entity_context, "requested_scope": payload.requested_scope,
                "resolved_entities": payload.resolved_entities, "notes": payload.notes,
            }
            if isinstance(data.get("standings_table"), list):
                data["standings_table"] = [_compact_standing(row) for row in data["standings_table"] if isinstance(row, dict)]
            if payload.requested_scope.get("operation_type") in {"fixture_next", "fixture_last"} and isinstance(data.get("fixtures"), list):
                data["fixtures"] = [_compact_fixture(item) for item in data["fixtures"] if isinstance(item, dict)]
        elif result.capability in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"} and isinstance(payload, dict):
            if isinstance(payload.get("artifact"), bytes) and payload["artifact"]:
                data = {"image": "done; it is shown with your reply, so do not mention attaching it", "filename": payload.get("filename")}
        elif result.capability in {"MEMORY", "ADMIN", "MINECRAFT", "NINJAS", "CODE", "REMINDER", "BIRTHDAY"} and isinstance(payload, dict):
            # Program output and text written by members can carry prompt injection.
            untrusted = (
                result.capability in {"CODE", "REMINDER"}
                or payload.get("operation") == "SERVER_MEMORY_LIST" or bool(payload.get("candidates"))
                or (result.capability == "BIRTHDAY" and payload.get("operation") in {"template_list", "event"})
                or (result.capability == "ADMIN" and payload.get("admin_action") in {
                    "user_info", "server_context_view", "color_list", "color_remove",
                })
            )
            key, data = ("untrusted_data" if untrusted else "data"), payload
        elif isinstance(payload, dict) and (payload.get("text") or payload.get("analysis")):
            data = {"analysis": payload.get("text") or payload.get("analysis")}

        output: dict[str, object] = {"task_id": result.task_id, "status": result.status.value}
        if result.native_outcome and result.native_outcome != result.status.value:
            output["outcome"] = result.native_outcome
        if result.reason:
            output["reason"] = result.reason
        if isinstance(payload, FootballRetrievalResult) and payload.missing_inputs:
            arguments = {
                "league": "league_candidates", "team": "team_candidates", "player": "player_candidates",
                "opponent": "team_candidates (both teams)",
            }
            output["reason"] = "missing required argument: " + ", ".join(
                dict.fromkeys(arguments.get(slot, slot) for slot in payload.missing_inputs)
            )
        data = pruned(data)
        if data not in (None, "", [], {}):
            serialized = json.dumps(data, ensure_ascii=False, default=str)
            output[key] = data if len(serialized) <= 12000 else serialized[:12000] + " [truncated]"
        return output

    @staticmethod
    def _tool_call_request(
        family: str, call_id: str, args: dict[str, Any], execution_context: CapabilityExecutionContext,
    ) -> tuple[CapabilityRequest | None, CapabilityRequirement | None, str | None]:
        descriptors = operand_descriptors(family)
        unknown = sorted(set(args) - set(descriptors) - {"purpose", "source_refs", "inputs"})
        if unknown:
            return None, None, f"unknown arguments {unknown}; allowed: {sorted(descriptors)}"
        operands = {key: value for key, value in args.items() if key in descriptors and value is not None}
        for field_name, value in operands.items():
            error = validate_operands(family, {field_name: value})
            if error:
                return None, None, f"{field_name}: {error}; expected {descriptors[field_name].json_schema()}"
        operation_field = canonical_operation_field(family)
        if operation_field and operands.get(operation_field) not in canonical_operations(family):
            return None, None, f"{operation_field} must be one of {sorted(canonical_operations(family))}"
        bindings: list[InputBinding] = []
        for item in args.get("inputs") or ():
            task_id = str((item or {}).get("task_id") or "")
            output_field = str((item or {}).get("output_field") or "")
            upstream = execution_context.results.get(task_id)
            if upstream is None:
                return None, None, f"inputs: no earlier result has task_id {task_id!r}"
            if output_field not in known_output_fields(upstream.capability):
                return None, None, f"inputs: {upstream.capability} outputs are {sorted(known_output_fields(upstream.capability))}"
            bindings.append(InputBinding(task_id, output_field))
        purpose = str(args.get("purpose") or "").strip()
        scopes = {
            "IMAGE_ANALYZE": "MEDIA_ANALYSIS",
            "IMAGE_GENERATE": "IMAGE_GENERATION", "MEME": "IMAGE_GENERATION", "IMAGE_EDIT": "IMAGE_EDIT",
            "ADMIN": "DISCORD_ACTION", "REACTION": "DISCORD_ACTION",
        }
        request = CapabilityRequest(
            name=family, task_id=call_id, required=True, evidence_span=purpose,
            depends_on=tuple(binding.upstream_task_id for binding in bindings),
            scope=scopes.get(family, "STRUCTURED_DATA"),
            selection_count=int(operands.get("selection_count") or 1), operands=operands,
            input_bindings=tuple(bindings), source_refs=tuple(str(ref) for ref in args.get("source_refs") or ()),
            requested_outputs=tuple(sorted(known_output_fields(family))),
        )
        if operation_effect(request) is None:
            return None, None, "unknown or missing operation"
        requirement = CapabilityRequirement(
            requirement_id=call_id, objective=purpose or evidence, capability_owner=family, owner_task_id=call_id,
        )
        return request, requirement, None

    def _apply_deliver_call(self, message, args: dict[str, Any], control: dict[str, object]) -> dict[str, object]:
        if isinstance(args.get("artifacts"), list):
            control["artifacts"] = [str(task_id) for task_id in args["artifacts"]]
        if args.get("voice"):
            delivery = {
                "modality": "VOICE", "explicit": True, "source": "CURRENT_MESSAGE",
                "evidence_span": str(args.get("voice_request_quote") or ""), "semantic_reason": "voice requested",
            }
            decision = self._validated_response_delivery_for_message(message, delivery)
            if decision.modality != ResponseModality.VOICE:
                return {"status": "VALIDATION_FAILED", "reason": f"voice_request_quote must be the exact words of the current message asking for audio ({decision.reason})"}
            self._remember_response_delivery_decision(
                message, RouteDecision("RESPOND", "CHAT", 1.0, 1.0, "DIRECT_REQUEST", response_delivery=delivery),
            )
        return {"status": "SUCCESS"}

    async def _remember_replied_ai_message(self, message, replied_message, convo_key, *, is_reply_to_ai: bool) -> None:
        if (
            replied_message is not None
            and self.bot.user is not None
            and replied_message.author.id == self.bot.user.id
            and is_reply_to_ai
            and replied_message.content.strip()
        ):
            cleaned_replied = replied_message.content
            if len(cleaned_replied) > self._history_entry_max_chars:
                cleaned_replied = cleaned_replied[: self._history_entry_max_chars - 3].rstrip() + "..."
            expected_prefixed = cleaned_replied
            already_in_history = any(
                item.get("role") == "assistant"
                and item.get("content") == expected_prefixed
                for item in self._conversation_history[convo_key]
            )
            if not already_in_history:
                prefixed = self._append_conversation_turn(
                    convo_key,
                    role="assistant",
                    speaker=self.bot.user.display_name,
                    content=replied_message.content.strip(),
                )
                if prefixed is not None:
                    await self._persist_conversation_turn(
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        role="assistant",
                        speaker=self.bot.user.display_name,
                        content=prefixed,
                        message_id=replied_message.id,
                    )

    async def _send_ai_failure_reply(self, message, exc: Exception, *, lang: str, image_urls, reply_to_trigger: bool) -> None:
        if self._is_ai_limit_error(exc):
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "Wow, I think that's a lot of talking from me right now, try later!",
                    "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                ),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
        elif self._is_empty_completion_error(exc):
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "Oops, my brain lagged for a second. Try again!",
                    "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                ),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
        elif image_urls and self._is_ai_image_access_error(exc):
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "I can see you attached an image, but this xAI model/API access is not enabled for image understanding. Ask an admin to set `XAI_MODEL` to a vision-capable model and confirm image input access.",
                    "Veo que adjuntaste una imagen, pero este modelo/acceso de xAI no esta habilitado para entender imagenes. Pide a un admin configurar `XAI_MODEL` con un modelo con vision y revisar el acceso a imagenes.",
                ),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
        else:
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "I hit an internal AI error. Please try again in a bit.",
                    "Tuve un error interno de IA. Intenta de nuevo en un momento.",
                ),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )

    async def _deliver_turn_reply(
        self, message, *, reply: str, convo_key, user_prompt: str, dispatch_action: str, route_decision: RouteDecision,
        result_only_composition: bool, execution_plan, execution_context: CapabilityExecutionContext, pending_for_send,
        reply_to_trigger: bool, anchor_type: str,
    ) -> None:
        if not self._pending_can_send(pending_for_send):
            self._log_pending_event(message, "stale_suppressed", pending_for_send)
            return
        prefixed_user = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=message.author.display_name,
            content=user_prompt,
        )
        if prefixed_user is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="user",
                speaker=message.author.display_name,
                content=prefixed_user,
                message_id=getattr(message, "id", None),
                author_user_id=getattr(message.author, "id", None),
                parent_message_id=self._parent_message_id(message),
                action_type=dispatch_action,
                resolved_request=route_decision.resolved_request,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        prefixed_bot = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=reply,
        )
        native_artifact_sinks: list[tuple[object, CapabilityTerminalResult, dict[str, object], bytes, str, str]] = []
        artifact_sinks_valid = True
        if result_only_composition and execution_plan.semantic_plan.semantic_source == "router":
            declared_sinks = execution_plan.semantic_plan.response_delivery.get("artifacts", ())
            if isinstance(declared_sinks, (list, tuple)):
                requests_by_task = {item.task_id: item for item in execution_plan.ordered_capabilities}
                used_delivery_filenames: set[str] = set()
                for index, sink in enumerate(declared_sinks, start=1):
                    if not isinstance(sink, dict):
                        artifact_sinks_valid = False
                        break
                    task_id = sink.get("upstream_task_id")
                    output_field = sink.get("output_field")
                    request = requests_by_task.get(task_id) if isinstance(task_id, str) else None
                    result = execution_context.results.get(task_id) if isinstance(task_id, str) else None
                    payload = result.payload if result is not None and isinstance(result.payload, dict) else None
                    artifact = payload.get(output_field) if payload is not None and isinstance(output_field, str) else None
                    if (
                        request is None
                        or request.name not in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"}
                        or output_field not in request.requested_outputs
                        or result is None
                        or result.task_id != request.task_id
                        or result.capability != request.name
                        or result.status != CapabilityTerminalStatus.SUCCESS
                        or not result.satisfies_request
                        or payload is None
                        or not isinstance(artifact, bytes)
                        or not 0 < len(artifact) <= MAX_RESULT_IMAGE_ARTIFACT_BYTES
                        or result.provider != ("memegen" if request.name == "MEME" else "xai_image")
                        or result.provenance.get("authorized") is False
                        or not (
                            request.name == "IMAGE_GENERATE"
                            and result.provenance.get("source") == "generate_image"
                            and result.provenance.get("execution_path") == "image_generate"
                            or request.name == "IMAGE_EDIT"
                            and result.provenance.get("source") == "edit_image"
                            and result.provenance.get("execution_path") == "direct_image_edit"
                            or request.name == "MEME"
                            and result.provenance.get("source") == "memegen"
                            and result.provenance.get("execution_path") in {"memegen_template", "memegen_random", "memegen_custom"}
                        )
                    ):
                        artifact_sinks_valid = False
                        break
                    producer_filename = str(payload.get("filename") or "nitori_image.png")
                    safe_task_id = re.sub(r"[^A-Za-z0-9_-]+", "_", request.task_id)[:32] or "task"
                    extension = producer_filename.rsplit(".", 1)[-1].lower() if "." in producer_filename else "png"
                    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
                        extension = "png"
                    delivery_filename = f"nitori_{index}_{safe_task_id}.{extension}"
                    if delivery_filename in used_delivery_filenames:
                        artifact_sinks_valid = False
                        break
                    used_delivery_filenames.add(delivery_filename)
                    content_type = str(payload.get("content_type") or "image/png")
                    if not content_type.casefold().startswith("image/"):
                        artifact_sinks_valid = False
                        break
                    native_artifact_sinks.append((request, result, payload, artifact, delivery_filename, content_type))
            else:
                artifact_sinks_valid = False
        delivery_files = [
            discord.File(BytesIO(artifact), filename=filename)
            for _request, _result, _payload, artifact, filename, _content_type in native_artifact_sinks
        ] if artifact_sinks_valid else []
        delivery_delete_after = next(
            (
                float(result.delivery_metadata["delete_after_seconds"])
                for result in execution_context.bundle.results
                if result.status == CapabilityTerminalStatus.SUCCESS
                and result.satisfies_request
                and "delete_after_seconds" in result.delivery_metadata
            ),
            None,
        )
        delete_trigger_on_success = any(
            result.status == CapabilityTerminalStatus.SUCCESS
            and result.satisfies_request
            and result.delivery_metadata.get("delete_trigger_on_success") is True
            for result in execution_context.bundle.results
        )
        artifact_receipts: dict[str, dict[str, object]] = {}
        artifact_receipt_set_complete = False

        def _record_artifact_receipts(sent_message: object) -> dict[str, dict[str, object]]:
            nonlocal artifact_receipt_set_complete
            attachments = list(getattr(sent_message, "attachments", ()) or ())
            faults: list[str] = []
            try:
                delivery_message_id = int(getattr(sent_message, "id", 0) or 0)
            except (TypeError, ValueError):
                delivery_message_id = 0
            if delivery_message_id <= 0:
                artifact_receipt_set_complete = False
                return dict(artifact_receipts)
            complete = len(attachments) == len(native_artifact_sinks) and delivery_message_id > 0
            if not complete:
                faults.append(f"attachment_count={len(attachments)}/{len(native_artifact_sinks)}")
            remaining = {sink[4]: sink for sink in native_artifact_sinks}
            seen_attachment_ids: set[int] = set()
            seen_attachment_urls: set[str] = set()
            for attachment in attachments:
                filename = getattr(attachment, "filename", None)
                sink = remaining.pop(filename, None) if isinstance(filename, str) else None
                if sink is None:
                    faults.append("unexpected_filename")
                    complete = False
                    continue
                request, _result, _payload, artifact, expected_filename, content_type = sink
                attachment_id = getattr(attachment, "id", None)
                attachment_url = getattr(attachment, "url", None)
                try:
                    attachment_size = int(getattr(attachment, "size", -1))
                    attachment_id = int(attachment_id)
                except (TypeError, ValueError):
                    faults.append("bad_id_or_size")
                    complete = False
                    continue
                try:
                    parsed_attachment_url = urlparse(attachment_url) if isinstance(attachment_url, str) else None
                except ValueError:
                    faults.append("bad_url")
                    complete = False
                    continue
                if (
                    attachment_id <= 0
                    or attachment_size != len(artifact)
                    or parsed_attachment_url is None
                    or parsed_attachment_url.scheme != "https"
                    or not parsed_attachment_url.hostname
                    or not is_supported_image_url(attachment_url, str(getattr(attachment, "content_type", "") or ""))
                    or getattr(attachment, "content_type", None) != content_type
                    or attachment_id in seen_attachment_ids
                    or attachment_url in seen_attachment_urls
                ):
                    faults.append(
                        f"mismatch size={attachment_size}/{len(artifact)} "
                        f"content_type={getattr(attachment, 'content_type', None)}/{content_type}"
                    )
                    complete = False
                    continue
                seen_attachment_ids.add(attachment_id)
                seen_attachment_urls.add(attachment_url)
                artifact_receipts[request.task_id] = {
                    "attachment_id": attachment_id,
                    "filename": expected_filename,
                    "size": attachment_size,
                    "url": attachment_url,
                    "message_id": delivery_message_id,
                }
            artifact_receipt_set_complete = bool(
                complete and not remaining and len(artifact_receipts) == len(native_artifact_sinks)
            )
            if faults:
                logging.warning("AI native artifact receipt faults message=%s faults=%s", delivery_message_id, ";".join(faults))
            return dict(artifact_receipts)

        try:
            sent_delivery = await self._send_long_reply(
                message,
                reply,
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
                files=delivery_files if artifact_sinks_valid else None,
                on_first_delivery=_record_artifact_receipts if delivery_files else None,
                delete_after=delivery_delete_after,
            )
        except Exception as exc:
            if delivery_files:
                receipt_summary = ",".join(
                    f"{task_id}:{receipt.get('message_id')}:{receipt.get('attachment_id')}:{receipt.get('size')}"
                    for task_id, receipt in sorted(artifact_receipts.items())
                ) or "none"
                logging.warning(
                    "AI native artifact delivery incomplete text_complete=false receipts=%s failure=%s",
                    receipt_summary,
                    type(exc).__name__,
                )
                self._invalidate_owner_lease_for_rejection(message, "image_attachment_delivery_incomplete")
                self._clear_pending_interaction(pending_for_send, event="image_attachment_delivery_incomplete")
                return
            raise
        if delete_trigger_on_success and sent_delivery is not None:
            try:
                await message.delete()
            except Exception:
                logging.warning(
                    "AI shared delivery trigger_delete success=false guild=%s channel=%s message=%s",
                    getattr(message.guild, "id", None),
                    getattr(message.channel, "id", None),
                    getattr(message, "id", None),
                )
            else:
                logging.info(
                    "AI shared delivery trigger_delete success=true guild=%s channel=%s message=%s",
                    getattr(message.guild, "id", None),
                    getattr(message.channel, "id", None),
                    getattr(message, "id", None),
                )
        sent_message_id = sent_delivery
        if delivery_files:
            try:
                sent_message_id = int(getattr(sent_delivery, "id", 0) or 0)
            except (TypeError, ValueError):
                sent_message_id = 0
        if prefixed_bot is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=prefixed_bot,
                message_id=sent_message_id,
                author_user_id=getattr(self.bot.user, "id", None),
                parent_message_id=getattr(message, "id", None),
                action_type=dispatch_action,
                resolved_request=route_decision.resolved_request,
            )
        native_sink_delivery = bool(
            result_only_composition and execution_plan.semantic_plan.semantic_source == "router"
            and execution_plan.semantic_plan.response_delivery.get("artifacts")
        )
        if native_sink_delivery:
            image_delivery_valid = bool(
                artifact_sinks_valid
                and artifact_receipt_set_complete
                and len(artifact_receipts) == len(native_artifact_sinks)
                and len(native_artifact_sinks) > 0
            )
        if native_sink_delivery and not (
            artifact_sinks_valid
            and artifact_receipt_set_complete
            and len(artifact_receipts) == len(native_artifact_sinks)
            and len(native_artifact_sinks) > 0
        ):
            logging.warning(
                "AI native artifact delivery unverified sinks_valid=%s receipts=%s/%s receipt_set_complete=%s",
                artifact_sinks_valid, len(artifact_receipts), len(native_artifact_sinks), artifact_receipt_set_complete,
            )
            self._invalidate_owner_lease_for_rejection(message, "image_attachment_delivery_unverified")
            self._clear_pending_interaction(pending_for_send, event="image_attachment_delivery_unverified")
            return
        self._log_capability_terminal_checkpoint(message, execution_context)
        if anchor_type != "REPLY_TO_WATCH":
            continuation_context = execution_context
            if native_sink_delivery:
                projected_results = dict(execution_context.results)
                for request, result, _payload, _artifact, _filename, _content_type in native_artifact_sinks:
                    receipt = artifact_receipts[request.task_id]
                    projected_payload = dict(result.payload) if isinstance(result.payload, dict) else {}
                    projected_payload["media_urls"] = (receipt["url"],)
                    projected_results[request.task_id] = replace(
                        result,
                        payload=projected_payload,
                        provenance={
                            **dict(result.provenance),
                            "authorized": True,
                            "source_kind": "generated_artifact" if request.name in {"IMAGE_GENERATE", "MEME"} else "edited_artifact",
                            "context_identity": hash_text(str(receipt["url"])),
                        },
                        scope={
                            **dict(result.scope),
                            "source_message_id": receipt["message_id"],
                            "media_count": 1,
                            "context_identity": hash_text(str(receipt["url"])),
                        },
                    )
                continuation_context = replace(execution_context, results=projected_results)
            self._publish_typed_continuation_context(
                message,
                last_bot_response_id=sent_message_id,
                action=dispatch_action,
                resolved_request=route_decision.resolved_request,
                execution_context=continuation_context,
            )
        if dispatch_action == "CHAT":
            self._mark_football_context_dormant(message)
        self._clear_pending_interaction(pending_for_send, event="success")

    @commands.hybrid_command(
        name="roast",
        description="Generate a playful roast using the server vibe context.",
    )
    @discord.app_commands.describe(target="Member to roast (default: you)")
    async def roast(
        self,
        ctx: commands.Context,
        target: discord.Member | None = None,
    ) -> None:
        guild = ctx.guild
        lang = await self._lang(guild)
        if target is None:
            await ctx.send(
                tr(
                    lang,
                    "Provide a target user for `/roast`, or use `/roastme`.",
                    "Proporciona un usuario objetivo para `/roast`, o usa `/roastme`.",
                )
            )
            return

        if guild is None:
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        await self._run_roast(
            ctx,
            target_member=target,
        )

    @commands.hybrid_command(
        name="roastme",
        description="Roast yourself using the server vibe context.",
    )
    async def roastme(self, ctx: commands.Context) -> None:
        await self._run_roast(
            ctx,
            target_member=ctx.author,
        )

    async def _run_roast(
        self,
        ctx: commands.Context,
        *,
        target_member: discord.Member,
    ) -> None:
        guild = ctx.guild
        lang = await self._lang(guild)
        if guild is None:
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in text channels or threads.",
                    "Este comando solo se puede usar en canales de texto o hilos.",
                )
            )
            return

        channel_allowed = await self._is_ai_allowed_in_channel(guild, channel)
        if not channel_allowed:
            await ctx.send(
                tr(
                    lang,
                    "AI commands are not enabled in this channel.",
                    "Los comandos de IA no estan habilitados en este canal.",
                )
            )
            return

        settings = await self.bot.db.get_or_create_guild_settings(guild.id)
        target = target_member.display_name
        mention = target_member.mention
        target_profile = await self._build_roast_target_profile(
            guild=guild,
            channel=channel,
            target=target_member,
            lang=lang,
        )
        prompt = tr(
            lang,
            (
                f"Roast target (and only this target): {target} [{mention}]. "
                "Write a playful roast in 5-7 sentences with strong humor and server-style references when possible. "
                "Do not roast the command author unless they are the target. "
                "Output only the roast content; no headers, no speaker labels, no 'Nitori:' prefixes. "
                f"Start by mentioning the target exactly once as {mention}. "
                "Use the profile details below to personalize the roast with concrete references.\n"
                f"{target_profile}"
            ),
            (
                f"Objetivo del roast (y solo ese objetivo): {target} [{mention}]. "
                "Escribe un roast jugueton en 5-7 frases con humor fuerte y referencias al estilo del servidor cuando se pueda. "
                "No roastees al autor del comando a menos que sea el objetivo. "
                "Devuelve solo el roast, sin encabezados ni etiquetas de hablante ni prefijos tipo 'Nitori:'. "
                f"Empieza mencionando al objetivo exactamente una vez como {mention}. "
                "Usa los detalles del perfil para personalizar el roast con referencias concretas.\n"
                f"{target_profile}"
            ),
        )

        convo_key = self._conversation_key(guild.id, channel.id)
        await self._ensure_history_loaded(convo_key, guild.id, channel.id)

        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                pass

        try:
            reply = await self.bot.llm_client.chat(
                server_context=settings.server_context,
                user_message=prompt,
                author_name=ctx.author.display_name,
                channel_name=getattr(channel, "name", "unknown"),
                channel_reference=self._channel_reference(channel),
                available_channels=self._serialize_text_channels(guild),
                available_emojis=self._serialize_custom_emojis(guild),
                conversation_history=self._build_conversation_history(convo_key),
                is_owner=self.bot.is_owner_user(ctx.author),
                conversation_mode="command",
                reply_language=lang,
            )
        except Exception as exc:
            if guild is not None:
                logging.exception("AI roast failure in guild=%s channel=%s", guild.id, channel.id)
            if self._is_ai_limit_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta mas tarde!",
                    )
                )
            elif self._is_empty_completion_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    )
                )
            else:
                await ctx.send(
                    tr(
                        lang,
                        "I hit an internal AI error. Please try again in a bit.",
                        "Tuve un error interno de IA. Intenta de nuevo en un momento.",
                    )
                )
            return

        reply = self._format_roast_reply(reply, mention)
        prefixed_user = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=ctx.author.display_name,
            content=prompt,
        )
        if prefixed_user is not None:
            await self._persist_conversation_turn(
                guild_id=guild.id,
                channel_id=channel.id,
                role="user",
                speaker=ctx.author.display_name,
                content=prefixed_user,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        prefixed_bot = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=reply,
        )
        if prefixed_bot is not None:
            await self._persist_conversation_turn(
                guild_id=guild.id,
                channel_id=channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=prefixed_bot,
            )
        await ctx.send(
            reply[:1900],
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )

    async def _build_roast_target_profile(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        target: discord.Member,
        lang: str,
    ) -> str:
        warnings = await self.bot.db.get_warnings(guild.id, target.id)
        warning_reasons: list[str] = []
        for item in warnings[:3]:
            reason = str(item.get("reason", "")).strip()
            if reason:
                warning_reasons.append(reason[:120])

        roles = [role.name for role in target.roles if not role.is_default()]
        role_preview = ", ".join(roles[-8:]) if roles else tr(lang, "none", "ninguno")

        recent_count, recent_lines = await self._collect_recent_target_messages(
            channel=channel,
            member_id=target.id,
            sample_limit=6,
            scan_limit=280,
        )
        recent_preview = (
            " | ".join(recent_lines)
            if recent_lines
            else tr(lang, "No recent text found in this channel.", "No se encontro texto reciente en este canal.")
        )

        warning_preview = (
            " | ".join(warning_reasons)
            if warning_reasons
            else tr(lang, "No warning reasons recorded.", "No hay razones de advertencia registradas.")
        )

        joined_ts = (
            f"<t:{int(target.joined_at.timestamp())}:R>"
            if target.joined_at is not None
            else tr(lang, "Unknown", "Desconocido")
        )
        created_ts = f"<t:{int(target.created_at.timestamp())}:R>"
        boost_text = tr(
            lang,
            "yes" if target.premium_since else "no",
            "si" if target.premium_since else "no",
        )

        return tr(
            lang,
            (
                f"Target mention: {target.mention}\n"
                f"Target ID: {target.id}\n"
                f"Display name: {target.display_name}\n"
                f"Username: {target.name}\n"
                f"Joined server: {joined_ts}\n"
                f"Joined Discord: {created_ts}\n"
                f"Server booster: {boost_text}\n"
                f"Roles: {role_preview}\n"
                f"Warnings: {len(warnings)}\n"
                f"Warning reasons sample: {warning_preview}\n"
                f"Recent text messages in this channel (count {recent_count}): {recent_preview}"
            ),
            (
                f"Mencion del objetivo: {target.mention}\n"
                f"ID del objetivo: {target.id}\n"
                f"Nombre visible: {target.display_name}\n"
                f"Usuario: {target.name}\n"
                f"Ingreso al servidor: {joined_ts}\n"
                f"Ingreso a Discord: {created_ts}\n"
                f"Booster del servidor: {boost_text}\n"
                f"Roles: {role_preview}\n"
                f"Advertencias: {len(warnings)}\n"
                f"Muestra de razones de advertencia: {warning_preview}\n"
                f"Mensajes recientes en este canal (cantidad {recent_count}): {recent_preview}"
            ),
        )

    async def _collect_recent_target_messages(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        member_id: int,
        sample_limit: int,
        scan_limit: int,
    ) -> tuple[int, list[str]]:
        total = 0
        samples: list[str] = []
        try:
            async for msg in channel.history(limit=scan_limit):
                if msg.author.id != member_id:
                    continue
                content = (msg.content or "").strip()
                if not content and msg.attachments:
                    content = "[attachment]"
                if not content:
                    continue
                normalized = " ".join(content.split())
                total += 1
                if len(samples) < sample_limit:
                    samples.append(normalized[:160])
                if total >= max(sample_limit * 4, 20):
                    break
        except (discord.Forbidden, discord.HTTPException):
            return 0, []
        return total, samples

    @staticmethod
    def _format_roast_reply(text: str, target_mention: str) -> str:
        cleaned = text.strip().strip("`")
        prefix_re = re.compile(
            r"^(?:\*\*)?(?:nitori(?:-buchona)?|bot)\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        while True:
            updated = prefix_re.sub("", cleaned, count=1).lstrip()
            if updated == cleaned:
                break
            cleaned = updated

        if not cleaned:
            return target_mention

        if cleaned.startswith(target_mention):
            doubled = f"{target_mention} {target_mention}"
            if cleaned.startswith(doubled):
                return f"{target_mention} {cleaned[len(doubled):].lstrip()}"
            return cleaned
        return f"{target_mention} {cleaned}"

    @commands.hybrid_command(
        name="translate",
        description="Translate replied text or provided text.",
    )
    @discord.app_commands.describe(language="Target language, for example en, es or French", text="Text to translate (default: the message you reply to)")
    async def translate_cmd(
        self,
        ctx: commands.Context,
        language: str,
        *,
        text: str | None = None,
    ) -> None:
        # Discord drops a slash command not acknowledged within 3 seconds, and Grok can take longer.
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                pass
        lang = await self._lang(ctx.guild)
        # Any language Grok can translate to; Grok also notices when the text is already in it.
        target = " ".join(language.split())[:40]
        if not target:
            await ctx.send(self._translate_usage_message(lang))
            return

        source_text = (text or "").strip()
        if not source_text and ctx.message is not None:
            replied = await self._get_replied_message(ctx.message)
            if replied is not None:
                source_text = replied.content.strip()

        if not source_text:
            await ctx.send(
                tr(
                    lang,
                    "Reply to a message and use `translate <language>`, or provide text after the language.",
                    "Responde a un mensaje y usa `translate <idioma>`, o agrega texto después del idioma.",
                )
            )
            return

        try:
            translated = await self.bot.llm_client.translate(
                text=source_text,
                target_language=target,
            )
        except Exception as exc:
            logging.exception("AI translate command failure in guild=%s", ctx.guild.id if ctx.guild else None)
            if self._is_ai_limit_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                    )
                )
            elif self._is_empty_completion_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    )
                )
            else:
                await ctx.send(
                    tr(
                        lang,
                        "I hit an internal AI error while translating. Please try again.",
                        "Tuve un error interno de IA al traducir. Intenta de nuevo.",
                    )
                )
            return

        await ctx.send(translated[:1900], allowed_mentions=discord.AllowedMentions.none())

    def _is_slash_command_response_message(self, message: discord.Message | None) -> bool:
        if message is None:
            return False
        if getattr(message, "interaction_metadata", None):
            return True
        application_id = getattr(message, "application_id", None)
        bot_application_id = getattr(self.bot, "application_id", None)
        if application_id is not None and bot_application_id is not None:
            try:
                return int(application_id) == int(bot_application_id)
            except (TypeError, ValueError):
                return application_id == bot_application_id
        return False

    def _is_command_result_message(self, message: discord.Message | None) -> bool:
        return self._is_slash_command_response_message(message) or self._is_bot_embed_message(message)

    def _is_bot_embed_message(self, message: discord.Message | None) -> bool:
        if message is None or self.bot.user is None:
            return False
        author = getattr(message, "author", None)
        if getattr(author, "id", None) != self.bot.user.id:
            return False
        return bool(getattr(message, "embeds", None))

    def _mentions_bot(self, message: discord.Message) -> bool:
        # Only a mention written in the text: a reply ping also lists the replied-to bot in message.mentions.
        return self.bot.user is not None and self.bot.user.id in getattr(message, "raw_mentions", ())

    def _message_mentions_bot_with_prompt(self, message: discord.Message) -> bool:
        return self._mentions_bot(message) and bool(self._remove_bot_mentions(message.content, self.bot.user.id))

    async def _is_reply_to_ai_message(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> bool:
        if self.bot.user is None or replied_message is None:
            return False
        if replied_message.author.id != self.bot.user.id:
            return False
        if self._is_command_result_message(replied_message):
            return False
        if self._is_chat_response_message(replied_message.id):
            return True
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        db = getattr(self.bot, "db", None)
        if guild is None or channel is None or db is None:
            return False
        try:
            return await db.is_ai_assistant_message(guild.id, channel.id, replied_message.id)
        except Exception:
            logging.exception("Failed to check persisted AI reply message")
            return False

    def _is_addressed_to_bot_by_name(self, content: str) -> bool:
        return self._alias_at_start_match(content) is not None

    def _canonical_bot_aliases(self) -> set[str]:
        return {"nitori", "nitori-buchona", "nitori buchona"}

    def _bot_aliases(self) -> set[str]:
        aliases = set(self._canonical_bot_aliases())
        if self.bot.user is None:
            return aliases
        for name in (
            getattr(self.bot.user, "name", None),
            getattr(self.bot.user, "display_name", None),
            getattr(self.bot.user, "global_name", None),
        ):
            raw = str(name or "").strip()
            if not raw:
                continue
            aliases.add(raw)
            normalized = self._normalize_alias_text(raw)
            if normalized:
                aliases.add(normalized)
        return {alias for alias in aliases if alias}

    @staticmethod
    def _normalize_alias_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(text or ""))
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = normalized.casefold()
        normalized = re.sub(r"[_\-\s]+", " ", normalized)
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        return " ".join(normalized.split())

    def _alias_at_start_match(self, content: str) -> str | None:
        normalized = self._normalize_alias_text(content)
        if not normalized:
            return None
        prefixes = ("", "oye ", "hey ", "ey ", "oie ")
        for alias in sorted(self._bot_aliases(), key=len, reverse=True):
            alias_norm = self._normalize_alias_text(alias)
            if not alias_norm:
                continue
            for prefix in prefixes:
                candidate = f"{prefix}{alias_norm}".strip()
                if normalized == candidate or normalized.startswith(candidate + " "):
                    return alias_norm
        return None

    def _alias_reference_match(self, content: str) -> str | None:
        normalized = self._normalize_alias_text(content)
        if not normalized:
            return None
        padded = f" {normalized} "
        for alias in sorted(self._bot_aliases(), key=len, reverse=True):
            alias_norm = self._normalize_alias_text(alias)
            if not alias_norm:
                continue
            if f" {alias_norm} " in padded and self._alias_at_start_match(content) is None:
                return alias_norm
        return None

    async def _conversation_anchor_type(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
        *,
        is_reply_to_ai: bool,
        prefix: str,
        settings: object,
        is_command_context_message: bool = False,
    ) -> str:
        if self.bot.user is None:
            return "NONE"
        if self._mentions_bot(message):
            stripped = self._remove_bot_mentions(message.content, self.bot.user.id)
            return "DIRECT_MENTION" if stripped else "NONE"
        if is_reply_to_ai:
            return "REPLY_TO_AI"
        if self._is_reply_to_watch_message(message):
            return "REPLY_TO_WATCH"
        if self._is_addressed_to_bot_by_name(message.content):
            return "NAME_AT_START"
        if await self._is_same_user_continuation_candidate(
            message,
            prefix=prefix,
            settings=settings,
            is_command_context_message=is_command_context_message,
        ):
            return "SAME_USER_CONTINUATION"
        if self._alias_reference_match(message.content):
            return "NAME_REFERENCE"
        return "NONE"

    async def _is_same_user_continuation_candidate(
        self,
        message: discord.Message,
        *,
        prefix: str,
        settings: object,
        is_command_context_message: bool = False,
    ) -> bool:
        lease = self._valid_lease_for_message(message)
        if lease is None:
            return False
        if getattr(message.author, "id", None) != lease.owner_user_id:
            return False
        if is_command_context_message:
            return False
        if getattr(message, "webhook_id", None) is not None:
            return False
        reference = getattr(message, "reference", None)
        if reference is not None and getattr(reference, "message_id", None) is not None:
            logging.info(
                "AI continuation rejected guild=%s channel=%s message=%s author=%s continuation_rejected_reason=reply_to_human",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(message.author, "id", None),
            )
            return False
        bot_user_id = getattr(getattr(self, "bot", None).user, "id", None)
        for mentioned in getattr(message, "mentions", []) or []:
            mentioned_id = getattr(mentioned, "id", None)
            if mentioned_id != bot_user_id and not getattr(mentioned, "bot", False):
                return False
        content = message.content.strip()
        if not content or self._looks_like_command_message(content, configured_prefix=prefix):
            return False
        if await self._is_passive_ai_blocked_channel(message.guild, message.channel, settings):
            logging.info(
                "AI continuation rejected guild=%s channel=%s message=%s author=%s continuation_rejected_reason=passive_blocked_channel",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(message.author, "id", None),
            )
            return False
        return True

    def _valid_lease_for_message(self, message: discord.Message) -> ContinuationLease | None:
        key = self._message_channel_key(message)
        lease = self._continuation_leases.get(key)
        if lease is None:
            return None
        if lease.expires_at <= time.monotonic():
            self._continuation_leases.pop(key, None)
            return None
        return lease

    def _watch_for_replied_message(self, message: discord.Message) -> FootballLiveWatch | None:
        reference = getattr(message, "reference", None)
        replied_id = getattr(reference, "message_id", None)
        if replied_id is None:
            return None
        watch = self._football_live_watches.get(self._message_channel_key(message))
        if watch is None or watch.canceled:
            return None
        try:
            return watch if int(replied_id) in watch.watch_message_ids else None
        except (TypeError, ValueError):
            return None

    def _is_reply_to_watch_message(self, message: discord.Message) -> bool:
        return self._watch_for_replied_message(message) is not None

    def _invalidate_lease_for_intervening_human(self, message: discord.Message) -> None:
        lease = self._valid_lease_for_message(message)
        if lease is None:
            return
        if getattr(message.author, "id", None) != lease.owner_user_id:
            self._continuation_leases.pop(self._message_channel_key(message), None)
            logging.info(
                "AI routing lease invalidated guild=%s channel=%s message=%s author=%s reason=intervening_human",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(message.author, "id", None),
            )

    def _invalidate_owner_lease_for_rejection(self, message: discord.Message, reason: str) -> None:
        lease = self._valid_lease_for_message(message)
        if lease is None:
            return
        if getattr(message.author, "id", None) == lease.owner_user_id:
            self._continuation_leases.pop(self._message_channel_key(message), None)
            logging.info(
                "AI routing lease invalidated guild=%s channel=%s message=%s author=%s reason=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(message.author, "id", None),
                reason,
            )

    def _create_or_renew_lease(
        self,
        message: discord.Message,
        *,
        last_bot_response_id: int | None,
        action: str,
        resolved_request: str | None = None,
        active_typed_context: tuple[CapabilityExecutionPlan, CapabilityResultBundle] | None = None,
    ) -> None:
        self._continuation_leases[self._message_channel_key(message)] = ContinuationLease(
            owner_user_id=int(message.author.id),
            last_user_message_id=getattr(message, "id", None),
            last_bot_response_id=last_bot_response_id,
            expires_at=time.monotonic() + self._continuation_lease_seconds,
            last_action=action,
            resolved_request=resolved_request,
            active_typed_context=active_typed_context,
        )

    def _publish_typed_continuation_context(
        self,
        message: discord.Message,
        *,
        last_bot_response_id: int | None,
        action: str,
        resolved_request: str | None = None,
        execution_context: CapabilityExecutionContext,
    ) -> None:
        bundle = execution_context.bundle
        active_typed_context = None
        publication_eligible = bool(
            bundle.satisfied and bundle.complete and bundle.results and all(
            result.status == CapabilityTerminalStatus.SUCCESS
            and result.terminal
            and result.satisfies_request
            and result.provenance.get("authorized") is not False
            and bool(result.scope)
            for result in bundle.results
            )
        )
        logging.info(
            "AI continuation context publication message=%s capabilities=%s result_count=%s "
            "complete=%s satisfied=%s publication_eligible=%s scope_present=%s authorized=%s",
            getattr(message, "id", None),
            ",".join(bundle.required_capabilities),
            len(bundle.results),
            bundle.complete,
            bundle.satisfied,
            publication_eligible,
            all(bool(result.scope) for result in bundle.results),
            all(result.provenance.get("authorized") is not False for result in bundle.results),
        )
        if publication_eligible:
            continuation_bundle = replace(
                bundle,
                results=tuple(
                    replace(result, delivery_metadata={})
                    for result in bundle.results
                ),
            )
            active_typed_context = (execution_context.execution_plan, continuation_bundle)
        self._create_or_renew_lease(
            message,
            last_bot_response_id=last_bot_response_id,
            action=action,
            resolved_request=resolved_request,
            active_typed_context=active_typed_context,
        )

    def _football_context_key(self, message: discord.Message) -> tuple[int, int, int] | None:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        author_id = getattr(getattr(message, "author", None), "id", None)
        if guild_id is None or channel_id is None or author_id is None:
            return None
        return int(guild_id), int(channel_id), int(author_id)

    def _valid_football_turn_context(self, message: discord.Message) -> FootballTurnContext | None:
        key = self._football_context_key(message)
        if key is None:
            return None
        context = self._football_turn_contexts.get(key)
        if context is None:
            return None
        if time.monotonic() > context.expires_at:
            self._football_turn_contexts.pop(key, None)
            return None
        return context


    def _mark_football_context_dormant(self, message: discord.Message) -> None:
        context = self._valid_football_turn_context(message)
        if context is not None:
            context.dormant = True

    def _pending_key(
        self,
        message: discord.Message,
        target_message_id: int | None = None,
    ) -> tuple[int, int, int, int | None]:
        return (
            int(message.guild.id),
            int(message.channel.id),
            int(message.author.id),
            target_message_id,
        )

    def _pending_keys_for_message(self, message: discord.Message) -> list[tuple[int, int, int, int | None]]:
        guild_id = int(message.guild.id)
        channel_id = int(message.channel.id)
        author_id = int(message.author.id)
        return [
            key
            for key in self._pending_interactions
            if key[0] == guild_id and key[1] == channel_id and key[2] == author_id
        ]

    def _get_pending_interaction(self, message: discord.Message) -> PendingInteraction | None:
        now = time.monotonic()
        for key in self._pending_keys_for_message(message):
            pending = self._pending_interactions.get(key)
            if pending is None:
                continue
            if pending.created_at + PENDING_INTERACTION_SECONDS <= now:
                self._pending_interactions.pop(key, None)
                continue
            return pending
        return None

    def _invalidate_pending_for_intervening_human(self, message: discord.Message) -> None:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        author_id = getattr(getattr(message, "author", None), "id", None)
        if guild_id is None or channel_id is None or author_id is None:
            return
        for key, pending in list(self._pending_interactions.items()):
            if key[0] == int(guild_id) and key[1] == int(channel_id) and key[2] != int(author_id):
                self._cancel_pending_interaction(pending, event="intervening_human")

    def _set_pending_interaction(
        self,
        message: discord.Message,
        *,
        route_decision: RouteDecision,
        target_message_id: int | None,
    ) -> PendingInteraction:
        key = self._pending_key(message, target_message_id)
        version = self._pending_versions.get(key, 0) + 1
        self._pending_versions[key] = version
        pending = PendingInteraction(
            guild_id=int(message.guild.id),
            channel_id=int(message.channel.id),
            owner_user_id=int(message.author.id),
            target_message_id=target_message_id,
            action=route_decision.action,
            route_decision=route_decision,
            version=version,
            created_at=time.monotonic(),
        )
        self._pending_interactions[key] = pending
        self._log_pending_event(message, "set", pending)
        return pending

    def _clear_pending_interaction(self, pending: PendingInteraction | None, event: str = "clear") -> None:
        if pending is None:
            return
        key = (pending.guild_id, pending.channel_id, pending.owner_user_id, pending.target_message_id)
        self._pending_interactions.pop(key, None)
        logging.info(
            "AI pending event=%s guild=%s channel=%s owner=%s target=%s version=%s action=%s",
            event,
            pending.guild_id,
            pending.channel_id,
            pending.owner_user_id,
            pending.target_message_id,
            pending.version,
            pending.action,
        )

    def _cancel_pending_interaction(self, pending: PendingInteraction | None, event: str = "cancel") -> None:
        if pending is None:
            return
        pending.canceled = True
        key = (pending.guild_id, pending.channel_id, pending.owner_user_id, pending.target_message_id)
        self._pending_versions[key] = self._pending_versions.get(key, pending.version) + 1
        self._clear_pending_interaction(pending, event=event)

    def _pending_can_send(self, pending: PendingInteraction | None) -> bool:
        if pending is None or pending.canceled:
            return False
        key = (pending.guild_id, pending.channel_id, pending.owner_user_id, pending.target_message_id)
        return self._pending_interactions.get(key) is pending and self._pending_versions.get(key) == pending.version

    def _is_pending_followup_candidate(self, message: discord.Message, pending: PendingInteraction | None) -> bool:
        if pending is None:
            return False
        if pending.owner_user_id != int(message.author.id):
            return False
        if pending.guild_id != int(message.guild.id) or pending.channel_id != int(message.channel.id):
            return False
        if getattr(message, "webhook_id", None) is not None:
            return False
        return bool(message.content.strip())

    def _log_pending_event(self, message: discord.Message, event: str, pending: PendingInteraction) -> None:
        logging.info(
            "AI pending event=%s guild=%s channel=%s message=%s owner=%s target=%s version=%s action=%s",
            event,
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            pending.owner_user_id,
            pending.target_message_id,
            pending.version,
            pending.action,
        )

    def _route_target_message_id(
        self,
        message: discord.Message,
        decision: RouteDecision,
        image_context: ChatImageContext,
        replied_message: discord.Message | None,
    ) -> int | None:
        if decision.target_message is not None:
            return decision.target_message
        target = image_context.reaction_target
        if target is not message and hasattr(target, "id"):
            return getattr(target, "id", None)
        if replied_message is not None and decision.action in {"ADD_REACTION", "REACT_ONLY", "EDIT_IMAGE", "ANALYZE_IMAGE"}:
            return getattr(replied_message, "id", None)
        return getattr(message, "id", None)


    async def _execute_reaction_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        image_context: ChatImageContext,
        replied_message: discord.Message | None,
        pending_interaction: PendingInteraction | None,
    ) -> bool:
        target = self._reaction_target_for_decision(
            message,
            decision,
            image_context=image_context,
            replied_message=replied_message,
            pending_interaction=pending_interaction,
        )
        if target is None or not hasattr(target, "add_reaction"):
            return False
        emoji = decision.emoji or (decision.emojis[0] if decision.emojis else None)
        if not emoji:
            return False
        try:
            reaction: str | discord.PartialEmoji = emoji
            if emoji.startswith("<") and emoji.endswith(">"):
                reaction = discord.PartialEmoji.from_str(emoji)
            await target.add_reaction(reaction)
            logging.info(
                "AI reaction action guild=%s channel=%s message=%s target=%s action=%s emoji=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(target, "id", None),
                decision.action,
                self._anon_id(emoji),
            )
            return True
        except Exception:
            logging.exception("Failed to add routed AI reaction")
            return False

    async def _execute_admin_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        lang: str,
        resolution_message: discord.Message | None = None,
    ) -> CapabilityTerminalResult:
        guild = message.guild
        if guild is None:
            return self._admin_result_only_failure(
                "VALIDATION_FAILED",
                reason="missing_guild",
                direct_message=tr(lang, "This action requires a server.", "Esta accion requiere un servidor."),
            )
        resolution_message = resolution_message or message
        admin_plan = self._coerce_admin_plan(decision.admin)
        logging.info(
            "AI admin action plan guild=%s channel=%s author=%s admin_action=%s confidence=%.2f duration_seconds=%s time_window_seconds=%s message_count=%s reason_present=%s",
            getattr(guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message.author, "id", None),
            admin_plan.get("admin_action"),
            float(admin_plan.get("confidence", 0.0) or 0.0),
            admin_plan.get("duration_seconds"),
            admin_plan.get("time_window_seconds"),
            admin_plan.get("message_count"),
            bool(admin_plan.get("reason")),
        )
        if not admin_plan.get("valid"):
            return self._admin_result_only_failure(
                "VALIDATION_FAILED",
                reason="invalid_admin_plan",
                payload={
                    "admin_action": admin_plan.get("admin_action", "unknown"),
                    "outcome_code": "invalid_admin_plan",
                },
                direct_message=admin_plan.get("clarification_question")
                or tr(lang, "I need a clearer admin action before doing that.", "Necesito una accion de admin mas clara antes de hacer eso."),
            )
        action = str(admin_plan["admin_action"])
        if action in {"join_stats", "leave_stats"}:
            return await self._execute_member_event_stats(
                message,
                admin_plan,
                lang=lang,
            )
        if action == "delete_messages":
            result = await self._admin_actions.delete_recent_messages(
                guild,
                message.author,
                message.channel,
                message,
                int(admin_plan.get("message_count") or 0),
                lang=lang,
            )
            if result.success:
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": "delete_messages",
                        "outcome_code": str(result.code or "success")[:80],
                        "state": "deleted",
                        "requested_count": int(admin_plan.get("message_count") or 0),
                    },
                    provenance={
                        "source": "AdminActionService",
                        "authorized": True,
                    },
                    scope={
                        "guild_id": getattr(guild, "id", None),
                        "channel_id": getattr(message.channel, "id", None),
                    },
                    delivery_metadata={
                        "delete_after_seconds": 5,
                        "delete_trigger_on_success": True,
                    },
                    satisfies_request=True,
                )
            return self._admin_result_only_failure(
                str(result.code or "PROVIDER_FAILURE"),
                reason="delete_messages_failed",
                payload={
                    "admin_action": "delete_messages",
                    "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80],
                },
                direct_message=str(result.message or "")[:500],
            )
        if action == "delete_role":
            role = await self._resolve_admin_role_for_execution(resolution_message, admin_plan, lang)
            if isinstance(role, str):
                return self._admin_result_only_failure(
                    "VALIDATION_FAILED",
                    reason="delete_role_target_invalid",
                    payload={"admin_action": "delete_role", "outcome_code": "target_invalid"},
                    direct_message=str(role)[:500],
                )
            result = await self._admin_actions.delete_role(
                guild,
                message.author,
                role,
                reason=admin_plan.get("reason"),
                lang=lang,
            )
            if result.success:
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": "delete_role",
                        "outcome_code": str(result.code or "success")[:80],
                        "state": "deleted",
                        "role_id": getattr(role, "id", None),
                    },
                    provenance={
                        "source": "AdminActionService",
                        "authorized": True,
                    },
                    scope={
                        "guild_id": getattr(guild, "id", None),
                        "role_id": getattr(role, "id", None),
                    },
                    satisfies_request=True,
                )
            return self._admin_result_only_failure(
                str(result.code or "PROVIDER_FAILURE"),
                reason="delete_role_failed",
                payload={
                    "admin_action": "delete_role",
                    "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80],
                },
                direct_message=str(result.message or "")[:500],
            )
        if action == "create_role":
            role_name = str(admin_plan.get("role_name") or "")
            result = await self._admin_actions.create_role(
                guild,
                message.author,
                role_name,
                color_hex=admin_plan.get("role_color_hex"),
                reason=admin_plan.get("reason"),
                lang=lang,
            )
            if result.success:
                resource_handle = getattr(result, "resource_handle", None)
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": "create_role",
                        "outcome_code": str(result.code or "success")[:80],
                        "state": "created",
                        "resource_handle": resource_handle,
                    },
                    provenance={
                        "source": "AdminActionService",
                        "authorized": True,
                    },
                    scope={
                        "guild_id": getattr(guild, "id", None),
                        **({"role_id": resource_handle.get("resource_id")} if isinstance(resource_handle, dict) else {}),
                    },
                    satisfies_request=True,
                )
            return self._admin_result_only_failure(
                str(result.code or "PROVIDER_FAILURE"),
                reason="create_role_failed",
                payload={
                    "admin_action": "create_role",
                    "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80],
                },
                direct_message=str(result.message or "")[:500],
            )
        if action in {"add_role", "remove_role"}:
            target = await self._resolve_admin_target_member(resolution_message, admin_plan, lang)
            role = await self._resolve_admin_role_for_execution(resolution_message, admin_plan, lang)
            if isinstance(target, str) or isinstance(role, str):
                error = target if isinstance(target, str) else role
                return self._admin_result_only_failure("VALIDATION_FAILED", reason="role_assignment_target_invalid", payload={"admin_action": action, "outcome_code": "target_invalid"}, direct_message=str(error)[:500])
            result = await getattr(self._admin_actions, action)(guild, message.author, target, role, reason=admin_plan.get("reason"), lang=lang)
            state = "added" if action == "add_role" else "removed"
            scope = {"guild_id": getattr(guild, "id", None), "user_id": getattr(target, "id", None), "role_id": getattr(role, "id", None)}
        elif action in {"kick", "ban", "tempban", "warn", "list_warnings", "remove_warning", "clear_warnings", "set_nickname", "user_info", "purge_user_messages"}:
            target = await self._resolve_admin_target_member(resolution_message, admin_plan, lang)
            if isinstance(target, str):
                return self._admin_result_only_failure("VALIDATION_FAILED", reason="member_target_invalid", payload={"admin_action": action, "outcome_code": "target_invalid"}, direct_message=str(target)[:500])
            if action == "kick":
                result = await self._admin_actions.kick_member(guild, message.author, target, reason=admin_plan.get("reason"), lang=lang)
                state = "kicked"
            elif action == "ban":
                result = await self._admin_actions.ban_member(guild, message.author, target, reason=admin_plan.get("reason"), lang=lang)
                state = "banned"
            elif action == "tempban":
                result = await self._admin_actions.tempban_member(guild, message.author, target, duration_seconds=int(admin_plan["duration_seconds"]), reason=admin_plan.get("reason"), lang=lang)
                state = "banned"
            elif action == "warn":
                result = await self._admin_actions.add_warning(guild, message.author, target, reason=admin_plan.get("reason"), lang=lang)
                state = "added"
            elif action == "list_warnings":
                result = await self._admin_actions.list_warnings(guild, message.author, target, lang=lang)
                state = "listed"
            elif action == "remove_warning":
                result = await self._admin_actions.remove_warning(guild, message.author, target, int(admin_plan["warning_number"]), lang=lang)
                state = "removed"
            elif action == "set_nickname":
                result = await self._admin_actions.set_nickname(guild, message.author, target, admin_plan["nickname"], lang=lang)
                state = "updated"
            elif action == "user_info":
                result = await self._admin_actions.user_info(guild, message.author, target, lang=lang)
                state = "read"
            elif action == "purge_user_messages":
                # The current channel only (like say); the trigger message is never a candidate.
                result = await self._admin_actions.purge_user_messages(guild, message.author, message.channel, target, int(admin_plan["message_count"]), exclude_message_id=getattr(message, "id", None), lang=lang)
                state = "purged"
            else:
                result = await self._admin_actions.clear_warnings(guild, message.author, target, reason=admin_plan.get("reason"), lang=lang)
                state = "cleared"
            scope = {"guild_id": getattr(guild, "id", None), "user_id": getattr(target, "id", None)}
            if action == "purge_user_messages":
                scope["channel_id"] = getattr(message.channel, "id", None)
        elif action == "unban":
            user_id = int(admin_plan["target_user_id"])
            result = await self._admin_actions.unban_user(guild, message.author, user_id, reason=admin_plan.get("reason"), lang=lang)
            state = "unbanned"
            scope = {"guild_id": getattr(guild, "id", None), "user_id": user_id}
        elif action == "create_channel":
            result = await self._admin_actions.create_text_channel(guild, message.author, str(admin_plan["channel_name"]), lang=lang)
            state = "created"
            scope = {"guild_id": getattr(guild, "id", None)}
        elif action in {"delete_channel", "set_slowmode", "clear_channel", "clone_channel", "color_publish"} | ADMIN_CONFIG_CHANNEL_OPERATIONS:
            channel = await self._resolve_admin_channel_for_execution(resolution_message, admin_plan, lang)
            if isinstance(channel, str):
                return self._admin_result_only_failure("VALIDATION_FAILED", reason="channel_target_invalid", payload={"admin_action": action, "outcome_code": "target_invalid"}, direct_message=str(channel)[:500])
            if action == "set_slowmode":
                result = await self._admin_actions.set_slowmode(guild, message.author, channel, int(admin_plan["slowmode_seconds"]), lang=lang)
                state = "updated"
            elif action == "set_modlog":
                result = await self._admin_actions.set_modlog_channel(guild, message.author, channel, lang=lang)
                state = "set"
            elif action == "ai_channel_add":
                result = await self._admin_actions.add_ai_channel(guild, message.author, channel, lang=lang)
                state = "added"
            elif action == "ai_channel_remove":
                result = await self._admin_actions.remove_ai_channel(guild, message.author, channel, lang=lang)
                state = "removed"
            elif action == "server_context_learn":
                # Besides Manage Server the author must be able to read the channel; the AdminCog method makes the provider call.
                result = self._admin_actions.server_context_learn_denied(message.author, channel, lang=lang)
                if result is None:
                    admin_cog = self.bot.get_cog("AdminCog")
                    result = await admin_cog.learn_channel_context(guild, channel, lang) if admin_cog is not None else self._admin_cog_unavailable(lang)
                state = "learned"
            elif action == "clear_channel":
                result = await self._admin_actions.clear_channel(guild, message.author, channel, lang=lang)
                state = "cleared"
            elif action == "clone_channel":
                result = await self._admin_actions.clone_channel(guild, message.author, channel, lang=lang)
                state = "cloned"
            elif action == "color_publish":
                moderation_cog = self.bot.get_cog("ModerationCog")
                result = await moderation_cog.publish_color_panel(guild, message.author, channel, strict=True, lang=lang) if moderation_cog is not None else self._admin_cog_unavailable(lang)
                state = "published"
            else:
                result = await self._admin_actions.delete_channel(guild, message.author, channel, current_channel_id=getattr(message.channel, "id", None), reason=admin_plan.get("reason"), lang=lang)
                state = "deleted"
            scope = {"guild_id": getattr(guild, "id", None), "channel_id": getattr(channel, "id", None)}
        elif action in ADMIN_CONFIG_OPERATIONS:
            # Configuration without a channel; the plan already validated every operand and refused a channel.
            if action == "set_prefix":
                result = await self._admin_actions.set_prefix(guild, message.author, admin_plan["guild_prefix"], lang=lang)
                state = "set"
            elif action == "set_language":
                result = await self._admin_actions.set_language(guild, message.author, admin_plan["guild_language"], lang=lang)
                state = "set"
            elif action in {"set_antispam", "set_antilink"}:
                result = await self._admin_actions.set_filter(guild, message.author, "spam" if action == "set_antispam" else "link", admin_plan["filter_enabled"], lang=lang)
                state = "set"
            elif action == "ai_channel_list":
                result = await self._admin_actions.list_ai_channels(guild, message.author, lang=lang)
                state = "read"
            elif action == "server_context_view":
                result = await self._admin_actions.server_context_view(guild, message.author, lang=lang)
                state = "read"
            elif action == "server_context_reset":
                result = await self._admin_actions.reset_server_context(guild, message.author, admin_plan["context_scope"], lang=lang)
                state = "reset"
            else:
                result = await self._admin_actions.clear_ai_channels(guild, message.author, lang=lang)
                state = "cleared"
            scope = {"guild_id": getattr(guild, "id", None)}
        elif action in ADMIN_COLOR_OPERATIONS:
            # The other color operations take no channel (the plan refused one); ModerationCog checks Administrator and does the work.
            moderation_cog = self.bot.get_cog("ModerationCog")
            state = {"color_setup": "configured", "color_list": "read", "color_reload": "reloaded", "color_add": "added", "color_remove": "removed"}[action]
            if moderation_cog is None:
                result = self._admin_cog_unavailable(lang)
            elif action == "color_setup":
                result = await moderation_cog.color_setup(guild, message.author, strict=True, lang=lang)
            elif action == "color_list":
                result = await moderation_cog.color_list(guild, message.author, lang=lang)
            elif action == "color_reload":
                result = await moderation_cog.publish_color_panel(guild, message.author, None, reload=True, strict=True, lang=lang)
            elif action == "color_add":
                result = await moderation_cog.color_add(guild, message.author, admin_plan["role_color_hex"], admin_plan["role_name"], strict=True, lang=lang)
            else:
                result = await moderation_cog.color_remove(guild, message.author, admin_plan["role_name"], strict=True, lang=lang)
            scope = {"guild_id": getattr(guild, "id", None)}
        elif action == "help":
            # No permission needed, exactly like /help: the section keys and the page are filtered by the author's own permissions.
            admin_cog = self.bot.get_cog("AdminCog")
            member = message.author if getattr(message.author, "guild_permissions", None) is not None else None
            result = admin_cog.help_text(lang, member, admin_plan["help_section"]) if admin_cog is not None else self._admin_cog_unavailable(lang)
            state = "read"
            scope = {"guild_id": getattr(guild, "id", None)}
        elif action == "say":
            # The current channel only: say has no channel operand.
            result = await self._admin_actions.say(guild, message.author, message.channel, admin_plan["say_text"], lang=lang)
            state = "sent"
            scope = {"guild_id": getattr(guild, "id", None), "channel_id": getattr(message.channel, "id", None)}
        else:
            result = None
            state = ""
            scope = {}
        if result is not None:
            # A destructive action reports what it actually did, also when it only partly ran; a clone that exists keeps its handle.
            partial = dict(result.data or {}) if action in {"clear_channel", "purge_user_messages", "help"} | ADMIN_CONFIG_OPERATIONS | ADMIN_COLOR_OPERATIONS else {}
            if "channel_ids" in partial:
                # The AI channel list is bounded, but its real size stays visible so a longer list is never passed off as complete.
                partial["channel_count"], partial["channel_ids"] = len(partial["channel_ids"]), partial["channel_ids"][:25]
            if action in {"create_channel", "clone_channel"} and isinstance(getattr(result, "resource_handle", None), dict):
                partial["resource_handle"] = result.resource_handle
            if result.success:
                payload = {"admin_action": action, "outcome_code": str(result.code or "success")[:80], "state": state, **partial}
                if action == "tempban":
                    payload["duration"] = int(admin_plan["duration_seconds"])
                if action == "remove_warning":
                    payload["warning_number"] = int(admin_plan["warning_number"])
                if action == "list_warnings":
                    payload["warning_count"] = int(result.message) if str(result.message).isdigit() else None
                if action == "set_slowmode":
                    payload["slowmode_seconds"] = int(admin_plan["slowmode_seconds"])
                if action == "set_nickname":
                    payload["nickname"] = admin_plan["nickname"]
                if action == "user_info":
                    # Bounded projection of the member profile; names are user- and admin-chosen text.
                    profile = result.data or {}
                    payload.update(
                        profile,
                        display_name=str(profile.get("display_name", ""))[:100],
                        roles=[str(role["name"])[:100] for role in profile.get("roles", [])[:25]],
                        permissions=list(profile.get("permissions", []))[:50],
                    )
                return normalize_terminal_result("ADMIN", "SUCCESS", provider="admin_actions", payload=payload, provenance={"source": "AdminActionService", "authorized": True}, scope=scope, satisfies_request=True)
            return self._admin_result_only_failure(str(result.code or "PROVIDER_FAILURE"), reason=f"{action}_failed", payload={"admin_action": action, "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80], **partial}, direct_message=str(result.message or "")[:500])
        if action in {"mute", "tempmute", "unmute"}:
            target = await self._resolve_admin_target_member(resolution_message, admin_plan, lang)
            if isinstance(target, str):
                return self._admin_result_only_failure(
                    "VALIDATION_FAILED",
                    reason="member_target_invalid",
                    payload={"admin_action": action, "outcome_code": "target_invalid"},
                    direct_message=str(target)[:500],
                )
            if action == "unmute":
                result = await self._admin_actions.unmute_member(
                    guild,
                    message.author,
                    target,
                    reason=admin_plan.get("reason"),
                    lang=lang,
                )
            else:
                result = await self._admin_actions.mute_member(
                    guild,
                    message.author,
                    target,
                    duration_seconds=admin_plan.get("duration_seconds"),
                    duration_label=self._duration_label(admin_plan.get("duration_seconds")),
                    reason=admin_plan.get("reason"),
                    mute_mode="auto",
                    lang=lang,
                )
            if action == "unmute" and result.success:
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": "unmute",
                        "outcome_code": str(result.code or "success")[:80],
                        "state": "removed",
                    },
                    provenance={"source": "AdminActionService"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    satisfies_request=True,
                )
            if action in {"mute", "tempmute"} and result.success:
                duration = admin_plan.get("duration_seconds")
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": action,
                        "outcome_code": str(result.code or "success")[:80],
                        "mute_mode": str(result.mute_mode or "role_mute")[:40],
                        "state": "applied",
                        "duration": int(duration) if duration is not None else None,
                    },
                    provenance={"source": "AdminActionService"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    satisfies_request=True,
                )
            return self._admin_result_only_failure(
                str(result.code or "PROVIDER_FAILURE"),
                reason=f"{action}_failed",
                payload={
                    "admin_action": action,
                    "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80],
                },
                direct_message=str(result.message or "")[:500],
            )
        if action in {"lock_channel", "unlock_channel"}:
            channel = await self._resolve_admin_channel_for_execution(resolution_message, admin_plan, lang)
            if isinstance(channel, str):
                return self._admin_result_only_failure(
                    "VALIDATION_FAILED",
                    reason="channel_target_invalid",
                    payload={"admin_action": action, "outcome_code": "target_invalid"},
                    direct_message=str(channel)[:500],
                )
            result = await self._admin_actions.set_channel_lock(
                guild,
                message.author,
                channel,
                locked=action == "lock_channel",
                lang=lang,
            )
            if action in {"lock_channel", "unlock_channel"} and result.success:
                return normalize_terminal_result(
                    "ADMIN",
                    "SUCCESS",
                    provider="admin_actions",
                    payload={
                        "admin_action": action,
                        "outcome_code": str(result.code or "success")[:80],
                        "state": "locked" if action == "lock_channel" else "unlocked",
                        "channel_id": getattr(channel, "id", None),
                    },
                    provenance={
                        "source": "AdminActionService",
                        "authorized": True,
                    },
                    scope={
                        "guild_id": getattr(guild, "id", None),
                        "channel_id": getattr(channel, "id", None),
                    },
                    satisfies_request=True,
                )
            return self._admin_result_only_failure(
                str(result.code or "PROVIDER_FAILURE"),
                reason=f"{action}_failed",
                payload={
                    "admin_action": action,
                    "outcome_code": str(result.code or "PROVIDER_FAILURE")[:80],
                },
                direct_message=str(result.message or "")[:500],
            )
        return self._admin_result_only_failure(
            "UNSUPPORTED",
            reason="unsupported_admin_action",
            payload={"admin_action": action},
            direct_message=tr(lang, "I cannot do that admin action yet.", "Todavia no puedo hacer esa accion de admin."),
        )

    @staticmethod
    def _admin_result_only_failure(
        outcome: object,
        *,
        reason: str,
        payload: dict[str, object] | None = None,
        direct_message: str | None = None,
    ) -> CapabilityTerminalResult:
        return normalize_terminal_result(
            "ADMIN",
            outcome,
            provider="admin_actions",
            payload=payload,
            provenance={"source": "AdminActionService"},
            delivery_metadata={"direct_failure_message": direct_message} if direct_message else None,
            reason=reason,
            satisfies_request=False,
        )

    @staticmethod
    def _admin_cog_unavailable(lang: str) -> AdminActionResult:
        return AdminActionResult(False, tr(lang, "That is not available right now.", "Eso no esta disponible ahora."), "not_configured")

    def _coerce_admin_plan(self, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"valid": False, "admin_action": "unknown", "confidence": 0.0}
        action = str(raw.get("admin_action", "unknown") or "unknown").strip().casefold()
        allowed = ADMIN_ACTION_OPERATIONS
        confidence = self._coerce_float(raw.get("confidence"), default=0.0)
        duration_seconds = self._coerce_positive_int(raw.get("duration_seconds"))
        time_window_seconds = self._coerce_positive_int(raw.get("time_window_seconds"))
        message_count = self._coerce_positive_int(raw.get("message_count"))
        target_user_id = self._coerce_positive_int(raw.get("target_user_id"))
        target_role_id = self._coerce_positive_int(raw.get("target_role_id"))
        resource_handle = raw.get("resource_handle")
        if target_role_id is None and isinstance(resource_handle, dict):
            if str(resource_handle.get("resource_type") or "").casefold() == "role":
                target_role_id = self._coerce_positive_int(resource_handle.get("resource_id"))
        warning_number = self._coerce_positive_int(raw.get("warning_number"))
        message_count_over_limit = message_count is not None and message_count > 500
        if message_count is not None:
            message_count = min(message_count, 500)
        reason =" ".join(str(raw.get("reason", "") or "").split())[:280] or None
        clarification = " ".join(str(raw.get("clarification_question", "") or "").split())[:240] or None
        role_name = " ".join(str(raw.get("role_name", "") or "").split())[:100] or None
        raw_role_color = "".join(str(raw.get("role_color_hex", "") or "").split())
        role_color_hex = None
        invalid_role_color = False
        if raw_role_color:
            match = re.fullmatch(r"#?([0-9a-fA-F]{6})", raw_role_color)
            if match is None:
                invalid_role_color = True
            else:
                role_color_hex = f"#{match.group(1).lower()}"
        target_user_candidates = [
            " ".join(str(item).split())[:120]
            for item in raw.get("target_user_candidates", [])
            if isinstance(raw.get("target_user_candidates", []), list) and " ".join(str(item).split())
        ][:5]
        target_role_candidates = [
            " ".join(str(item).split())[:120]
            for item in raw.get("target_role_candidates", [])
            if isinstance(raw.get("target_role_candidates", []), list) and " ".join(str(item).split())
        ][:5]
        target_channel = " ".join(str(raw.get("target_channel", "") or "").split())[:120] or None
        channel_name = " ".join(str(raw.get("channel_name", "") or "").split())[:100] or None
        slowmode_seconds = raw.get("slowmode_seconds")
        if isinstance(slowmode_seconds, bool) or not isinstance(slowmode_seconds, int) or not 0 <= slowmode_seconds <= 21600:
            slowmode_seconds = None
        nickname = str(raw.get("nickname") or "").strip()
        say_text = str(raw.get("say_text") or "").strip()
        guild_prefix = str(raw.get("guild_prefix") or "").strip()
        guild_language = str(raw.get("guild_language") or "").strip().lower()
        filter_enabled = raw.get("filter_enabled")
        context_scope = str(raw.get("context_scope") or "").strip().casefold()
        help_section = " ".join(str(raw.get("help_section") or "").split())[:40]
        bound_channel_handle = isinstance(resource_handle, dict) and action in {
            "lock_channel", "unlock_channel", "delete_channel", "set_slowmode", "clear_channel", "clone_channel", "color_publish"
        } | ADMIN_CONFIG_CHANNEL_OPERATIONS
        valid = bool(raw.get("valid", True)) and action in allowed
        if action == "tempmute" and (
            duration_seconds is None
            or duration_seconds <= 0
            or duration_seconds > DISCORD_TIMEOUT_MAX_SECONDS
        ):
            valid = False
            clarification = clarification or "How long should the temporary mute last?"
        if action in {"join_stats", "leave_stats"} and time_window_seconds is None:
            valid = False
            clarification = clarification or "What time window should I count?"
        if action == "delete_messages" and message_count is None:
            valid = False
            clarification = clarification or "How many previous messages should I delete?"
        if action == "delete_role" and not target_role_candidates:
            valid = False
            clarification = clarification or "Which role should I delete? Name it in target_role_candidates."
        if action in {"add_role", "remove_role"} and not target_role_candidates and target_role_id is None and resource_handle is None:
            # role_name only names a role to create; an existing role is named in target_role_candidates.
            valid = False
            clarification = clarification or "Which role? Name it in target_role_candidates."
        if action == "create_role" and not role_name:
            valid = False
            clarification = clarification or "What role name should I create?"
        if action == "create_role" and invalid_role_color:
            valid = False
            clarification = clarification or "What six-digit hex color should the role use?"
        if action == "unban" and target_user_id is None:
            valid = False
            clarification = clarification or "Which user ID should I unban?"
        if action == "tempban" and duration_seconds is None:
            valid = False
            clarification = clarification or "How long should the temporary ban last?"
        if action == "create_channel" and not channel_name:
            valid = False
            clarification = clarification or "What channel name should I create?"
        if action == "delete_channel" and not target_channel and not bound_channel_handle:
            valid = False
            clarification = clarification or "Which channel should I delete?"
        if action in {"lock_channel", "unlock_channel", "set_slowmode"} and not target_channel and not bound_channel_handle:
            valid = False
            clarification = clarification or "Which channel should I target? Name it in target_channel."
        if action in {"clear_channel", "clone_channel"} and not target_channel and not bound_channel_handle:
            # Never the current channel and never a channel mentioned in the message: the plan must name it.
            valid = False
            clarification = clarification or "Which channel should I target? Name it explicitly."
        if action == "set_slowmode" and slowmode_seconds is None:
            valid = False
            clarification = clarification or "How many seconds (0 to 21600) should the slowmode be? Use 0 to disable it."
        if action == "set_nickname" and (not nickname or len(nickname) > 32):
            valid = False
            clarification = clarification or "What nickname (up to 32 characters) should I set?"
        if action == "say" and (not say_text or len(say_text) > 2000):
            valid = False
            clarification = clarification or "What should I say in this channel (up to 2000 characters)?"
        if action == "say" and (target_channel or resource_handle is not None):
            # say only ever posts in the channel of the current message.
            valid = False
            clarification = clarification or "I can only say things in the current channel."
        if action == "purge_user_messages" and (message_count is None or message_count_over_limit):
            valid = False
            clarification = clarification or "How many of that member's recent messages should I delete (1 to 500)?"
        if action == "purge_user_messages" and (target_channel or resource_handle is not None):
            # purge_user_messages only ever acts in the channel of the current message.
            valid = False
            clarification = clarification or "I can only delete messages in the current channel."
        if (action in ADMIN_CONFIG_CHANNEL_OPERATIONS or action == "color_publish") and not target_channel and not bound_channel_handle:
            # Never the current or a mentioned channel: the plan must name it (or bind the channel an upstream task just made).
            valid = False
            clarification = clarification or "Which channel should I use? Name it explicitly."
        if action == "set_prefix" and (not guild_prefix or len(guild_prefix) > 5 or not all(ch.isprintable() and not ch.isspace() for ch in guild_prefix)):
            valid = False
            clarification = clarification or "What prefix (up to 5 characters) should I set?"
        if action == "set_language" and guild_language not in {"en", "es"}:
            valid = False
            clarification = clarification or "Which language should I use, en or es?"
        if action in {"set_antispam", "set_antilink"} and not isinstance(filter_enabled, bool):
            valid = False
            clarification = clarification or "Should that filter be enabled or disabled?"
        if action == "server_context_reset" and context_scope not in {"summaries", "memory", "ai_history", "all"}:
            # Never defaulted (the command defaults to all): the plan must name what to reset.
            valid = False
            clarification = clarification or "What should I reset: summaries, memory, ai_history or all?"
        if action == "color_add" and (not role_name or role_color_hex is None):
            # Neither the name nor the color is ever defaulted (the command's own default name is its hex).
            valid = False
            clarification = clarification or "What color name and six-digit hex color should I add?"
        if action == "color_remove" and not role_name:
            valid = False
            clarification = clarification or "Which color role should I remove?"
        if action in {"color_setup", "color_list", "color_reload"} and (role_name or role_color_hex is not None):
            # These take no color operand: a misrouted 'add Teal' must not become a setup that creates every default role.
            valid = False
            clarification = clarification or "That action does not take a color name."
        if (action in ADMIN_CONFIG_OPERATIONS - ADMIN_CONFIG_CHANNEL_OPERATIONS or action in ADMIN_COLOR_OPERATIONS - {"color_publish"} or action == "help") and (target_channel or resource_handle is not None):
            valid = False
            clarification = clarification or "That action does not take a channel."
        if action in {
            "add_role", "remove_role", "kick", "ban", "tempban", "mute", "tempmute",
            "unmute", "warn", "list_warnings", "remove_warning", "clear_warnings", "set_nickname", "user_info", "purge_user_messages",
        } and target_user_id is None and not target_user_candidates:
            valid = False
            clarification = clarification or "Which member should I target?"
        if action == "remove_warning" and warning_number not in {1, 2, 3}:
            valid = False
            clarification = clarification or "Which warning number (1, 2, or 3) should I remove?"
        return {
            "valid": valid,
            "admin_action": action,
            "resource_handle": resource_handle if isinstance(resource_handle, dict) else None,
            "target_user_candidates": target_user_candidates,
            "target_role_candidates": target_role_candidates,
            "target_channel": target_channel,
            "channel_name": channel_name,
            "target_user_id": target_user_id,
            "target_role_id": target_role_id,
            "warning_number": warning_number,
            "role_name": role_name,
            "role_color_hex": role_color_hex,
            "message_count": message_count,
            "slowmode_seconds": slowmode_seconds,
            "nickname": nickname,
            "say_text": say_text,
            "guild_prefix": guild_prefix,
            "guild_language": guild_language,
            "filter_enabled": filter_enabled,
            "context_scope": context_scope,
            "help_section": help_section,
            "duration_seconds": duration_seconds,
            "reason": reason,
            "time_window_seconds": time_window_seconds,
            "confidence": confidence,
            "clarification_question": clarification,
        }

    @staticmethod
    def _coerce_float(value: object, *, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_positive_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    async def _execute_member_event_stats(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        *,
        lang: str,
    ) -> CapabilityTerminalResult:
        authority = self._route_authority_metadata(message)
        if not (
            authority["author_is_bot_owner"]
            or authority["author_is_guild_owner"]
            or authority["author_has_administrator"]
            or authority["author_has_manage_guild"]
        ):
            logging.info(
                "AI admin stats rejected guild=%s channel=%s author=%s rejected_reason=missing_manage_guild author_is_bot_owner=%s author_has_manage_guild=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                authority["author_is_bot_owner"],
                authority["author_has_manage_guild"],
            )
            return self._admin_result_only_failure(
                "PERMISSION_DENIED",
                reason="missing_manage_guild",
                payload={
                    "admin_action": admin_plan.get("admin_action", "unknown"),
                    "outcome_code": "missing_manage_guild",
                },
                direct_message=tr(lang, "You need Manage Server permission to ask for member stats.", "Necesitas Gestionar servidor para pedir estadisticas de miembros."),
            )
        seconds = int(admin_plan.get("time_window_seconds") or 0)
        if seconds <= 0:
            return self._admin_result_only_failure(
                "VALIDATION_FAILED",
                reason="missing_time_window",
                payload={
                    "admin_action": admin_plan.get("admin_action", "unknown"),
                    "outcome_code": "missing_time_window",
                },
                direct_message=tr(lang, "Tell me the time window to count.", "Dime la ventana de tiempo que quieres contar."),
            )
        event_type = "join" if admin_plan.get("admin_action") == "join_stats" else "leave"
        since = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        count = await self.bot.db.count_member_events(message.guild.id, event_type, since)
        label = self._duration_label(seconds)
        noun_en = "joined" if event_type == "join" else "left"
        noun_es = "entraron" if event_type == "join" else "salieron"
        text = tr(
            lang,
            f"{count} member(s) {noun_en} in the last {label}. I can only count events tracked after this feature was enabled.",
            f"{count} persona(s) {noun_es} en los ultimos {label}. Solo puedo contar eventos registrados desde que se activo este seguimiento.",
        )
        logging.info(
            "AI admin stats guild=%s channel=%s author=%s admin_action=%s time_window_seconds=%s count=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message.author, "id", None),
            admin_plan.get("admin_action"),
            seconds,
            count,
        )
        return normalize_terminal_result(
            "ADMIN",
            "SUCCESS",
            provider="database",
            payload={
                "event_type": event_type,
                "count": max(0, int(count)),
                "time_window_seconds": seconds,
            },
            provenance={"source": "count_member_events"},
            scope={"guild_id": getattr(message.guild, "id", None)},
            satisfies_request=True,
        )

    async def _resolve_admin_target_member(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        lang: str,
    ) -> discord.Member | str:
        guild = message.guild
        exact_user_id = self._coerce_positive_int(admin_plan.get("target_user_id"))
        if exact_user_id is not None:
            get_member = getattr(guild, "get_member", None)
            member = get_member(exact_user_id) if callable(get_member) else None
            if member is not None:
                return member
            fetch_member = getattr(guild, "fetch_member", None)
            if callable(fetch_member):
                try:
                    member = await fetch_member(exact_user_id)
                except Exception:
                    logging.exception("Failed to fetch exact admin member target")
                    member = None
                if member is not None:
                    return member
        candidates = admin_plan.get("target_user_candidates") or []
        matches: list[discord.Member] = []
        for candidate in candidates:
            # A candidate that is a whole Discord id names that member by identity; the member must be in this guild.
            candidate_id = self._coerce_positive_int(candidate) if str(candidate).strip().isdigit() else None
            get_member = getattr(guild, "get_member", None)
            member = get_member(candidate_id) if candidate_id is not None and callable(get_member) else None
            if member is not None:
                matches.append(member)
                continue
            member = guild.get_member_named(candidate) if hasattr(guild, "get_member_named") else None
            if member is not None:
                matches.append(member)
                continue
            query_members = getattr(guild, "query_members", None)
            if query_members is not None:
                try:
                    matches.extend(await query_members(query=candidate, limit=5))
                except Exception:
                    logging.exception("AI admin member query failed")
        unique: dict[int, discord.Member] = {
            int(getattr(member, "id", 0)): member
            for member in matches
            if getattr(member, "id", None) is not None
        }
        if len(unique) == 1:
            return next(iter(unique.values()))
        if len(unique) > 1:
            names = ", ".join(getattr(member, "display_name", getattr(member, "name", "user")) for member in list(unique.values())[:5])
            return tr(lang, f"I found multiple matching members: {names}. Mention the exact user.", f"Encontre varios miembros: {names}. Menciona al usuario exacto.")
        bot_user = getattr(self.bot, "user", None)
        if not candidates and exact_user_id is None:
            for member in getattr(message, "mentions", []) or []:
                if getattr(member, "id", None) != getattr(bot_user, "id", None) and not getattr(member, "bot", False):
                    return member
        return tr(lang, "I could not find that member. Mention the exact user.", "No encontre a ese miembro. Menciona al usuario exacto.")

    def _resolve_admin_target_role(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        lang: str,
    ) -> discord.Role | str:
        guild = message.guild
        resource_handle = admin_plan.get("resource_handle")
        if resource_handle is not None:
            if not isinstance(resource_handle, dict) or str(resource_handle.get("resource_type") or "").casefold() != "role":
                return tr(lang, "The supplied role handle is invalid.", "El identificador de rol proporcionado no es valido.")
            if self._coerce_positive_int(resource_handle.get("guild_id")) != self._coerce_positive_int(getattr(guild, "id", None)):
                return tr(lang, "The supplied role belongs to another server.", "El identificador de rol pertenece a otro servidor.")
            if self._coerce_positive_int(resource_handle.get("resource_id")) is None:
                return tr(lang, "The supplied role handle is invalid.", "El identificador de rol proporcionado no es valido.")
        exact_id = self._parse_role_id(str(admin_plan.get("target_role_id") or ""))
        if exact_id is not None and hasattr(guild, "get_role"):
            role = guild.get_role(exact_id)
            if role is not None:
                return role
        candidates = admin_plan.get("target_role_candidates") or []
        matches: list[discord.Role] = []
        for candidate in candidates:
            query = str(candidate or "").strip()
            if not query:
                continue
            role_id = self._parse_role_id(query)
            if role_id is not None and hasattr(guild, "get_role"):
                role = guild.get_role(role_id)
                if role is not None:
                    matches.append(role)
                continue
            normalized = query.lstrip("@").strip().casefold()
            exact = [
                role
                for role in getattr(guild, "roles", []) or []
                if str(getattr(role, "name", "")).casefold() == normalized
            ]
            matches.extend(exact)
            if not exact:
                matches.extend(
                    role
                    for role in getattr(guild, "roles", []) or []
                    if str(getattr(role, "name", "")).casefold().startswith(normalized)
                )
        unique: dict[int, discord.Role] = {
            int(getattr(role, "id", 0)): role
            for role in matches
            if getattr(role, "id", None) is not None
        }
        if len(unique) == 1:
            return next(iter(unique.values()))
        if len(unique) > 1:
            names = ", ".join(getattr(role, "name", "role") for role in list(unique.values())[:5])
            return tr(lang, f"I found multiple matching roles: {names}. Mention the exact role.", f"Encontre varios roles: {names}. Menciona el rol exacto.")
        mentioned = getattr(message, "role_mentions", None) or []
        if not candidates and mentioned:
            return mentioned[0]
        return tr(lang, "I could not find that role. Mention the exact role.", "No encontre ese rol. Menciona el rol exacto.")

    async def _resolve_admin_role_for_execution(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        lang: str,
    ) -> discord.Role | str:
        """Name matching is deliberately not a fallback: a same-name role is a different resource."""

        handle = admin_plan.get("resource_handle")
        if handle is None:
            return self._resolve_admin_target_role(message, admin_plan, lang)
        guild = message.guild
        if not isinstance(handle, dict) or str(handle.get("resource_type") or "").casefold() != "role":
            return tr(lang, "The supplied role handle is invalid.", "El identificador de rol proporcionado no es valido.")
        guild_id = self._coerce_positive_int(handle.get("guild_id"))
        resource_id = self._coerce_positive_int(handle.get("resource_id"))
        if guild is None or guild_id != self._coerce_positive_int(getattr(guild, "id", None)):
            return tr(lang, "The supplied role belongs to another server.", "El identificador de rol pertenece a otro servidor.")
        if resource_id is None:
            return tr(lang, "The supplied role handle is invalid.", "El identificador de rol proporcionado no es valido.")
        role = guild.get_role(resource_id) if callable(getattr(guild, "get_role", None)) else None
        if role is None:
            fetch_role = getattr(guild, "fetch_role", None)
            if callable(fetch_role):
                try:
                    role = await fetch_role(resource_id)
                except Exception:
                    logging.exception("Failed to fetch bound admin role")
                    role = None
        if role is None:
            return tr(lang, "I could not find the bound role.", "No encontre el rol vinculado.")
        if self._coerce_positive_int(getattr(role, "id", None)) != resource_id:
            return tr(lang, "The supplied role handle is invalid.", "El identificador de rol proporcionado no es valido.")
        role_guild = getattr(role, "guild", None)
        role_guild_id = getattr(role_guild, "id", None)
        if role_guild_id is not None and self._coerce_positive_int(role_guild_id) != guild_id:
            return tr(lang, "The supplied role belongs to another server.", "El identificador de rol pertenece a otro servidor.")
        return role

    async def _resolve_admin_channel_for_execution(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        lang: str,
    ) -> discord.TextChannel | str:
        handle = admin_plan.get("resource_handle")
        if handle is None:
            return self._resolve_admin_target_channel(message, admin_plan, lang)
        guild = message.guild
        if not isinstance(handle, dict) or str(handle.get("resource_type") or "").casefold() != "channel":
            return tr(lang, "The supplied channel handle is invalid.", "El identificador de canal proporcionado no es valido.")
        guild_id = self._coerce_positive_int(handle.get("guild_id"))
        resource_id = self._coerce_positive_int(handle.get("resource_id"))
        if guild is None or guild_id != self._coerce_positive_int(getattr(guild, "id", None)):
            return tr(lang, "The supplied channel belongs to another server.", "El identificador de canal pertenece a otro servidor.")
        if resource_id is None:
            return tr(lang, "The supplied channel handle is invalid.", "El identificador de canal proporcionado no es valido.")
        channel = guild.get_channel(resource_id) if callable(getattr(guild, "get_channel", None)) else None
        if channel is None:
            fetch_channel = getattr(guild, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(resource_id)
                except Exception:
                    logging.exception("Failed to fetch bound admin channel")
                    channel = None
        if channel is None:
            return tr(lang, "I could not find the bound channel.", "No encontre el canal vinculado.")
        if self._coerce_positive_int(getattr(channel, "id", None)) != resource_id:
            return tr(lang, "The supplied channel handle is invalid.", "El identificador de canal proporcionado no es valido.")
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild_id is not None and self._coerce_positive_int(channel_guild_id) != guild_id:
            return tr(lang, "The supplied channel belongs to another server.", "El identificador de canal pertenece a otro servidor.")
        if not isinstance(channel, discord.TextChannel) and not hasattr(channel, "set_permissions"):
            return tr(lang, "The supplied channel is not a text channel.", "El canal proporcionado no es de texto.")
        return channel

    @staticmethod
    def _parse_role_id(text: str) -> int | None:
        cleaned = text.strip()
        mention = re.fullmatch(r"<@&(\d{15,22})>", cleaned)
        if mention:
            return int(mention.group(1))
        if cleaned.isdigit():
            return int(cleaned)
        return None

    def _resolve_admin_target_channel(
        self,
        message: discord.Message,
        admin_plan: dict[str, Any],
        lang: str,
    ) -> discord.TextChannel | str:
        named = str(admin_plan.get("target_channel") or "").strip()
        # An id, a name, or a channel mention (<#id>), the form the prompt asks Grok to use.
        target = named.removeprefix("<#").removesuffix(">").lstrip("#").casefold()
        channels = getattr(message.guild, "text_channels", []) or []
        by_id = [channel for channel in channels if str(getattr(channel, "id", "")) == target]
        if by_id:
            return by_id[0]
        by_name = [channel for channel in channels if str(getattr(channel, "name", "")).casefold() == target]
        if len(by_name) > 1:
            return tr(lang, "I found several channels with that name. Use the exact channel id.", "Encontre varios canales con ese nombre. Usa el id exacto del canal.")
        if by_name:
            return by_name[0]
        # The current or a mentioned channel is a fallback only when no channel was named at all.
        mentioned = getattr(message, "channel_mentions", None) or []
        if not named and mentioned:
            return mentioned[0]
        if not named:
            return message.channel
        return tr(lang, "I could not find that channel.", "No encontre ese canal.")

    @staticmethod
    def _duration_label(seconds: int | None) -> str:
        if not seconds:
            return ""
        units = (
            (86400, "d"),
            (3600, "h"),
            (60, "m"),
        )
        for unit_seconds, suffix in units:
            if seconds % unit_seconds == 0 and seconds >= unit_seconds:
                return f"{seconds // unit_seconds}{suffix}"
        return f"{seconds}s"

    async def _execute_server_memory_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        lang: str,
    ) -> str | CapabilityTerminalResult:
        guild = message.guild
        if guild is None or self._server_memory is None:
            return "NOT_CONFIGURED"

        def refused(outcome: str, reason: str) -> CapabilityTerminalResult:
            # The reason goes back to Grok, which fixes the arguments or tells the user; nothing is posted from here.
            return normalize_terminal_result(
                "MEMORY", outcome, provider="server_memory", provenance={"source": "ServerMemoryService"}, reason=reason,
            )

        payload = decision.memory or {}
        raw_memory_type = payload.get("memory_type")
        # Grok leaves memory_type out of a lookup when the user named no category: that searches every category.
        untyped_lookup = decision.action == "SERVER_MEMORY_LOOKUP" and raw_memory_type is None
        memory_type_identity = str(raw_memory_type or "")
        normalization_identity = (
            "SERVER_FACT" if raw_memory_type is None and not untyped_lookup else memory_type_identity
        )
        bounded_memory_type_identity = memory_type_identity[:2400]
        input_type_name = type(raw_memory_type).__name__
        if input_type_name not in {"str", "int", "float", "bool", "list", "tuple", "dict", "NoneType"}:
            input_type_name = "other"
        logging.info(
            "AI server memory input boundary guild=%s channel=%s message=%s author=%s action=%s "
            "payload_present=%s memory_type_present=%s input_type=%s "
            "input_length=%s input_length_capped=%s memory_type_identity_hash=%s "
            "key_present=%s value_present=%s subject_present=%s stage=before_memory_type_normalization",
            getattr(guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            getattr(message.author, "id", None),
            decision.action,
            decision.memory is not None,
            "memory_type" in payload,
            input_type_name,
            len(bounded_memory_type_identity),
            len(memory_type_identity) > len(bounded_memory_type_identity),
            hash_text(bounded_memory_type_identity),
            bool(payload.get("key")),
            bool(payload.get("value")),
            bool(payload.get("subject")),
        )
        if untyped_lookup:
            memory_type = None
        else:
            try:
                memory_type = ServerMemoryService.normalize_memory_type(normalization_identity)
            except ValueError:
                return refused("VALIDATION_FAILED", f"memory_type must be one of {sorted(MEMORY_TYPES)} or left out")
        authority = self._route_authority_metadata(message)
        if memory_type == "BOT_BEHAVIOR_RULE" and not authority["author_can_manage_bot_behavior"]:
            logging.info(
                "AI server memory rejected guild=%s channel=%s author=%s memory_type=%s rejected_reason=not_trusted_admin author_is_bot_owner=%s author_has_manage_guild=%s author_can_manage_bot_behavior=%s",
                getattr(guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                memory_type,
                authority["author_is_bot_owner"],
                authority["author_has_manage_guild"],
                authority["author_can_manage_bot_behavior"],
            )
            return refused("PERMISSION_DENIED", "bot behavior rules need Manage Server or the bot owner")
        subject_user_id = self._memory_subject_user_id(message, payload)
        subject_channel_id = self._memory_subject_channel_id(message, payload)
        # A lookup naming only a type (no key, no subject) lists that type; it must not search an invented default key.
        type_level_lookup = (
            decision.action == "SERVER_MEMORY_LOOKUP" and subject_user_id is None and subject_channel_id is None
        )
        # A delete naming neither a key nor a member or channel names no exact record: the generic default key would match
        # whichever keyless record happens to carry it, so the miss hands back candidates instead.
        unaddressed_delete = (
            decision.action == "SERVER_MEMORY_DELETE" and subject_user_id is None and subject_channel_id is None
            and memory_type not in {"USER_NICKNAME", "USER_ALIAS"}
        )
        key = payload.get("key") or (
            "" if untyped_lookup or type_level_lookup or unaddressed_delete
            else self._default_memory_key(memory_type, subject_user_id, subject_channel_id)
        )

        if decision.action == "SERVER_MEMORY_LOOKUP":
            normalized_key = ServerMemoryService.normalize_key(key)
            rows = await self._server_memory.list_memories(
                guild.id,
                memory_type=memory_type,
                subject_user_id=subject_user_id,
                subject_channel_id=subject_channel_id,
                status="active",
                limit=10,
            )
            rows = [row for row in rows if str(row.get("key")) == normalized_key] if normalized_key else rows
            requested_key_identity = str(payload.get("key") or "")
            bounded_requested_key = requested_key_identity[:120]
            scope_identity = str(payload.get("scope") or "")[:120]
            subject_identity = str(payload.get("subject") or "")[:120]
            ambiguous_matches = untyped_lookup and bool(normalized_key) and len(rows) > 1
            lookup_outcome = (
                "VALIDATION_FAILED"
                if ambiguous_matches
                else "SUCCESS" if rows else "NO_DATA"
            )
            logging.info(
                "AI server memory lookup boundary guild=%s channel=%s message=%s "
                "canonical_memory_type_hash=%s requested_memory_type_hash=%s "
                "requested_key_length=%s requested_key_length_capped=%s requested_key_hash=%s "
                "scope_present=%s scope_hash=%s subject_present=%s subject_hash=%s "
                "normalized_lookup_key_hash=%s returned_row_count=%s terminal_outcome=%s",
                getattr(guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                hash_text(memory_type),
                hash_text(bounded_memory_type_identity),
                len(bounded_requested_key),
                len(requested_key_identity) > len(bounded_requested_key),
                hash_text(bounded_requested_key),
                bool(scope_identity),
                hash_text(scope_identity),
                bool(subject_identity),
                hash_text(subject_identity),
                hash_text(normalized_key[:120]),
                len(rows),
                lookup_outcome,
            )
            if not rows:
                return normalize_terminal_result(
                    "MEMORY",
                    "NO_DATA",
                    provider="server_memory",
                    payload={
                        "memory_type": memory_type,
                        "requested_key": normalized_key,
                        "subject": str(payload.get("subject") or "")[:120] or None,
                        "scope": str(payload.get("scope") or "")[:120] or None,
                        "rows": [],
                    },
                    provenance={"source": "ServerMemoryService"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    reason="memory_not_found",
                    satisfies_request=False,
                )
            if ambiguous_matches:
                return normalize_terminal_result(
                    "MEMORY",
                    "VALIDATION_FAILED",
                    provider="server_memory",
                    payload={
                        "memory_type": memory_type,
                        "requested_key": normalized_key,
                        "subject": str(payload.get("subject") or "")[:120] or None,
                        "scope": str(payload.get("scope") or "")[:120] or None,
                        "match_count": len(rows),
                        "rows": [],
                    },
                    provenance={"source": "ServerMemoryService"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    reason="multiple_exact_matches",
                    satisfies_request=False,
                )
            normalized_rows = [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "memory_type",
                        "subject_user_id",
                        "subject_channel_id",
                        "key",
                        "value",
                    )
                    if row.get(key) is not None
                }
                for row in rows[:10]
                if isinstance(row, dict)
            ]
            return normalize_terminal_result(
                "MEMORY",
                "SUCCESS",
                provider="server_memory",
                payload={
                    "memory_type": memory_type,
                    "requested_key": normalized_key,
                    "subject": str(payload.get("subject") or "")[:120] or None,
                    "scope": str(payload.get("scope") or "")[:120] or None,
                    "rows": normalized_rows,
                },
                provenance={"source": "ServerMemoryService"},
                scope={"guild_id": getattr(guild, "id", None)},
                satisfies_request=True,
            )

        if decision.action in {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE"}:
            value = payload.get("value", "")
            if memory_type.startswith("USER_") and subject_user_id is None:
                return refused("VALIDATION_FAILED", f"{memory_type} needs subject: the member's name, mention or id")
            if not value:
                return refused("VALIDATION_FAILED", "missing required argument: value")
            needs_approval = ServerMemoryService.should_require_approval(
                memory_type,
                author_can_manage=authority["author_can_manage_bot_behavior"],
                subject_is_author=subject_user_id is not None and subject_user_id == getattr(message.author, "id", None),
            )
            data = ServerMemoryInput(
                guild_id=guild.id,
                memory_type=memory_type,
                subject_user_id=subject_user_id,
                subject_channel_id=subject_channel_id,
                key=key,
                value=value,
                created_by_user_id=getattr(message.author, "id", 0),
                source_type="trusted_admin_instruction" if memory_type == "BOT_BEHAVIOR_RULE" else "ai",
                source_message_id=getattr(message, "id", None),
                approved_by_user_id=None if needs_approval else getattr(message.author, "id", None),
            )
            row = await (
                self._server_memory.create_pending_memory(data)
                if needs_approval
                else self._server_memory.create_memory(data)
            )
            # The subject of a personal memory may decide it too (see `_memory_decider_error`); everything else is administrators only.
            subject_may_decide = memory_type in SELF_DECIDABLE_MEMORY_TYPES and subject_user_id is not None
            text = (
                (
                    tr(lang, f"I made this a pending memory proposal (`{row.get('id')}`). An admin (someone who can manage the server) or the person it is about can approve it.", f"Deje esto como propuesta pendiente (`{row.get('id')}`). Un admin (alguien que pueda administrar el servidor) o la persona de quien trata puede aprobarla.")
                    if subject_may_decide
                    else tr(lang, f"I made this a pending memory proposal (`{row.get('id')}`). An admin (someone who can manage the server) can approve it.", f"Deje esto como propuesta pendiente (`{row.get('id')}`). Un admin (alguien que pueda administrar el servidor) puede aprobarla.")
                )
                if needs_approval
                else tr(lang, f"Saved server memory `{row.get('id')}`.", f"Guarde la memoria del servidor `{row.get('id')}`.")
            )
            logging.info(
                "AI server memory write guild=%s channel=%s author=%s action=%s memory_type=%s persisted_rule_key=%s memory_write_success=true author_is_bot_owner=%s author_has_manage_guild=%s author_can_manage_bot_behavior=%s",
                getattr(guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                decision.action,
                memory_type,
                ServerMemoryService.normalize_key(key),
                authority["author_is_bot_owner"],
                authority["author_has_manage_guild"],
                authority["author_can_manage_bot_behavior"],
            )
            record_id = row.get("id")
            if isinstance(record_id, str):
                record_id = record_id[:120]
            elif not isinstance(record_id, int):
                record_id = None
            outcome = "PENDING" if needs_approval else "SUCCESS"
            return normalize_terminal_result(
                "MEMORY",
                outcome,
                provider="server_memory",
                payload={
                    "memory_type": memory_type,
                    "record_id": record_id,
                    "action": decision.action,
                    "state": "pending" if needs_approval else "approved",
                },
                provenance={"source": "ServerMemoryService"},
                scope={"guild_id": getattr(guild, "id", None)},
                reason="approval_required" if needs_approval else None,
                satisfies_request=not needs_approval,
            )

        if decision.action == "SERVER_MEMORY_DELETE":
            rows = await self._server_memory.list_memories(
                guild.id,
                memory_type=memory_type,
                subject_user_id=subject_user_id,
                subject_channel_id=subject_channel_id,
                status="active",
                limit=20,
            )
            normalized_key = ServerMemoryService.normalize_key(key)
            target = next((row for row in rows if str(row.get("key")) == normalized_key), None)
            if target is None:
                # Keys are free-form router names, so a miss hands back the stored candidates (with ids) for the user to pick.
                candidates = [self._memory_row_summary(row) for row in rows[:10]]
                return normalize_terminal_result(
                    "MEMORY",
                    "NO_DATA",
                    provider="server_memory",
                    payload={
                        "memory_type": memory_type,
                        "action": decision.action,
                        "state": "needs_selection" if candidates else "not_found",
                        "candidates": candidates,
                    },
                    provenance={"source": "ServerMemoryService"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    reason="memory_needs_selection" if candidates else "memory_not_found",
                    satisfies_request=False,
                )
            if self._memory_decider_error(message, target):
                return normalize_terminal_result(
                    "MEMORY",
                    "PERMISSION_DENIED",
                    provider="orchestrator",
                    provenance={"source": "typed_task_operands"},
                    scope={"guild_id": getattr(guild, "id", None)},
                    reason="memory_lifecycle_not_authorized",
                    satisfies_request=False,
                )
            record_id = int(target["id"])
            await self._server_memory.delete_memory(guild.id, record_id)
            return normalize_terminal_result(
                "MEMORY",
                "SUCCESS",
                provider="server_memory",
                payload={
                    "memory_type": memory_type,
                    "record_id": record_id,
                    "action": decision.action,
                    "state": "deleted",
                },
                provenance={"source": "ServerMemoryService"},
                scope={"guild_id": getattr(guild, "id", None)},
                satisfies_request=True,
            )

        return "UNSUPPORTED"

    async def _server_context_with_memory(
        self,
        message: discord.Message,
        *,
        base_context: str,
        route_action: str,
        current_text: str,
        replied_message: discord.Message | None,
        participants: list[dict[str, Any]],
    ) -> str:
        guild = message.guild
        if guild is None or self._server_memory_context is None:
            return base_context
        try:
            memory_context = await self._server_memory_context.build_context(
                guild_id=guild.id,
                channel_id=getattr(message.channel, "id", None),
                author_user_id=getattr(message.author, "id", None),
                mentioned_user_ids=[
                    int(user.id)
                    for user in getattr(message, "mentions", [])
                    if not getattr(user, "bot", False)
                ],
                replied_user_id=(
                    int(replied_message.author.id)
                    if replied_message is not None and not getattr(replied_message.author, "bot", False)
                    else None
                ),
                participant_user_ids=[
                    int(item["author_id"]) for item in participants
                    if item.get("author_id") and not item.get("is_bot")
                ],
                user_label=lambda user_id: getattr(guild.get_member(user_id), "display_name", None),
                route_action=route_action,
                current_text=current_text,
            )
        except Exception:
            logging.exception("Failed to build server memory context")
            return base_context
        if not memory_context:
            return base_context
        base = (base_context or "").strip()
        return f"{base}\n\n{memory_context}".strip()

    def _memory_subject_user_id(
        self,
        message: discord.Message,
        payload: dict[str, str],
    ) -> int | None:
        # Grok names the subject; identity only: exactly one member's id, mention or name links the memory to that member.
        subject = " ".join(str(payload.get("subject") or "").split()).casefold()
        if not subject:
            return None
        members = [member for member in getattr(message.guild, "members", None) or () if not getattr(member, "bot", False)]
        token = re.fullmatch(r"<@!?(\d+)>|(\d{5,})", subject)
        if token:
            wanted = int(token.group(1) or token.group(2))
            return next((int(member.id) for member in members if int(member.id) == wanted), None)
        matches = {
            int(member.id)
            for member in members
            if subject in {str(name).casefold() for name in (member.name, member.display_name, getattr(member, "global_name", None)) if name}
        }
        return matches.pop() if len(matches) == 1 else None

    def _memory_subject_channel_id(self, message: discord.Message, payload: dict[str, str]) -> int | None:
        return int(message.channel.id) if str(payload.get("scope", "")).casefold() == "channel" else None

    @staticmethod
    def _default_memory_key(memory_type: str, subject_user_id: int | None, subject_channel_id: int | None) -> str:
        if memory_type == "USER_NICKNAME":
            return "preferred_nickname"
        if memory_type == "USER_ALIAS":
            return "alias"
        if subject_channel_id is not None:
            return "channel_context"
        if subject_user_id is not None:
            return "preference"
        return memory_type.casefold()


    def _log_capability_terminal_checkpoint(
        self,
        message: discord.Message,
        execution_context: CapabilityExecutionContext,
    ) -> None:
        diagnostics = execution_context.diagnostics()
        logging.info(
            "AI capability terminal guild=%s channel=%s message=%s complete=%s satisfied=%s missing=%s results=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            diagnostics["complete"],
            diagnostics["satisfied"],
            diagnostics["missing"],
            [
                {
                    "capability": item.get("capability"),
                    "status": item.get("status"),
                    "native_outcome": item.get("native_outcome"),
                    "satisfies_request": item.get("satisfies_request"),
                    "reason": item.get("reason"),
                }
                for item in diagnostics["results"]
            ],
        )
        if diagnostics["complete"] and diagnostics["satisfied"]:
            return
        failed_result = next(
            (
                item
                for item in diagnostics["results"]
                if item.get("status") != "SUCCESS" or not item.get("satisfies_request")
            ),
            None,
        )
        if failed_result is None:
            failure_stage = "capability_dependency"
            failure_capability = None
            failure_reason = "missing_required_result"
            failure_provider = "orchestrator"
            failure_contract = "required_capability_result"
        else:
            failure_stage = "capability_result"
            failure_capability = failed_result.get("capability")
            failure_reason = failed_result.get("reason") or failed_result.get("status")
            failure_provider = failed_result.get("provider") or "unknown"
            provenance = failed_result.get("provenance") or {}
            failure_contract = provenance.get("source") or "capability_execution"
        requirements = execution_context.execution_plan.semantic_plan.requirements
        requirement_ids = tuple(
            item.requirement_id
            for item in requirements
            if not failure_capability or item.capability_owner == failure_capability
        )[:4]
        logging.info(
            "AI capability terminal failure diagnostic guild=%s channel=%s message=%s failure_stage=%s capability=%s requirement_ids=%s reason=%s contract=%s provider=%s complete=%s satisfied=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            failure_stage,
            failure_capability,
            requirement_ids,
            failure_reason,
            failure_contract,
            failure_provider,
            diagnostics["complete"],
            diagnostics["satisfied"],
        )

    async def _execute_ninjas_result_only(self, operands: dict[str, object]) -> CapabilityTerminalResult:
        operation = operands.get("ninjas_action")
        scope = {"operation": operation}
        client = getattr(self.bot, "ninjas_client", None)
        if client is None:
            return normalize_terminal_result(
                "NINJAS",
                CapabilityTerminalStatus.PROVIDER_FAILURE,
                provider="api_ninjas",
                provenance={"source": "ApiNinjasClient"},
                scope=scope,
                reason="ninjas_client_unavailable",
                satisfies_request=False,
            )
        try:
            payload = await execute_ninjas_operation(
                client,
                str(operation),
                domain=operands.get("domain"),
            )
        except NinjasOperandError:
            return normalize_terminal_result(
                "NINJAS",
                CapabilityTerminalStatus.VALIDATION_FAILED,
                provider="orchestrator",
                provenance={"source": "typed_task_operands"},
                scope=scope,
                reason="missing_ninjas_operand",
                satisfies_request=False,
            )
        except Exception as exc:
            logging.exception("AI NINJAS result-only retrieval failed")
            return normalize_terminal_result(
                "NINJAS",
                CapabilityTerminalStatus.PROVIDER_FAILURE,
                provider="api_ninjas",
                provenance={"source": "ApiNinjasClient"},
                scope=scope,
                # The provider's own message, so Grok can see what to fix.
                reason=str(exc)[:200] or type(exc).__name__,
                satisfies_request=False,
            )
        return normalize_terminal_result(
            "NINJAS",
            CapabilityTerminalStatus.SUCCESS,
            provider="api_ninjas",
            payload=payload,
            provenance={"source": "ApiNinjasClient"},
            scope=scope,
            satisfies_request=True,
        )

    async def _execute_code_result_only(self, operands: dict[str, object]) -> CapabilityTerminalResult:
        operation = operands.get("code_action")
        language = operands.get("code_language")
        code = operands.get("code")
        scope = {"operation": operation, "language": language}

        def failure(status: CapabilityTerminalStatus, provider: str, source: str, reason: str) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "CODE", status, provider=provider, provenance={"source": source},
                scope=scope, reason=reason, satisfies_request=False,
            )

        if operation == "languages":
            return normalize_terminal_result(
                "CODE",
                CapabilityTerminalStatus.SUCCESS,
                provider="glot",
                payload={"operation": operation, "languages": list(RUN_FILENAMES)},
                provenance={"source": "GlotClient"},
                scope={"operation": operation},
                satisfies_request=True,
            )
        if operation != "run" or language not in RUN_FILENAMES or not isinstance(code, str) or not code.strip():
            return failure(
                CapabilityTerminalStatus.VALIDATION_FAILED, "orchestrator", "typed_task_operands", "invalid_code_operand",
            )
        client = getattr(self.bot, "glot_client", None)
        if client is None:
            return failure(
                CapabilityTerminalStatus.PROVIDER_FAILURE, "glot", "GlotClient", "code_client_unavailable",
            )
        try:
            outcome = await execute_run(client, language, code)
        except Exception as exc:
            logging.exception("AI CODE result-only run failed")
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, "glot", "GlotClient", type(exc).__name__)
        # A program that ran and exited non-zero is a normal result: stderr/error
        # stay in the payload for the synthesizer, exactly as `/code` reports them.
        return normalize_terminal_result(
            "CODE",
            CapabilityTerminalStatus.SUCCESS,
            provider="glot",
            payload={
                "operation": operation,
                "language": language,
                "stdout": truncate_output(outcome["stdout"]),
                "stderr": truncate_output(outcome["stderr"]),
                "error": truncate_output(outcome["error"]),
            },
            provenance={"source": "GlotClient"},
            scope=scope,
            satisfies_request=True,
        )

    @staticmethod
    def _reminder_operand_error(message: discord.Message, operands: dict[str, object]) -> str | None:
        action = operands.get("reminder_action")
        duration, text, reminder_id = (operands.get(key) for key in ("reminder_duration", "reminder_message", "reminder_id"))
        if action == "create":
            complete = isinstance(duration, str) and isinstance(text, str)
        elif action == "remove":
            complete = isinstance(reminder_id, int) and not isinstance(reminder_id, bool) and reminder_id >= 1
        else:
            complete = action == "list"
        if not complete:
            needed = {"create": "reminder_duration and reminder_message", "remove": "reminder_id"}.get(str(action), "reminder_action")
            return f"missing or invalid argument: reminder {action} needs {needed}"
        if message.guild is None or (action == "create" and not isinstance(message.channel, discord.TextChannel)):
            return "reminder_requires_guild_channel"
        if action == "create":
            try:
                validate_reminder(duration, text)
            except ReminderError as exc:
                return str(exc)
        return None

    async def _execute_reminder_result_only(
        self, message: discord.Message, operands: dict[str, object],
    ) -> CapabilityTerminalResult:
        operation = operands.get("reminder_action")

        def outcome(
            status: CapabilityTerminalStatus, *, payload: object | None = None, reason: str | None = None,
            provider: str = "database", source: str = "reminders",
        ) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "REMINDER", status, provider=provider, payload=payload,
                provenance={"source": source}, scope={"operation": operation}, reason=reason,
            )

        # Times are written in the request timezone the router already receives, so the user's own clock matches.
        local_zone = getattr(getattr(self.bot, "api_football_client", None), "display_timezone", None)

        def due_at(timestamp: object) -> str:
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(timestamp))
            return (moment.astimezone(local_zone) if isinstance(local_zone, tzinfo) else moment).isoformat()

        reason = self._reminder_operand_error(message, operands)
        if reason:
            return outcome(
                CapabilityTerminalStatus.VALIDATION_FAILED, reason=reason,
                provider="orchestrator", source="typed_task_operands",
            )
        db = self.bot.db
        guild_id, user_id = message.guild.id, message.author.id
        try:
            if operation == "create":
                created = await create_reminder(
                    db, guild_id, user_id, message.channel.id,
                    operands["reminder_duration"], operands["reminder_message"],
                )
                payload = {
                    "operation": operation, "reminder_id": created["reminder_id"],
                    "due_at": due_at(created["remind_at"]), "message": operands["reminder_message"].strip()[:200],
                    "delivery": "direct_message",
                }
            elif operation == "list":
                rows = await list_reminders(db, guild_id, user_id)
                payload = {
                    "operation": operation,
                    "reminders": [
                        {"id": int(row["reminder_id"]), "message": str(row["message"])[:200], "due_at": due_at(row["remind_at"])}
                        for row in rows[:25]
                    ],
                }
            else:
                removed = await remove_reminder(db, guild_id, user_id, operands["reminder_id"])
                if removed is None:
                    return outcome(CapabilityTerminalStatus.NO_DATA, reason="reminder_not_found")
                payload = {
                    "operation": operation, "reminder_id": operands["reminder_id"], "removed_or_unsubscribed": removed,
                }
        except Exception as exc:
            logging.exception("AI REMINDER result-only run failed")
            return outcome(CapabilityTerminalStatus.PROVIDER_FAILURE, reason=type(exc).__name__)
        return outcome(CapabilityTerminalStatus.SUCCESS, payload=payload)

    def _birthday_settings_plan(
        self, message: discord.Message, operands: dict[str, object],
    ) -> tuple[object | None, object | None] | str:
        action = operands["birthday_action"]
        if message.guild is None:
            return "birthday_requires_guild"
        # Same authority as the explicit commands (administrator, bot-owner bypass), read at guild level as typed ADMIN does.
        authority = self._route_authority_metadata(message)
        if not (authority["author_is_bot_owner"] or authority["author_is_guild_owner"] or authority["author_has_administrator"]):
            return "birthday_settings_not_authorized"
        supplied = {key for key, value in operands.items() if key != "birthday_action" and value not in (None, [])}
        texts = {key: operands.get(key) for key in ("birthday_timezone", "birthday_mode", "birthday_event", "birthday_event_color", "birthday_event_image", "birthday_event_message", "target_channel", "birthday_template_type", "birthday_template")}
        if supplied - BIRTHDAY_SETTINGS_OPERANDS[action] or any(value is not None and not isinstance(value, str) for value in texts.values()):
            return "invalid_birthday_operand"
        channel = role = None
        target, candidates = texts["target_channel"], operands.get("target_role_candidates")
        if action == "set_channel" or (action == "setup" and target is not None):
            channel = self._resolve_admin_target_channel(message, {"target_channel": target}, "en") if target and target.strip().lstrip("#") else None
            if channel is None or isinstance(channel, str):
                return "birthday_channel_not_found"
        if action in ("set_role", "blacklist_role") or (action in ("setup", "trusted") and candidates):
            role = self._resolve_admin_target_role(message, {"target_role_candidates": candidates}, "en") if candidates else None
            if role is None or isinstance(role, str):
                return "birthday_role_not_found"
        event = texts["birthday_event"]
        if action == "set_timezone" and validate_timezone(texts["birthday_timezone"]) is None:
            return "invalid_timezone"
        if action == "set_mode" and texts["birthday_mode"] not in BIRTHDAY_MODES:
            return "invalid_mode"
        if action == "set_ages" and not isinstance(operands.get("birthday_ages"), bool):
            return "invalid_birthday_operand"
        if action in ("event", "preview") and (event not in BIRTHDAY_EVENTS or (action == "preview" and event == "disable")):
            return "invalid_event"
        if action == "event" and texts["birthday_event_color"] is not None and normalize_hex_color(texts["birthday_event_color"]) is None:
            return "invalid_color"
        if action in ("template_add", "template_list", "template_remove") and texts["birthday_template_type"] not in BIRTHDAY_TEMPLATE_EVENTS:
            return "invalid_event"
        if action == "template_add" and not (texts["birthday_template"] or "").strip():
            return "empty_template"
        template_id = operands.get("birthday_template_id")
        if action == "template_remove" and not (isinstance(template_id, int) and not isinstance(template_id, bool) and template_id >= 1):
            return "invalid_birthday_operand"
        # The commands' `blocked` has no default; the trusted flags default to false.
        if action in ("blacklist_user", "blacklist_role") and not isinstance(operands.get("birthday_blocked"), bool):
            return "invalid_birthday_operand"
        if action == "trusted" and any(
            operands.get(key) is not None and not isinstance(operands.get(key), bool)
            for key in ("birthday_block_message", "birthday_block_role", "birthday_block_list")
        ):
            return "invalid_birthday_operand"
        return channel, role

    def _birthday_operand_error(self, message: discord.Message, operands: dict[str, object]) -> str | None:
        action = operands.get("birthday_action")
        if action in BIRTHDAY_SETTINGS_OPERANDS:
            plan = self._birthday_settings_plan(message, operands)
            return plan if isinstance(plan, str) else None
        month, day, year, count = (operands.get(key) for key in ("birth_month", "birth_day", "birth_year", "birthday_count"))

        def typed(value: object) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        # Only view takes a target: show, set and remove always concern the message author.
        target_id = operands.get("target_user_id")
        targeted = (target_id is not None and target_id != message.author.id) or bool(operands.get("target_user_candidates"))
        if action == "set":
            complete = typed(month) and typed(day) and (year is None or typed(year)) and not targeted
        elif action in ("show", "remove"):
            complete = not targeted
        elif action == "next":
            complete = count is None or (typed(count) and 1 <= count <= 25)
        else:
            complete = action == "view"
        if not complete:
            return "invalid_birthday_operand"
        if message.guild is None:
            return "birthday_requires_guild"
        if action == "set":
            try:
                validate_birthday(month, day, year)
            except BirthdayError as exc:
                return str(exc)
        return None

    async def _execute_birthday_result_only(
        self, message: discord.Message, operands: dict[str, object],
    ) -> CapabilityTerminalResult:
        """Settings operations check administrator authority before any database call."""

        operation = operands.get("birthday_action")

        def outcome(
            status: CapabilityTerminalStatus, *, payload: object | None = None, reason: str | None = None,
            provider: str = "database", source: str = "birthdays",
        ) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "BIRTHDAY", status, provider=provider, payload=payload,
                provenance={"source": source}, scope={"operation": operation}, reason=reason,
            )

        def invalid(reason: str) -> CapabilityTerminalResult:
            return outcome(
                CapabilityTerminalStatus.VALIDATION_FAILED, reason=reason,
                provider="orchestrator", source="typed_task_operands",
            )

        reason = self._birthday_operand_error(message, operands)
        if reason == "birthday_settings_not_authorized":
            return outcome(
                CapabilityTerminalStatus.PERMISSION_DENIED, reason=reason,
                provider="orchestrator", source="typed_task_operands",
            )
        if reason:
            return invalid(reason)
        db, guild, subject_id = self.bot.db, message.guild, message.author.id
        if operation == "view":
            # Same structural target resolution as typed ADMIN: exact id, named candidates, then message mentions.
            target = await self._resolve_admin_target_member(message, operands, "en")
            if isinstance(target, str):
                return invalid("invalid_birthday_operand")
            subject_id = target.id
        try:
            if operation in BIRTHDAY_SETTINGS_OPERANDS:
                return outcome(CapabilityTerminalStatus.SUCCESS, payload=await self._apply_birthday_setting(message, operands))
            if not await birthday_module_enabled(db, guild.id):
                return outcome(CapabilityTerminalStatus.NO_DATA, reason="birthday_module_disabled")
            if operation == "set":
                await set_birthday(
                    db, guild.id, subject_id, operands["birth_month"], operands["birth_day"], operands.get("birth_year"),
                )
                payload = {"operation": operation, "month": operands["birth_month"], "day": operands["birth_day"]}
            elif operation == "remove":
                if not await remove_birthday(db, guild.id, subject_id):
                    return outcome(CapabilityTerminalStatus.NO_DATA, reason="birthday_not_set")
                payload = {"operation": operation}
            elif operation == "next":
                rows = await next_birthdays(db, guild, operands.get("birthday_count") or 10)
                payload = {
                    "operation": operation,
                    "birthdays": [
                        {"user_id": row["member"].id, "month": row["month"], "day": row["day"], "days_until": row["days_until"]}
                        for row in rows
                    ],
                }
            else:
                # `show` is the author's own profile; `view` of someone else honors the blacklist exactly like the command.
                hidden = operation == "view" and await view_hidden_by_blacklist(db, guild, target, message.author.id)
                profile = None if hidden else await view_birthday(db, guild.id, subject_id)
                if profile is None:
                    return outcome(CapabilityTerminalStatus.NO_DATA, reason="birthday_not_set")
                payload = {
                    "operation": operation, "user_id": subject_id, "month": profile["month"],
                    "day": profile["day"], "year_or_null": profile["birth_year"],
                }
        except BirthdayError as exc:
            if str(exc) in ("template_limit_reached", "template_not_found"):
                return outcome(CapabilityTerminalStatus.NO_DATA, reason=str(exc))
            return invalid(str(exc))
        except Exception as exc:
            logging.exception("AI BIRTHDAY result-only run failed")
            return outcome(CapabilityTerminalStatus.PROVIDER_FAILURE, reason=type(exc).__name__)
        return outcome(CapabilityTerminalStatus.SUCCESS, payload=payload)

    async def _birthday_preview_target(self, message: discord.Message) -> tuple[object, object] | None:
        cog = self.bot.get_cog("BirthdaysCog")
        channel = await preview_channel(self.bot.db, message.guild, message.channel)
        return (cog, channel) if cog is not None and channel is not None else None

    async def _birthday_blacklist_member(self, message: discord.Message, operands: dict[str, object]) -> object | None:
        member = await self._resolve_admin_target_member(message, operands, "en")
        return None if isinstance(member, str) else member

    async def _apply_birthday_setting(self, message: discord.Message, operands: dict[str, object]) -> dict[str, object]:
        operation, db, guild = operands["birthday_action"], self.bot.db, message.guild
        channel, role = self._birthday_settings_plan(message, operands)

        def done(field: str, value: object) -> dict[str, object]:
            return {"operation": operation, "changed_field": field, "new_value": value}

        if operation == "setup":
            settings = await db.update_birthday_guild_settings(
                guild.id, enabled=True,
                channel_id=channel.id if channel is not None else Ellipsis,
                role_id=role.id if role is not None else Ellipsis,
            )
            return done("module_setup", {key: settings.get(key) for key in ("channel_id", "role_id", "server_timezone")})
        if operation == "set_channel":
            await db.update_birthday_guild_settings(guild.id, channel_id=channel.id)
            return done("channel_id", channel.id)
        if operation == "set_role":
            await db.update_birthday_guild_settings(guild.id, role_id=role.id)
            return done("role_id", role.id)
        if operation == "set_timezone":
            return done("server_timezone", await set_birthday_timezone(db, guild.id, operands["birthday_timezone"]))
        if operation == "set_mode":
            return done("birthday_timezone_mode", await set_birthday_mode(db, guild.id, operands["birthday_mode"]))
        if operation == "set_ages":
            await set_birthday_ages(db, guild.id, operands["birthday_ages"])
            return done("ages_visible", operands["birthday_ages"])
        if operation == "event":
            event_type, variant, cfg = await set_birthday_event(
                db, guild.id, operands["birthday_event"], operands.get("birthday_event_color"),
                operands.get("birthday_event_image"), operands.get("birthday_event_message"),
            )
            if event_type is None:
                return done("module_enabled", False)
            return done("event_settings", {
                "event_type": event_type, "variant": variant, "embed_color": cfg.get("embed_color"),
                **{key: str(cfg.get(key) or "")[:200] for key in ("embed_image_url", "birthday_message_no_year", "birthday_message_with_age")},
            })
        event_type, blocked = operands.get("birthday_template_type"), operands.get("birthday_blocked")
        if operation == "template_add":
            template_id = await add_birthday_template(db, guild.id, event_type, operands["birthday_template"])
            return done("template_added", {"event_type": event_type, "template_id": template_id})
        if operation == "template_list":
            rows = await list_birthday_templates(db, guild.id, event_type)
            return {
                "operation": operation, "event_type": event_type, "total": len(rows),
                "templates": [{"id": int(row["id"]), "text": str(row["template_text"]).strip()[:200]} for row in rows[:25]],
            }
        if operation == "template_remove":
            if not await remove_birthday_template(db, guild.id, event_type, operands["birthday_template_id"]):
                raise BirthdayError("template_not_found")
            return done("template_removed", {"event_type": event_type, "template_id": operands["birthday_template_id"]})
        if operation == "blacklist_user":
            member = await self._birthday_blacklist_member(message, operands)
            if member is None:
                raise BirthdayError("birthday_user_not_found")
            await set_birthday_blacklist_user(db, guild.id, member.id, blocked)
            return done("blacklist_user", {"user_id": member.id, "blocked": blocked})
        if operation == "blacklist_role":
            await set_birthday_blacklist_role(db, guild.id, role.id, blocked)
            return done("blacklist_role", {"role_id": role.id, "blocked": blocked})
        if operation == "trusted":
            flags = [operands.get(key) is True for key in ("birthday_block_message", "birthday_block_role", "birthday_block_list")]
            await set_birthday_trusted(db, guild.id, role.id if role is not None else None, *flags)
            return done("trusted_role", {
                "role_id": role.id if role is not None else None,
                "block_message": flags[0], "block_role": flags[1], "block_list": flags[2],
            })
        target = await self._birthday_preview_target(message)
        if target is None:
            raise BirthdayError("birthday_preview_unavailable")
        await preview_birthday_event(target[0], guild, message.author, target[1], operands["birthday_event"], await self._lang(guild))
        return done("preview_posted", {"event": operands["birthday_event"], "channel_id": target[1].id})

    @staticmethod
    def _memory_is_actionable(row: dict[str, object] | None, operation: object = None) -> bool:
        """Pending rows past their expiry are not actionable; expiry is swept lazily."""

        if operation == "SERVER_MEMORY_DELETE":
            return row is not None
        if row is None or row.get("status") != "pending":
            return False
        expires_at = row.get("expires_at")
        if not expires_at:
            return True
        expiry = datetime.fromisoformat(str(expires_at))
        return (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc)

    @staticmethod
    def _memory_row_summary(row: dict[str, object]) -> dict[str, object]:
        return {
            **{key: row.get(key) for key in ("id", "memory_type", "key", "status")},
            "value": str(row.get("value") or "")[:200],
            **{key: row[key] for key in ("subject_user_id", "subject_channel_id") if row.get(key) is not None},
        }

    def _memory_decider_error(self, message: discord.Message, row: dict[str, object] | None) -> str | None:
        """A missing row gets the same denial as a forbidden one, so a non-admin cannot probe which ids exist."""

        if self._route_authority_metadata(message)["author_can_manage_bot_behavior"]:
            return None
        if (
            row is not None
            and row.get("memory_type") in SELF_DECIDABLE_MEMORY_TYPES
            and row.get("subject_user_id") is not None
            and row.get("subject_user_id") == message.author.id
        ):
            return None
        return "memory_lifecycle_not_authorized"

    def _memory_lifecycle_operand_error(self, message: discord.Message, operands: dict[str, object]) -> str | None:
        if message.guild is None or self._server_memory is None:
            return "memory_requires_guild"
        # Administrators (manage_guild with the owner bypass, like /servercontext) may do everything; anyone else may only
        # approve, reject or delete, and only a memory about themselves: that row-dependent check is `_memory_decider_error`.
        if operands.get("memory_action") not in {"SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT", "SERVER_MEMORY_DELETE"} and not self._route_authority_metadata(message)["author_can_manage_bot_behavior"]:
            return "memory_lifecycle_not_authorized"
        memory_id = operands.get("memory_id")
        if operands.get("memory_action") != "SERVER_MEMORY_LIST" and not (
            isinstance(memory_id, int) and not isinstance(memory_id, bool) and memory_id >= 1
        ):
            return "invalid_memory_operand"
        return None

    async def _execute_memory_lifecycle_result_only(
        self, message: discord.Message, operands: dict[str, object],
    ) -> CapabilityTerminalResult:
        operation = operands.get("memory_action")

        def outcome(
            status: CapabilityTerminalStatus, *, payload: object | None = None, reason: str | None = None,
            provider: str = "server_memory", source: str = "ServerMemoryService",
        ) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "MEMORY", status, provider=provider, payload=payload,
                provenance={"source": source}, scope={"operation": operation}, reason=reason,
            )

        reason = self._memory_lifecycle_operand_error(message, operands)
        if reason:
            return outcome(
                CapabilityTerminalStatus.PERMISSION_DENIED
                if reason == "memory_lifecycle_not_authorized"
                else CapabilityTerminalStatus.VALIDATION_FAILED,
                reason=reason, provider="orchestrator", source="typed_task_operands",
            )
        service, guild_id = self._server_memory, message.guild.id
        try:
            if operation == "SERVER_MEMORY_LIST":
                status = operands.get("memory_status") or "active"
                memory_type = operands.get("memory_type") or None
                try:
                    rows = await service.list_memories(guild_id, memory_type=memory_type, status=status, limit=25)
                except ValueError:
                    return outcome(
                        CapabilityTerminalStatus.VALIDATION_FAILED, reason="invalid_memory_operand",
                        provider="orchestrator", source="typed_task_operands",
                    )
                return outcome(
                    CapabilityTerminalStatus.SUCCESS,
                    payload={
                        "operation": operation,
                        "memory_status": status,
                        "memories": [self._memory_row_summary(row) for row in rows[:25]],
                    },
                )
            memory_id = operands["memory_id"]
            row = await service.get_memory(guild_id, memory_id)
            if self._memory_decider_error(message, row):
                return outcome(
                    CapabilityTerminalStatus.PERMISSION_DENIED, reason="memory_lifecycle_not_authorized",
                    provider="orchestrator", source="typed_task_operands",
                )
            if not self._memory_is_actionable(row, operation):
                return outcome(
                    CapabilityTerminalStatus.NO_DATA,
                    reason="memory_not_found" if row is None or operation == "SERVER_MEMORY_DELETE" else "memory_not_pending",
                )
            if operation == "SERVER_MEMORY_DELETE":
                status_after, done = "deleted", await service.delete_memory(guild_id, memory_id)
            else:
                approve = operation == "SERVER_MEMORY_APPROVE"
                status_after = "active" if approve else "rejected"
                done = await (service.approve_memory if approve else service.reject_memory)(guild_id, memory_id, message.author.id)
            if not done:
                return outcome(CapabilityTerminalStatus.NO_DATA, reason="memory_not_found")
        except Exception as exc:
            logging.exception("AI MEMORY lifecycle result-only run failed")
            return outcome(CapabilityTerminalStatus.PROVIDER_FAILURE, reason=type(exc).__name__)
        return outcome(
            CapabilityTerminalStatus.SUCCESS,
            payload={"operation": operation, "memory_id": memory_id, "status_after": status_after},
        )

    async def _execute_translate_result_only(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
        operands: dict[str, object],
    ) -> CapabilityTerminalResult:
        def failure(status: CapabilityTerminalStatus, reason: str, *, provider: str = "orchestrator") -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "TRANSLATE",
                status,
                provider=provider,
                provenance={"source": "typed_task_operands" if provider == "orchestrator" else "xai_translate"},
                reason=reason,
                satisfies_request=False,
            )

        target = " ".join(str(operands.get("target_language") or "").split())
        if not target:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "missing_translate_operand")
        text = str(operands.get("text") or "").strip()
        source = operands.get("text_source")
        if bool(text) == bool(source):
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "translate_text_source_invalid")
        if source:
            # Structural authority only: the exact referenced, non-bot message.
            authorized, _content = self._authorized_reply_source(message, replied_message)
            text = str(getattr(replied_message, "content", "") or "").strip() if authorized else ""
            if not text:
                return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "reply_source_unavailable")
            if len(text) > 1800:
                return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "translate_text_too_long")
        try:
            translated = await self.bot.llm_client.translate(text=text, target_language=target)
        except Exception as exc:
            logging.exception("AI TRANSLATE result-only translation failed")
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, type(exc).__name__, provider="xai_translate")
        if getattr(translated, "refused", False):
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, "translation_refused", provider="xai_translate")
        if getattr(translated, "incomplete", False):
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, "translation_incomplete", provider="xai_translate")
        return normalize_terminal_result(
            "TRANSLATE",
            CapabilityTerminalStatus.SUCCESS,
            provider="xai_translate",
            payload={
                "text": str(translated),
                "target_language": target,
                "text_source": "replied_message" if source else "provided",
            },
            provenance={"source": "xai_translate"},
            scope={"target_language": target},
            satisfies_request=True,
        )

    async def _execute_meme_result_only(
        self,
        operands: dict[str, object],
        source: dict[str, object] | None,
    ) -> CapabilityTerminalResult:
        def failure(status: CapabilityTerminalStatus, reason: str) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                "MEME",
                status,
                provider="memegen",
                provenance={"source": "memegen"},
                reason=reason,
                satisfies_request=False,
            )

        client = getattr(self.bot, "memegen_client", None)
        if client is None:
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, "memegen_unavailable")
        action = str(operands.get("meme_action") or "")
        if action not in MEME_OPERATIONS:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "invalid_meme_action")
        top = str(operands.get("meme_top") or "").strip()
        bottom = str(operands.get("meme_bottom") or "").strip()
        def caption_text(value: object) -> str:
            payload = getattr(value, "payload", value)
            if isinstance(payload, dict):
                payload = next((payload[key] for key in ("text", "answer") if isinstance(payload.get(key), str)), None)
            return payload.strip() if isinstance(payload, str) else ""

        sources = [operands[key] for key in ("answer", "text", "result") if operands.get(key) is not None]
        slot = str(operands.get("meme_text_slot") or "")
        if sources or slot:
            texts = [text for text in map(caption_text, sources) if text]
            if len(sources) != 1 or len(texts) != 1 or not slot:
                reason = "missing_meme_text_slot" if len(texts) == 1 and not slot else "invalid_meme_text_binding"
                return failure(CapabilityTerminalStatus.VALIDATION_FAILED, reason)
            if {"top": top, "bottom": bottom}[slot]:
                return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "meme_text_slot_conflict")
            if len(texts[0]) > 200:
                return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "meme_caption_too_long")
            top, bottom = (texts[0], bottom) if slot == "top" else (top, texts[0])
        if not top and not bottom:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "missing_meme_caption")
        template = str(operands.get("meme_template") or "").strip()
        if action == "template" and not template:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "missing_meme_template")
        if action != "template" and template:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "unexpected_meme_template")
        template_id: str | None = None
        try:
            if action == "custom":
                url = client.build_custom_url(background_url=str(source["url"]), top_text=top, bottom_text=bottom)
            else:
                if action == "template":
                    templates = await client.list_templates()
                    needle = template.casefold()
                    match = next(
                        (
                            item for item in templates
                            if needle in (str(item.get("id") or "").casefold(), str(item.get("name") or "").casefold())
                        ),
                        None,
                    )
                    if match is None:
                        return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "meme_template_not_found")
                    template_id = str(match["id"])
                else:
                    template_id = await client.pick_random_template()
                url = client.build_template_url(template_id=template_id, top_text=top, bottom_text=bottom)
            image_bytes, content_type = await client.fetch_image(url)
        except ValueError:
            return failure(CapabilityTerminalStatus.VALIDATION_FAILED, "invalid_meme_request")
        except Exception as exc:
            logging.exception("AI MEME result-only render failed")
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, type(exc).__name__)
        if content_type != "image/png":
            return failure(CapabilityTerminalStatus.PROVIDER_FAILURE, "meme_not_png")
        return self._image_artifact_terminal_result(
            "MEME",
            image_bytes,
            source="memegen",
            filename="nitori_meme.png",
            provider="memegen",
            provenance={"execution_path": f"memegen_{action}", "template_id": template_id},
            scope={"template_id": template_id},
        )

    async def _execute_minecraft_status_result_only(
        self,
        minecraft: object,
    ) -> CapabilityTerminalResult:
        operands = minecraft if isinstance(minecraft, dict) else {}
        address = " ".join(str(operands.get("address") or "").split())[:255]
        if not address:
            return normalize_terminal_result(
                "MINECRAFT",
                CapabilityTerminalStatus.PENDING,
                provider="mcsrvstat.us",
                provenance={"source": "typed_capability_operand"},
                reason="minecraft_address_required",
                satisfies_request=False,
            )
        mc_client = getattr(self.bot, "mc_client", None)
        if mc_client is None or not hasattr(mc_client, "get_status"):
            return normalize_terminal_result(
                "MINECRAFT",
                CapabilityTerminalStatus.PROVIDER_FAILURE,
                provider="mcsrvstat.us",
                provenance={"source": "McSrvStatClient"},
                scope={"address": address, "fresh": False},
                reason="minecraft_status_client_unavailable",
                satisfies_request=False,
            )
        try:
            data = await mc_client.get_status(address)
        except Exception as exc:
            logging.exception("AI Minecraft status result-only retrieval failed")
            return normalize_terminal_result(
                "MINECRAFT",
                CapabilityTerminalStatus.PROVIDER_FAILURE,
                provider="mcsrvstat.us",
                provenance={"source": "McSrvStatClient", "cache_hit": False},
                scope={"address": address, "fresh": False},
                reason=type(exc).__name__,
                satisfies_request=False,
            )
        if not isinstance(data, dict):
            return normalize_terminal_result(
                "MINECRAFT",
                CapabilityTerminalStatus.PROVIDER_FAILURE,
                provider="mcsrvstat.us",
                provenance={"source": "McSrvStatClient", "cache_hit": False},
                scope={"address": address, "fresh": False},
                reason="invalid_minecraft_status_payload",
                satisfies_request=False,
            )

        players = data.get("players") if isinstance(data.get("players"), dict) else {}
        motd_data = data.get("motd") if isinstance(data.get("motd"), dict) else {}
        motd_rows = motd_data.get("clean") if isinstance(motd_data.get("clean"), list) else []

        def bounded_count(value: object) -> int | None:
            try:
                count = int(value)
            except (TypeError, ValueError):
                return None
            return count if count >= 0 else None

        payload = {
            "address": address,
            "online": bool(data.get("online")),
            "ip": " ".join(str(data.get("ip") or "").split())[:255] or None,
            "port": bounded_count(data.get("port")),
            "version": " ".join(str(data.get("version") or "").split())[:120] or None,
            "players_online": bounded_count(players.get("online")),
            "players_max": bounded_count(players.get("max")),
            "motd": " | ".join(" ".join(str(row).split()) for row in motd_rows[:8])[:500] or None,
        }
        return normalize_terminal_result(
            "MINECRAFT",
            CapabilityTerminalStatus.SUCCESS,
            provider="mcsrvstat.us",
            payload=payload,
            provenance={"source": "McSrvStatClient", "cache_hit": False},
            scope={"address": address, "fresh": True},
        )

    async def _execute_image_analysis_result_only(
        self,
        message: discord.Message,
        user_prompt: str,
        context: CapabilityExecutionContext,
        *,
        settings: object,
        fallback_media_urls: list[str] | tuple[str, ...] = (),
        fallback_media_source_kind: str = "none",
        source_message_id: int | None = None,
        source_entries: tuple[dict[str, object], ...] = (),
        selection_count: int = 1,
        dependency_task_ids: tuple[str, ...] = (),
    ) -> CapabilityTerminalResult:
        target_selection_count = max(1, min(4, int(selection_count or 1)))
        dependency_results = [context.results.get(task_id) for task_id in dependency_task_ids]
        # An image produced earlier in the same request is inspected from its own bytes.
        vision_inputs: list[object] = [
            SimpleNamespace(data=result.payload["artifact"], content_type=str(result.payload.get("content_type") or "image/png"))
            for result in dependency_results
            if result is not None and result.capability in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"}
            and result.satisfies_request and isinstance(result.payload, dict) and isinstance(result.payload.get("artifact"), bytes)
        ][:target_selection_count]
        evidence_source = "generated_artifact"
        if not vision_inputs:
            vision_inputs = list(dict.fromkeys(str(url) for url in fallback_media_urls if url))[:target_selection_count]
            evidence_source = "authorized_image_context"
        media_urls = [item for item in vision_inputs if isinstance(item, str)]
        fallback_media_source_kind = str(fallback_media_source_kind or "none").strip() or "none"

        def media_provenance(source: str) -> dict[str, object]:
            provenance: dict[str, object] = {"source": source}
            if source == "authorized_image_context" and fallback_media_source_kind != "none":
                provenance["source_kind"] = fallback_media_source_kind
            if source == "authorized_image_context" and source_entries:
                provenance["source_entries"] = tuple(
                    {
                        "ref": item.get("ref"),
                        "source_kind": item.get("source_kind"),
                        "source_message_id": item.get("source_message_id"),
                    }
                    for item in source_entries
                )
            return provenance

        if not vision_inputs:
            return normalize_terminal_result(
                "IMAGE_ANALYZE",
                "NO_DATA_FOR_SCOPE",
                provider="xai_vision",
                provenance={**media_provenance(evidence_source), "media_selection": "none"},
                reason="authorized_media_unavailable",
            )
        vision_input_count = len(vision_inputs)
        logging.info(
            "AI image analysis vision_input_prepared guild=%s channel=%s message=%s vision_input_count=%s media_source=%s media_source_kind=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            vision_input_count,
            evidence_source,
            fallback_media_source_kind,
        )
        prompt = user_prompt
        try:
            reply = await self.bot.llm_client.chat(
                server_context=getattr(settings, "server_context", ""),
                user_message=prompt,
                author_name=message.author.display_name,
                channel_name=getattr(message.channel, "name", "unknown"),
                channel_reference=self._channel_reference(message.channel),
                available_channels=self._serialize_text_channels(message.guild),
                available_emojis=self._serialize_custom_emojis(message.guild),
                conversation_history=[],
                mention_hints=[],
                relay_instruction="",
                is_owner=self.bot.is_owner_user(message.author),
                conversation_mode="mention",
                image_inputs=vision_inputs,
            )
        except Exception as exc:
            logging.exception("AI result-only image analysis failed")
            return normalize_terminal_result(
                "IMAGE_ANALYZE",
                "PROVIDER_FAILURE",
                provider="xai_vision",
                provenance={**media_provenance(evidence_source), "media_count": len(media_urls)},
                scope={"media_count": len(media_urls)},
                reason=type(exc).__name__,
            )
        analysis_text = reply
        logging.info(
            "AI image analysis vision_execution outcome=SUCCESS input_count=%s source=%s",
            vision_input_count,
            evidence_source,
        )
        logging.info(
            "AI image analysis vision_output source=%s output_length=%s output_hash=%s output_empty=%s",
            evidence_source,
            len(analysis_text),
            hash_text(analysis_text),
            not bool(analysis_text.strip()),
        )
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(
                "AI image analysis vision_output_excerpt source=%s output_hash=%s excerpt=%s",
                evidence_source,
                hash_text(analysis_text),
                self._bounded_diagnostic_excerpt(
                    analysis_text,
                    forbidden=(
                        user_prompt,
                        prompt,
                        str(getattr(settings, "server_context", "") or ""),
                        str(getattr(message.author, "display_name", "") or ""),
                        str(getattr(message.channel, "name", "") or ""),
                    ),
                ),
            )
        context_identity = hash_text("|".join(media_urls[:4])) if media_urls else None
        return normalize_terminal_result(
            "IMAGE_ANALYZE",
            "SUCCESS",
            provider="xai_vision",
            payload={
                "text": analysis_text,
                "media_urls": tuple(media_urls[:4]),
                "rendered_media_count": sum(
                    1 for item in vision_inputs[:4] if not isinstance(item, str)
                ),
            },
            provenance={
                **media_provenance(evidence_source),
                "authorized": True,
                "media_count": vision_input_count,
                **({"source_message_id": source_message_id} if source_message_id is not None else {}),
                **({"context_identity": context_identity} if context_identity else {}),
            },
            scope={
                "media_count": vision_input_count,
                **({"source_message_id": source_message_id} if source_message_id is not None else {}),
                **({"context_identity": context_identity} if context_identity else {}),
            },
        )

    @staticmethod
    def _football_standings_diagnostic(standings: object) -> tuple[int, str, int, str]:
        rows = standings if isinstance(standings, list) else []
        sample: list[dict[str, object]] = []
        for item in rows[:4]:
            if not isinstance(item, dict):
                continue
            compact = _compact_standing(item)
            sample.append(
                {
                    field: (
                        AIChatCog._bounded_diagnostic_excerpt(value, limit=80)
                        if value is not None
                        else None
                    )
                    for field, value in compact.items()
                }
            )
        serialized = json.dumps(sample, ensure_ascii=False, separators=(",", ":"), default=str)
        return len(rows), hash_text(serialized), len(serialized), serialized


    async def _execute_live_media_composition_result_only(
        self,
        message: discord.Message,
        user_prompt: str,
        execution_plan: object,
        *,
        settings: object,
        replied_message: discord.Message | None = None,
        football_anchor_type: str = "MENTION",
        memory_lang: str = "en",
        admin_lang: str = "en",
        image_context: ChatImageContext | None = None,
        execution_context: CapabilityExecutionContext | None = None,
        can_continue=None,
    ) -> CapabilityExecutionContext:
        # Every task is one of Grok's function calls and carries its own typed operands.
        semantic_plan = getattr(execution_plan, "semantic_plan", None)
        resolution_message = message
        preflight_lang = admin_lang

        def _task_operands(request: object, context: CapabilityExecutionContext) -> dict[str, object]:
            operands = getattr(request, "operands", None)
            resolved = dict(operands) if isinstance(operands, dict) else {}

            def output_value(result: object, field: str) -> object:
                value: object = result
                if field == "result":
                    return value
                for part in str(field).split("."):
                    if isinstance(value, CapabilityTerminalResult):
                        if part == "payload":
                            value = value.payload
                        elif part == "provenance":
                            value = value.provenance
                        elif part == "scope":
                            value = value.scope
                        else:
                            payload = value.payload
                            value = payload.get(part) if isinstance(payload, dict) and part in payload else getattr(payload, part, None)
                    elif isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = getattr(value, part, None)
                return value

            for binding in tuple(getattr(request, "input_bindings", ()) or ()):
                upstream = context.results.get(getattr(binding, "upstream_task_id", ""))
                if upstream is None:
                    continue
                field = str(getattr(binding, "output_field", "") or "").strip()
                if field:
                    resolved[field.rsplit(".", 1)[-1]] = output_value(upstream, field)
            return resolved

        def _task_objective(request: object, operands: dict[str, object]) -> str | None:
            for key in ("objective", "prompt", "query", "research_question", "request"):
                value = operands.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            task_id = getattr(request, "task_id", None)
            if execution_context is not None and task_id in execution_context.evidence_requirements:
                return str(getattr(request, "evidence_span", "") or "")
            for requirement in tuple(getattr(semantic_plan, "requirements", ()) or ()):
                if getattr(requirement, "owner_task_id", None) == task_id:
                    value = str(getattr(requirement, "objective", "") or "").strip()
                    if value:
                        return value
            return None

        def _task_failure(capability: str, reason: str) -> CapabilityTerminalResult:
            return normalize_terminal_result(
                capability,
                CapabilityTerminalStatus.VALIDATION_FAILED,
                provider="orchestrator",
                provenance={"source": "typed_task_operands"},
                reason=reason,
                satisfies_request=False,
            )

        capability_requests = tuple(execution_plan.ordered_capabilities)
        prepared_live_watch_fixtures: dict[str, dict[str, Any]] = {}

        async def chat_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            objective = _task_objective(_request, task_operands)
            if not objective:
                return _task_failure("CHAT", "missing_chat_task_objective")
            bound_lines: list[str] = []
            for binding in tuple(getattr(_request, "input_bindings", ()) or ()):
                upstream = _context.results.get(getattr(binding, "upstream_task_id", ""))
                if (
                    upstream is None
                    or upstream.status != CapabilityTerminalStatus.SUCCESS
                    or not upstream.satisfies_request
                    or upstream.provenance.get("authorized") is False
                ):
                    return _task_failure("CHAT", "bound_chat_evidence_unavailable")
                field = str(getattr(binding, "output_field", "") or "").strip()
                value = task_operands.get(field.rsplit(".", 1)[-1])
                if value is None:
                    return _task_failure("CHAT", "bound_chat_evidence_missing")
                bound_lines.append(f"{field}={value}")
            trusted_context = (
                "[AUTHORIZED_BOUND_TASK_EVIDENCE]\n" + "\n".join(bound_lines)
                if bound_lines
                else None
            )
            # A CHAT task owns only its own objective; the final answer sees the whole request.
            answer = await self.bot.llm_client.chat(
                server_context=settings.server_context,
                user_message=objective,
                trusted_capability_context=trusted_context,
                author_name=message.author.display_name,
                channel_name=getattr(message.channel, "name", "unknown"),
                channel_reference=self._channel_reference(message.channel),
                available_channels=self._serialize_text_channels(message.guild),
                available_emojis=self._serialize_custom_emojis(message.guild),
                conversation_history=[],
                is_owner=self.bot.is_owner_user(message.author),
                conversation_mode="command",
                reply_language=getattr(settings, "language_code", None),
                author_permissions=self._author_permissions_text(message),
            )
            if not isinstance(answer, str) or not answer.strip():
                return _task_failure("CHAT", "empty_chat_answer")
            return normalize_terminal_result(
                "CHAT",
                "SUCCESS",
                provider="xai_chat",
                payload={"answer": answer},
                provenance={"source": "chat_handler", "authorized": True},
                scope={"scope": "GENERAL_REASONING"},
                satisfies_request=True,
            )

        async def image_handler(_request, context):
            task_operands = _task_operands(_request, context)
            request_source_refs = tuple(
                str(value).strip()
                for value in (getattr(_request, "source_refs", ()) or ())
                if str(value).strip()
            )
            continuation_media_urls = tuple(
                str(value).strip()
                for value in task_operands.get("media_urls", task_operands.get("image_urls", ())) or ()
                if str(value).strip()
            )
            continuation_source_kind = str(task_operands.get("source_kind") or "none")
            continuation_source_message_id = task_operands.get("source_message_id")
            source_entries: tuple[dict[str, object], ...] = ()
            projection = self._image_source_projection(
                image_context
                or ChatImageContext(urls=[], from_replied_message=False, reaction_target=message)
            )
            by_ref = {str(item["ref"]): item for item in projection}
            dependency_satisfied = any(
                getattr(context.results.get(dep), "satisfies_request", False)
                for dep in tuple(getattr(_request, "depends_on", ()) or ())
            )
            if not request_source_refs and not dependency_satisfied:
                return _task_failure("IMAGE_ANALYZE", "missing_image_task_source")
            if request_source_refs and any(ref not in by_ref for ref in request_source_refs):
                return _task_failure("IMAGE_ANALYZE", "invalid_image_source_ref")
            if request_source_refs:
                selection_limit = max(1, min(4, int(getattr(_request, "selection_count", 1) or 1)))
                if len(request_source_refs) > selection_limit:
                    return _task_failure("IMAGE_ANALYZE", "image_source_selection_exceeds_limit")
                selected_entries = [by_ref[ref] for ref in request_source_refs]
                continuation_media_urls = tuple(str(item["url"]) for item in selected_entries)
                selected_kinds = {str(item["source_kind"]) for item in selected_entries}
                selected_message_ids = {item["source_message_id"] for item in selected_entries}
                continuation_source_kind = next(iter(selected_kinds)) if len(selected_kinds) == 1 else "mixed"
                continuation_source_message_id = (
                    next(iter(selected_message_ids)) if len(selected_message_ids) == 1 else None
                )
                source_entries = tuple(selected_entries)
            return await self._execute_image_analysis_result_only(
                message,
                _task_objective(_request, task_operands) or "",
                context,
                settings=settings,
                fallback_media_urls=continuation_media_urls,
                fallback_media_source_kind=continuation_source_kind,
                source_message_id=continuation_source_message_id,
                source_entries=source_entries,
                selection_count=getattr(_request, "selection_count", 1),
                dependency_task_ids=tuple(getattr(_request, "depends_on", ()) or ()),
            )

        async def football_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            if not task_operands:
                return _task_failure("FOOTBALL", "missing_football_task_operands")
            task_prompt = _task_objective(_request, task_operands) or ""
            family = str(task_operands.get("operation_family") or "")
            task_action = (
                f"FOOTBALL_{family.upper()}"
                if family in FOOTBALL_MUTATING_OPERATIONS
                else str(getattr(_request, "owner_action", None) or "FOOTBALL_LOOKUP")
            )
            task_decision = RouteDecision(
                "RESPOND", task_action, 1.0, 1.0, "TYPED_TASK",
                resolved_request=task_prompt or None,
                # Grok's catalog-validated operands go to the compiler as they are.
                capability_plan={**task_operands, "semantic_source": "router"},
            )
            if task_action.upper() in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP"}:
                return await self._execute_football_live_watch_result_only(
                    message,
                    task_decision,
                    operands=task_operands,
                    prepared_fixture=prepared_live_watch_fixtures.get(getattr(_request, "task_id", "")),
                )
            return await self._execute_football_action(
                message,
                task_decision,
                user_prompt=task_prompt,
                anchor_type=football_anchor_type,
            )

        async def minecraft_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            if not task_operands.get("address"):
                return _task_failure("MINECRAFT", "missing_minecraft_address")
            return await self._execute_minecraft_status_result_only(task_operands)

        async def ninjas_handler(_request, _context):
            return await self._execute_ninjas_result_only(_task_operands(_request, _context))

        async def code_handler(_request, _context):
            return await self._execute_code_result_only(_task_operands(_request, _context))

        async def reminder_handler(_request, _context):
            return await self._execute_reminder_result_only(message, _task_operands(_request, _context))

        async def birthday_handler(_request, _context):
            return await self._execute_birthday_result_only(message, _task_operands(_request, _context))

        async def translate_handler(_request, _context):
            return await self._execute_translate_result_only(
                message, replied_message, _task_operands(_request, _context),
            )

        async def memory_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            if is_memory_lifecycle(task_operands):
                return await self._execute_memory_lifecycle_result_only(message, task_operands)
            if not task_operands:
                return _task_failure("MEMORY", "missing_memory_task_operands")
            # The task's own memory_action is the operation that preflight validated.
            task_action = str(task_operands.get("memory_action") or getattr(_request, "owner_action", None) or "").upper()
            task_decision = RouteDecision(
                "RESPOND", task_action, 1.0, 1.0, "SERVER_MEMORY_REQUEST", memory=dict(task_operands),
            )
            if task_decision.action not in {
                "SERVER_MEMORY_LOOKUP",
                "SERVER_MEMORY_WRITE",
                "SERVER_MEMORY_UPDATE",
                "SERVER_MEMORY_DELETE",
            }:
                return normalize_terminal_result(
                    "MEMORY",
                    "VALIDATION_FAILED",
                    provider="orchestrator",
                    provenance={"source": "typed_capability_requirement"},
                    reason="missing_memory_decision",
                )
            result = await self._execute_server_memory_action(
                message,
                task_decision,
                lang=memory_lang,
            )
            if isinstance(result, CapabilityTerminalResult):
                return result
            return normalize_terminal_result(
                "MEMORY",
                result,
                provider="server_memory",
                provenance={"source": "ServerMemoryService"},
            )

        async def admin_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            if not task_operands:
                return _task_failure("ADMIN", "missing_admin_task_operands")
            if not task_operands.get("admin_action"):
                return normalize_terminal_result(
                    "ADMIN",
                    "VALIDATION_FAILED",
                    provider="orchestrator",
                    provenance={"source": "typed_capability_requirement"},
                    reason="missing_admin_stats_decision",
                )
            task_decision = RouteDecision(
                "RESPOND", "ADMIN_ACTION", 0.0, 0.0, "ADMIN_ACTION_REQUEST", admin=dict(task_operands),
            )
            return await self._execute_admin_action(
                message,
                task_decision,
                lang=admin_lang,
                resolution_message=resolution_message,
            )

        async def image_generate_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            prompt = _task_objective(_request, task_operands)
            if not prompt:
                return _task_failure("IMAGE_GENERATE", "missing_image_generation_objective")
            return await self._execute_image_generation_result_only(prompt)

        async def image_edit_handler(_request, context):
            task_operands = _task_operands(_request, context)
            bound_artifact = task_operands.get("artifact")
            edit_source_url = None
            edit_source_kind = "none"
            edit_source_message_id = None
            if isinstance(bound_artifact, bytes):
                edit_source_url = "data:image/png;base64," + base64.b64encode(bound_artifact).decode("ascii")
                edit_source_kind = "bound_artifact"
            if getattr(_request, "input_bindings", ()) and not edit_source_url:
                return _task_failure("IMAGE_EDIT", "bound_image_source_unavailable")
            prompt = _task_objective(_request, task_operands)
            if not prompt:
                return _task_failure("IMAGE_EDIT", "missing_image_edit_objective")
            if not edit_source_url:
                selected_source, reason = self._selected_image_source(
                    _request, task_operands, image_context, message, "image_edit",
                )
                if reason is not None:
                    return _task_failure("IMAGE_EDIT", reason)
                edit_source_url = str(selected_source["url"])
                edit_source_kind = str(selected_source["source_kind"])
                edit_source_message_id = selected_source.get("source_message_id")
            return await self._execute_image_edit_result_only(
                prompt,
                edit_source_url,
                source_kind=edit_source_kind,
                source_message_id=edit_source_message_id,
            )

        async def meme_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            source = None
            if task_operands.get("meme_action") == "custom":
                source, reason = self._selected_image_source(
                    _request, task_operands, image_context, message, "meme",
                )
                if reason is not None:
                    return _task_failure("MEME", reason)
            return await self._execute_meme_result_only(task_operands, source)

        async def reaction_handler(_request, _context):
            task_operands = _task_operands(_request, _context)
            if not task_operands:
                return _task_failure("REACTION", "missing_reaction_task_operands")
            emoji = task_operands.get("emoji")
            emojis = task_operands.get("emojis")
            task_decision = RouteDecision(
                "RESPOND",
                "ADD_REACTION",
                1.0,
                1.0,
                "REACTION_ACK",
                emoji=str(emoji) if emoji else None,
                emojis=tuple(str(value) for value in emojis if str(value).strip()) if isinstance(emojis, (list, tuple)) else (),
            )
            if not (task_decision.emoji or task_decision.emojis):
                return normalize_terminal_result(
                    "REACTION",
                    "VALIDATION_FAILED",
                    provider="discord",
                    provenance={"source": "reaction_handler", "action": "ADD_REACTION"},
                    reason="missing_emoji",
                )
            decision = replace(task_decision, action="ADD_REACTION")
            reaction_ok = await self._execute_reaction_action(
                message,
                decision,
                image_context=image_context
                or ChatImageContext(urls=[], from_replied_message=False, reaction_target=message),
                replied_message=replied_message,
                pending_interaction=None,
            )
            return normalize_terminal_result(
                "REACTION",
                "SUCCESS" if reaction_ok else "PROVIDER_FAILURE",
                provider="discord",
                provenance={"source": "reaction_handler", "action": "ADD_REACTION"},
                scope={"action": "ADD_REACTION"},
                reason=None if reaction_ok else "reaction_execution_failed",
            )

        async def mutation_preflight(tasks, _context):
            """Validate every declared mutation before the first mutation runs."""

            for request in tasks:
                task_name = str(getattr(request, "name", "") or "").upper()
                task_operands = _task_operands(request, _context)
                if not task_operands:
                    return f"{task_name.lower()} was called without arguments"
                if task_name == "REACTION":
                    emoji = task_operands.get("emoji")
                    emojis = task_operands.get("emojis")
                    if not (emoji or (isinstance(emojis, (list, tuple)) and any(str(value).strip() for value in emojis))):
                        return False
                    target = (image_context.reaction_target if image_context is not None else None) or message
                    permissions_for = getattr(getattr(target, "channel", None), "permissions_for", None)
                    bot_member = self._admin_actions._bot_member(message.guild) if message.guild is not None else None
                    try:
                        reaction_permission = permissions_for(bot_member) if bot_member is not None and permissions_for is not None else None
                    except Exception:
                        logging.exception("Failed to read reaction permissions during mutation preflight")
                        reaction_permission = None
                    if bot_member is None or reaction_permission is None or not bool(getattr(reaction_permission, "add_reactions", False)):
                        return False
                    continue
                if task_name == "MEMORY":
                    if is_memory_lifecycle(task_operands):
                        if error := self._memory_lifecycle_operand_error(message, task_operands):
                            return error
                        target = await self._server_memory.get_memory(message.guild.id, task_operands["memory_id"])
                        if self._memory_decider_error(message, target) or not self._memory_is_actionable(
                            target, task_operands["memory_action"],
                        ):
                            return False
                        continue
                    task_action = str(getattr(request, "owner_action", None) or task_operands.get("memory_action") or task_operands.get("operation") or "").upper()
                    if task_action not in {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE", "SERVER_MEMORY_DELETE"}:
                        return False
                    if self._server_memory is None or not task_operands.get("memory_type"):
                        return "missing required argument: memory_type"
                    if task_action in {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE"} and not task_operands.get("value"):
                        return "missing required argument: value"
                    try:
                        memory_type = ServerMemoryService.normalize_memory_type(
                            str(task_operands.get("memory_type") or "")
                        )
                    except ValueError:
                        return False
                    if memory_type.startswith("USER_") and self._memory_subject_user_id(
                        resolution_message,
                        task_operands,
                    ) is None:
                        return False
                    if memory_type == "BOT_BEHAVIOR_RULE" and not self._route_authority_metadata(message)["author_can_manage_bot_behavior"]:
                        return False
                    continue
                if task_name == "REMINDER":
                    if error := self._reminder_operand_error(message, task_operands):
                        return error
                    if task_operands["reminder_action"] == "remove" and not any(
                        row["reminder_id"] == task_operands["reminder_id"]
                        for row in await list_reminders(self.bot.db, message.guild.id, message.author.id, limit=-1)
                    ):
                        return "reminder_id is not one of the author's reminders"
                    continue
                if task_name == "BIRTHDAY":
                    if task_operands.get("birthday_action") in BIRTHDAY_SETTINGS_OPERANDS:
                        if self._birthday_operand_error(message, task_operands) or (
                            task_operands["birthday_action"] == "preview"
                            and await self._birthday_preview_target(message) is None
                        ) or (
                            task_operands["birthday_action"] == "blacklist_user"
                            and await self._birthday_blacklist_member(message, task_operands) is None
                        ):
                            return False
                        # Read-only lookups: the template limit and the template id must hold now; execution re-validates.
                        if task_operands["birthday_action"] == "template_add" and await self.bot.db.count_birthday_templates(
                            message.guild.id, task_operands["birthday_template_type"]
                        ) >= BIRTHDAY_TEMPLATE_LIMIT:
                            return False
                        if task_operands["birthday_action"] == "template_remove" and not any(
                            row["id"] == task_operands["birthday_template_id"]
                            for row in await list_birthday_templates(
                                self.bot.db, message.guild.id, task_operands["birthday_template_type"]
                            )
                        ):
                            return False
                        continue
                    if error := self._birthday_operand_error(message, task_operands):
                        return error
                    if not await birthday_module_enabled(self.bot.db, message.guild.id):
                        return "birthdays are disabled in this server"
                    if task_operands["birthday_action"] == "remove" and await self.bot.db.get_birthday_profile(
                        message.guild.id, message.author.id
                    ) is None:
                        return False
                    continue
                if task_name == "FOOTBALL":
                    task_action = str(getattr(request, "owner_action", None) or task_operands.get("operation") or task_operands.get("operation_family") or "").upper()
                    if task_action not in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP", "LIVE_WATCH_START", "LIVE_WATCH_STOP"}:
                        return False
                    if task_action.endswith("START"):
                        client = getattr(self.bot, "api_football_client", None)
                        if client is None or not any(
                            task_operands.get(key)
                            for key in ("fixture_id", "fixture_focus", "team_candidates", "league_candidates")
                        ):
                            return False
                        request_text = _task_objective(request, task_operands) or ""
                        football_plan = {**task_operands, "semantic_source": "router"}
                        operation = build_operation(
                            "FOOTBALL_LIVE_WATCH_START",
                            request_text,
                            football_plan,
                        )
                        fixture = await self._resolve_football_live_watch_fixture(
                            client,
                            request=request_text,
                            plan=football_plan,
                            operation=operation,
                        )
                        fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
                        if not isinstance(fixture_obj, dict) or not isinstance(fixture_obj.get("id"), int):
                            return False
                        prepared_live_watch_fixtures[getattr(request, "task_id", "")] = fixture
                    continue
                if task_name != "ADMIN":
                    return False
                guild = message.guild
                if guild is None:
                    return False
                if not task_operands.get("admin_action"):
                    return False
                task_decision = RouteDecision(
                    "RESPOND", "ADMIN_ACTION", 0.0, 0.0, "ADMIN_ACTION_REQUEST", admin=dict(task_operands),
                )
                # A dependent channel target is not available until its create
                # task runs; validate the binding below, not a name fallback.
                if isinstance(task_operands, dict) and any(
                    str(getattr(binding, "output_field", "")).strip() == "resource_handle"
                    for binding in tuple(getattr(request, "input_bindings", ()) or ())
                ):
                    task_decision = replace(
                        task_decision,
                        admin={**dict(task_decision.admin or {}), "resource_handle": {"resource_type": "channel", "guild_id": getattr(guild, "id", None), "resource_id": 1}},
                    )
                plan = self._coerce_admin_plan(task_decision.admin)
                if not plan.get("valid"):
                    return plan.get("clarification_question") or False
                action = str(plan.get("admin_action") or "").casefold()
                if action in {"join_stats", "leave_stats"}:
                    authority = self._route_authority_metadata(message)
                    if not (
                        authority["author_is_bot_owner"]
                        or authority["author_is_guild_owner"]
                        or authority["author_has_administrator"]
                        or authority["author_has_manage_guild"]
                    ) or not plan.get("time_window_seconds"):
                        return False
                    continue
                actor = message.author
                bot_member = self._admin_actions._bot_member(guild)
                if bot_member is None:
                    return False
                if action == "delete_messages":
                    if not (1 <= int(plan.get("message_count") or 0) <= 500):
                        return False
                    if not self._admin_actions._actor_has(actor, "manage_messages"):
                        return False
                    if not self._admin_actions._has_channel_perm(bot_member, message.channel, "manage_messages"):
                        return False
                    continue
                if action in {"create_role", "create_channel"}:
                    permission = "manage_roles" if action == "create_role" else "manage_channels"
                    if not self._admin_actions._actor_has(actor, permission):
                        return False
                    if not self._admin_actions._has_perm(bot_member, permission):
                        return False
                    if action == "create_role" and not plan.get("role_name"):
                        return False
                    if action == "create_channel" and not plan.get("channel_name"):
                        return False
                    continue
                if action in {"delete_role", "add_role", "remove_role"}:
                    role_binding = next(
                        (
                            binding
                            for binding in tuple(getattr(request, "input_bindings", ()) or ())
                            if str(getattr(binding, "output_field", "")).strip() == "resource_handle"
                        ),
                        None,
                    )
                    if role_binding is not None and action in {"add_role", "remove_role"}:
                        upstream_task_id = getattr(role_binding, "upstream_task_id", None)
                        if upstream_task_id not in tuple(getattr(request, "depends_on", ()) or ()):
                            return False
                        upstream = next(
                            (
                                item
                                for item in execution_plan.ordered_capabilities
                                if item.task_id == upstream_task_id
                            ),
                            None,
                        )
                        upstream_operands = getattr(upstream, "operands", {}) if upstream is not None else {}
                        if (
                            upstream is None
                            or "resource_handle" not in tuple(getattr(upstream, "requested_outputs", ()) or ())
                            or getattr(upstream, "name", "") != "ADMIN"
                            or str(upstream_operands.get("admin_action") or "").casefold() != "create_role"
                        ):
                            return False
                        role = None
                    else:
                        # A refusal returns its message, so Grok can fix the arguments or tell the user why.
                        role = self._resolve_admin_target_role(resolution_message, plan, preflight_lang)
                        if isinstance(role, str):
                            return role
                        if refusal := self._admin_actions._can_manage_role(guild, actor, role, preflight_lang):
                            return refusal.message
                    if action in {"add_role", "remove_role"}:
                        # The member must exist; the role's position is the only limit, so the author may be the target.
                        target = await self._resolve_admin_target_member(resolution_message, plan, preflight_lang)
                        if isinstance(target, str):
                            return target
                    continue
                if action in {"kick", "ban", "tempban", "mute", "tempmute", "unmute", "warn", "list_warnings", "remove_warning", "clear_warnings", "set_nickname"}:
                    target = await self._resolve_admin_target_member(resolution_message, plan, preflight_lang)
                    # Same rule as the service: only a nickname change may target the author.
                    self_ok = {"allow_self": True} if action == "set_nickname" else {}
                    if isinstance(target, str):
                        return target
                    if refusal := self._admin_actions._can_act_on_target(guild, actor, target, preflight_lang, **self_ok):
                        return refusal.message
                    permission = "manage_nicknames" if action == "set_nickname" else "moderate_members" if action in {"mute", "tempmute", "unmute", "warn", "list_warnings", "remove_warning", "clear_warnings"} else ("kick_members" if action == "kick" else "ban_members")
                    if not self._admin_actions._actor_has(actor, permission) or not self._admin_actions._has_perm(bot_member, permission):
                        return False
                    if action == "tempban" and not plan.get("duration_seconds"):
                        return False
                    continue
                if action == "unban":
                    if not plan.get("target_user_id") or not self._admin_actions._actor_has(actor, "ban_members") or not self._admin_actions._has_perm(bot_member, "ban_members"):
                        return False
                    continue
                if action == "purge_user_messages":
                    # The current channel only; the plan already refused any channel operand, a missing count and a missing member.
                    target = await self._resolve_admin_target_member(resolution_message, plan, preflight_lang)
                    if (
                        isinstance(target, str)
                        or not 1 <= int(plan.get("message_count") or 0) <= 500
                        or not callable(getattr(message.channel, "history", None))
                        or not self._admin_actions._actor_has(actor, "manage_messages")
                        or not self._admin_actions._has_channel_perm(bot_member, message.channel, "manage_messages")
                    ):
                        return False
                    continue
                if action == "say":
                    # The current channel only; the text and the missing channel operand are validated by the plan.
                    if not self._admin_actions._actor_has(actor, "manage_messages") or not self._admin_actions._has_channel_perm(bot_member, message.channel, "send_messages"):
                        return False
                    continue
                if action in {"lock_channel", "unlock_channel", "delete_channel", "set_slowmode", "clear_channel", "clone_channel", "color_publish"} | ADMIN_CONFIG_CHANNEL_OPERATIONS:
                    # Clearing needs the bot to manage messages in the channel; every other channel action manages the channel.
                    # Configuration only writes the database: it needs Manage Server from the actor and nothing from the bot.
                    # The color panel needs Administrator and the bot able to post it (in the channel, or guild-wide for one created in this batch).
                    config_channel = action in ADMIN_CONFIG_CHANNEL_OPERATIONS
                    color_panel = action == "color_publish"
                    actor_permission = "administrator" if color_panel else "manage_guild" if config_channel else "manage_channels"
                    bot_permission = "manage_messages" if action == "clear_channel" else "manage_channels"
                    channel_binding = next(
                        (
                            binding
                            for binding in tuple(getattr(request, "input_bindings", ()) or ())
                            if str(getattr(binding, "output_field", "")).strip() == "resource_handle"
                        ),
                        None,
                    )
                    if channel_binding is not None:
                        upstream_task_id = getattr(channel_binding, "upstream_task_id", None)
                        upstream = next(
                            (item for item in execution_plan.ordered_capabilities if item.task_id == upstream_task_id),
                            None,
                        )
                        upstream_operands = getattr(upstream, "operands", {}) if upstream is not None else {}
                        if (
                            upstream_task_id not in tuple(getattr(request, "depends_on", ()) or ())
                            or upstream is None
                            or "resource_handle" not in tuple(getattr(upstream, "requested_outputs", ()) or ())
                            or getattr(upstream, "name", "") != "ADMIN"
                            or str(upstream_operands.get("admin_action") or "").casefold() not in {"create_channel", "clone_channel"}
                        ):
                            return False
                        if action == "delete_channel" and upstream_task_id == getattr(request, "task_id", None):
                            return False
                        if (
                            not self._admin_actions._actor_has(actor, actor_permission)
                            or (not config_channel and not color_panel and not self._admin_actions._has_perm(bot_member, bot_permission))
                            or (color_panel and (self.bot.get_cog("ModerationCog") is None or self._admin_actions.color_panel_bot_denied(guild, None, preflight_lang) is not None))
                            or (action == "server_context_learn" and self.bot.get_cog("AdminCog") is None)
                        ):
                            return False
                        continue
                    channel = self._resolve_admin_target_channel(resolution_message, plan, preflight_lang)
                    if isinstance(channel, str):
                        return channel
                    if action == "delete_channel" and getattr(channel, "id", None) == getattr(message.channel, "id", None):
                        return False
                    if (
                        not self._admin_actions._actor_has(actor, actor_permission)
                        or (not config_channel and not color_panel and not self._admin_actions._has_channel_perm(bot_member, channel, bot_permission))
                        or (color_panel and (self.bot.get_cog("ModerationCog") is None or self._admin_actions.color_panel_bot_denied(guild, channel, preflight_lang) is not None))
                        or (
                            action == "server_context_learn"
                            and (
                                self._admin_actions.server_context_learn_denied(actor, channel, lang=preflight_lang) is not None
                                # Executability: the bot must be able to read the channel and the AdminCog (the summarizer) must be loaded.
                                or not all(self._admin_actions._has_channel_perm(bot_member, channel, name) for name in ("view_channel", "read_message_history"))
                                or self.bot.get_cog("AdminCog") is None
                            )
                        )
                    ):
                        return False
                    continue
                if action in ADMIN_CONFIG_OPERATIONS:
                    # The channel configuration actions continued above, so this is the channel-less rest (Manage Server only);
                    # the plan already validated its operands and refused a channel.
                    if not self._admin_actions._actor_has(actor, "manage_guild"):
                        return False
                    continue
                if action in ADMIN_COLOR_OPERATIONS:
                    # color_publish continued above. Administrator only, like the /color commands; ModerationCog does the work, so it must be
                    # loaded. What depends on database state an earlier task may change (existing colors, the stored panel) is decided at execution.
                    if not self._admin_actions._actor_has(actor, "administrator") or self.bot.get_cog("ModerationCog") is None:
                        return False
                    if action in {"color_setup", "color_add", "color_remove"} and not self._admin_actions._has_perm(bot_member, "manage_roles"):
                        return False
                    if action == "color_remove":
                        entry = await self.bot.db.get_color_role_by_name(guild.id, plan["role_name"])
                        role = guild.get_role(int(entry["role_id"])) if entry is not None else None
                        if role is not None and self._admin_actions._can_manage_role(guild, actor, role, preflight_lang) is not None:
                            return False
                    continue
                return False
            return True

        handlers = {
            "CHAT": chat_handler,
            "IMAGE_ANALYZE": image_handler,
            "FOOTBALL": football_handler,
            "MINECRAFT": minecraft_handler,
            "NINJAS": ninjas_handler,
            "CODE": code_handler,
            "REMINDER": reminder_handler,
            "BIRTHDAY": birthday_handler,
            "TRANSLATE": translate_handler,
            "MEME": meme_handler,
            "IMAGE_GENERATE": image_generate_handler,
            "IMAGE_EDIT": image_edit_handler,
            "MEMORY": memory_handler,
            "ADMIN": admin_handler,
            "REACTION": reaction_handler,
        }
        specialized_execution_plan = execution_plan
        requested_capabilities = {
            getattr(item, "name", "")
            for item in getattr(specialized_execution_plan, "ordered_capabilities", ()) or ()
        }
        if not requested_capabilities.issubset(set(handlers)):
            raise RuntimeError("stage4_result_only_handler_registry_mismatch")
        execution_context = execution_context or CapabilityExecutionContext(execution_plan)
        async def guarded_read(request, context):
            if request.task_id in context.evidence_requirements:
                operands = _task_operands(request, context)
                typed = {key: value for key, value in operands.items() if key in operand_descriptors(request.name)}
                if validate_operands(request.name, typed) or operation_effect(replace(request, operands=typed)) != "read":
                    return _task_failure(request.name, "invalid_bound_investigation_operation")
                key = evidence_read_key(replace(request, operands=operands))
                for prior in context.execution_plan.ordered_capabilities:
                    if prior.task_id in context.results and evidence_read_key(replace(prior, operands=_task_operands(prior, context))) == key:
                        return _task_failure(request.name, "duplicate_investigation_read")
            result = await handlers[request.name](request, context)
            if request.task_id in context.evidence_requirements and isinstance(result.payload, FootballRetrievalResult):
                scope = {key: value for key, value in result.scope.items() if key != "request_plan"}
                for prior in context.results.values():
                    if (scope and prior.status == result.status == CapabilityTerminalStatus.SUCCESS
                            and isinstance(prior.payload, FootballRetrievalResult)
                            and scope == {key: value for key, value in prior.scope.items() if key != "request_plan"}
                            and result.payload.resolved_entities == prior.payload.resolved_entities):
                        return replace(result, status=CapabilityTerminalStatus.NOT_EXECUTED, payload=None,
                                       satisfies_request=False, reason=f"duplicate_resolved_read:{prior.task_id}")
            return result

        return await execute_ordered_capabilities(
            specialized_execution_plan,
            {name: guarded_read for name in handlers} if can_continue is not None else handlers,
            context=execution_context,
            preflight=mutation_preflight,
            can_continue=can_continue,
        )

    async def _execute_football_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        user_prompt: str,
        anchor_type: str,
    ) -> CapabilityTerminalResult:
        client = getattr(self.bot, "api_football_client", None)
        if client is None:
            return normalize_terminal_result(
                "FOOTBALL",
                "NOT_CONFIGURED",
                provider="api-football",
                provenance={"source": "client_configuration"},
                reason="api_football_not_configured",
            )
        request = self._football_request_with_lease_context(message, user_prompt, anchor_type=anchor_type)
        plan = decision.capability_plan
        action = "FOOTBALL_LOOKUP"
        operation_request = request if plan is not None else ""
        lease_key = self._message_channel_key(message)
        stored_lease = self._continuation_leases.get(lease_key)
        active_lease = self._valid_lease_for_message(message)
        lease_owner_matches_author = bool(
            active_lease is not None
            and getattr(message.author, "id", None) == active_lease.owner_user_id
        )
        active_football_turn_context = self._valid_football_turn_context(message)
        prior_context = self._football_prior_lease_context(message)
        try:
            operation = compile_football_operation(
                action, operation_request, plan, prior_context=prior_context,
            )
        except InvalidFootballApiRequest as exc:
            return normalize_terminal_result(
                "FOOTBALL", "VALIDATION_FAILED", provider="football_compiler", reason=str(exc),
                payload=FootballRetrievalResult(outcome=FootballOutcome.RESOLUTION_FAILED, notes=[str(exc)],
                    missing_inputs=["typed_operands"], requested_scope={
                        "operation_type": (plan or {}).get("operation_family"),
                        "supplied_team_candidates": (plan or {}).get("team_candidates", []),
                    }),
            )
        logging.info(
            "AI football continuation branch input message=%s continuation_lease_present=%s "
            "continuation_lease_active=%s lease_owner_matches_author=%s football_turn_context_active=%s "
            "prior_context_present=%s league_candidates=%s team_candidates=%s league_slot_authorities=%s",
            getattr(message, "id", None),
            stored_lease is not None,
            active_lease is not None,
            lease_owner_matches_author,
            active_football_turn_context is not None,
            prior_context is not None,
            list(operation.league_candidates)[:4],
            list(operation.team_candidates)[:4],
            [getattr(slot, "authority", None) for slot in operation.league_slots],
        )
        action = operation.route_action
        try:
            data_focus = operation.data_focus
            logging.info(
                "AI football plan action=%s operation=%s data_focus=%s league_candidates=%s team_candidates=%s player_candidates=%s capability=%s",
                action,
                operation.operation_type,
                data_focus,
                list(operation.league_candidates)[:4],
                list(operation.team_candidates)[:4],
                list(operation.player_candidates)[:4],
                getattr(operation.capability_intent, "operation_family", None),
            )

            # No nested Grok call picks a name here: on AMBIGUOUS or NOT_FOUND Grok sees the candidates and retries.
            football_service = FootballOperationService(client, player_alias_cache=self._football_player_alias_cache())
            result = await football_service.execute(
                operation,
                league_id=None,
                season=None,
                data_focus=data_focus,
            )
            row_count, sample_digest, sample_length, compact_sample = self._football_standings_diagnostic(
                result.standings_table
            )
            logging.info(
                "AI football retrieval standings_projection operation=%s outcome=%s row_count=%s sample_digest=%s sample_length=%s sample=%s",
                operation.operation_type,
                result.outcome,
                row_count,
                sample_digest,
                sample_length,
                compact_sample,
            )
            football_terminal_result = normalize_terminal_result(
                "FOOTBALL",
                result.outcome,
                provider="api-football",
                payload=result,
                provenance={"source": "FootballRetrievalResult", "operation": operation.operation_type},
                scope=result.requested_scope,
                reason=";".join(result.notes[:2]) if result.outcome != FootballOutcome.SELECTED and result.notes else None,
            )
            logging.info(
                "AI football retrieval action=%s operation=%s outcome=%s endpoints=%s notes=%s",
                action,
                operation.operation_type,
                result.outcome,
                result.endpoints,
                result.notes[:5],
            )
            return football_terminal_result
        except Exception:
            logging.exception("AI football action failed")
            return normalize_terminal_result(
                "FOOTBALL",
                "PROVIDER_FAILURE",
                provider="api-football",
                provenance={"source": "football_handler"},
                reason="football_handler_exception",
            )

    def _football_prior_lease_context(self, message: discord.Message) -> str | None:
        turn_context = self._valid_football_turn_context(message)
        if turn_context is not None:
            return turn_context.to_prior_context()
        watch = self._watch_for_replied_message(message)
        if watch is not None:
            return f"Watched fixture: {watch.fixture_label}. Original watch request: {watch.request}"[:450]
        lease = self._valid_lease_for_message(message)
        if (
            lease is None
            or getattr(message.author, "id", None) != lease.owner_user_id
            or not str(lease.last_action).startswith("FOOTBALL_")
        ):
            return None
        prior = " ".join(str(lease.resolved_request or "").split())
        return json.dumps({"prior_request": prior[:400]}, ensure_ascii=False, separators=(",", ":")) if prior else None

    @staticmethod
    def _football_plan_text(plan: dict[str, Any] | None, key: str) -> str | None:
        if not isinstance(plan, dict):
            return None
        cleaned = " ".join(str(plan.get(key) or "").split())[:120]
        return cleaned or None

    def _football_request_with_lease_context(self, message: discord.Message, request: str, *, anchor_type: str) -> str:
        cleaned = " ".join(str(request or "").split())
        return cleaned

    @staticmethod
    def _web_request_date(message: discord.Message) -> str:
        created_at = getattr(message, "created_at", None)
        if isinstance(created_at, datetime):
            return created_at.date().isoformat()
        return date.today().isoformat()

    def _football_request_temporal_context(self, message: discord.Message) -> tuple[str, str | None]:
        client = getattr(self.bot, "api_football_client", None)
        today_iso = getattr(client, "today_iso", None)
        request_date = today_iso() if callable(today_iso) else self._web_request_date(message)
        if not isinstance(request_date, str) or not request_date.strip():
            request_date = self._web_request_date(message)
        timezone_name = getattr(client, "timezone_name", None)
        return request_date[:32], str(timezone_name)[:80] if timezone_name else None

    async def _execute_football_live_watch_result_only(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        operands: dict[str, object],
        prepared_fixture: dict[str, Any] | None = None,
    ) -> CapabilityTerminalResult:
        if message.guild is None:
            return normalize_terminal_result("FOOTBALL", "VALIDATION_FAILED", provider="orchestrator", reason="missing_guild")
        key = (int(message.guild.id), int(message.channel.id))
        action = str(decision.action or "").upper()
        if action == "FOOTBALL_LIVE_WATCH_STOP":
            stopped = self._cancel_football_live_watch(key, reason="typed_task_stop")
            return normalize_terminal_result(
                "FOOTBALL",
                "SUCCESS" if stopped else "NO_DATA",
                provider="football_watch",
                payload={"action": action, "stopped": stopped},
                scope={"guild_id": message.guild.id, "channel_id": message.channel.id},
                satisfies_request=stopped,
            )
        client = getattr(self.bot, "api_football_client", None)
        if client is None:
            return normalize_terminal_result("FOOTBALL", "NOT_CONFIGURED", provider="api-football", reason="api_football_not_configured")
        request = " ".join(str(decision.resolved_request or "").split())[:900]
        football = {**operands, "semantic_source": "router"}
        operation = build_operation("FOOTBALL_LIVE_WATCH_START", request, football)
        fixture = prepared_fixture or await self._resolve_football_live_watch_fixture(
            client,
            request=request,
            plan=football,
            operation=operation,
        )
        fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
        fixture_id = fixture_obj.get("id") if isinstance(fixture_obj, dict) else None
        if not isinstance(fixture_id, int):
            return normalize_terminal_result("FOOTBALL", "NO_DATA", provider="api-football", reason="live_watch_fixture_unresolved")
        self._cancel_football_live_watch(key, reason="typed_task_replace")
        watch = self._create_football_live_watch(message, fixture=fixture, request=request)
        watch.task = asyncio.create_task(self._run_football_live_watch(key))
        self._football_live_watches[key] = watch
        return normalize_terminal_result(
            "FOOTBALL",
            "SUCCESS",
            provider="football_watch",
            payload={"action": action, "fixture_id": watch.fixture_id, "request": request},
            scope={"guild_id": watch.guild_id, "channel_id": watch.channel_id, "fixture_id": watch.fixture_id},
            satisfies_request=True,
        )


    async def _resolve_football_live_watch_fixture(
        self,
        client: object,
        *,
        request: str,
        plan: dict[str, Any] | None,
        operation: FootballQueryOperation | None = None,
    ) -> dict[str, Any] | None:
        operation = operation or build_operation("FOOTBALL_LIVE_WATCH_START", request, plan)
        result = await FootballOperationService(client).execute(
            operation,
            league_id=None,
            season=None,
            data_focus="live_watch_start",
        )
        fixtures = result.fixtures
        if len(fixtures) == 1:
            return fixtures[0]
        if not fixtures:
            return None
        focus = self._football_plan_text(plan, "fixture_focus") or request
        picked = self._pick_football_fixture(fixtures, focus)
        if picked is not None:
            return picked
        return fixtures[0] if operation.team_slots and len(fixtures) <= 2 else None

    def _create_football_live_watch(
        self,
        message: discord.Message,
        *,
        fixture: dict[str, Any],
        request: str,
    ) -> FootballLiveWatch:
        home, away = fixture_teams(fixture)
        status = fixture_status(fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {})
        score = fixture_score(fixture)
        fixture_obj = fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {}
        now = time.monotonic()
        return FootballLiveWatch(
            guild_id=int(message.guild.id),
            channel_id=int(message.channel.id),
            channel=message.channel,
            owner_user_id=int(message.author.id),
            fixture_id=int(fixture_obj.get("id")),
            fixture_label=f"{home} vs {away}",
            request=request,
            started_at=now,
            expires_at=now + self._football_live_watch_max_seconds,
            last_score=score,
            last_status=status,
            seen_event_keys=set(),
            last_snapshot=snapshot_from_fixture(fixture),
        )

    async def _run_football_live_watch(self, key: tuple[int, int]) -> None:
        try:
            while True:
                watch = self._football_live_watches.get(key)
                if watch is None or watch.canceled:
                    return
                if time.monotonic() >= watch.expires_at:
                    self._cancel_football_live_watch(key, reason="timeout")
                    return
                await asyncio.sleep(self._football_live_watch_poll_seconds)
                await self._poll_football_live_watch(key)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("AI football live watch loop failed key=%s", key)
            self._cancel_football_live_watch(key, reason="error")

    async def _poll_football_live_watch(self, key: tuple[int, int]) -> None:
        watch = self._football_live_watches.get(key)
        client = getattr(self.bot, "api_football_client", None)
        if watch is None or watch.canceled or client is None:
            return
        try:
            base_detail = await FootballLiveMatchService(client).get_match_center(
                watch.fixture_id,
                time_scope="live_watch",
                include_events=True,
            )
            fixture = base_detail.fixture
            events = base_detail.events
            statistics: list[dict[str, Any]] = []
            lineups: list[dict[str, Any]] = []
            if fixture is not None:
                current_snapshot = snapshot_from_fixture(fixture)
                include_lineups = should_fetch_lineups(current_snapshot, lineups_fetched=watch.lineups_fetched)
                include_stats = should_fetch_statistics(current_snapshot, emitted_checkpoints=watch.emitted_checkpoints)
                if include_lineups or include_stats:
                    detail = await FootballLiveMatchService(client).get_match_center(
                        watch.fixture_id,
                        time_scope="live_watch",
                        include_events=False,
                        include_stats=include_stats,
                        include_lineups=include_lineups,
                    )
                    statistics = detail.statistics
                    lineups = detail.lineups
                    if include_lineups:
                        watch.lineups_fetched = True
        except Exception:
            logging.exception("AI football live watch poll failed guild=%s channel=%s fixture=%s", watch.guild_id, watch.channel_id, watch.fixture_id)
            return
        if fixture is None:
            return
        current_snapshot = snapshot_from_fixture(fixture, statistics=statistics)
        updates, new_snapshot = build_watch_updates(
            previous=watch.last_snapshot,
            current=current_snapshot,
            fixture=fixture,
            events=events,
            seen_event_keys=watch.seen_event_keys,
            emitted_checkpoints=watch.emitted_checkpoints,
            statistics=statistics,
            lineups=lineups,
        )
        update_text = "\n".join(update.text for update in updates if update.needs_send)[:1900]
        if update_text:
            if not self._football_live_watch_can_send(key, watch):
                return
            sent = await watch.channel.send(update_text)
            sent_id = getattr(sent, "id", None)
            if isinstance(sent_id, int):
                watch.watch_message_ids.append(sent_id)
            for update in updates:
                if update.event_key:
                    watch.seen_event_keys.add(update.event_key)
                if update.checkpoint:
                    watch.emitted_checkpoints.add(update.checkpoint)
        watch.last_snapshot = new_snapshot
        watch.last_score = new_snapshot.score
        watch.last_status = new_snapshot.status
        if is_terminal_status(new_snapshot.status):
            self._cancel_football_live_watch(key, reason="terminal")

    def _football_live_watch_can_send(self, key: tuple[int, int], watch: FootballLiveWatch) -> bool:
        current = self._football_live_watches.get(key)
        return current is watch and not watch.canceled

    def _cancel_football_live_watch(self, key: tuple[int, int], *, reason: str) -> bool:
        watch = self._football_live_watches.pop(key, None)
        if watch is None:
            return False
        watch.canceled = True
        if watch.task is not None:
            watch.task.cancel()
        logging.info(
            "AI football live watch stopped guild=%s channel=%s fixture=%s reason=%s",
            watch.guild_id,
            watch.channel_id,
            watch.fixture_id,
            reason,
        )
        return True

    @staticmethod
    def _pick_football_fixture(fixtures: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
        normalized = football_resolver.normalize_key(text)
        if not normalized:
            return None
        for fixture in fixtures:
            home, away = fixture_teams(fixture)
            haystack = football_resolver.normalize_key(f"{home} {away} {fixture.get('league', {})}")
            if normalized in haystack or any(part and part in haystack for part in normalized.split()):
                return fixture
        return None

    def _football_player_alias_cache(self) -> dict[str, dict[str, Any]]:
        cache = getattr(self.bot, "football_player_alias_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self.bot, "football_player_alias_cache", cache)
        return cache

    def _reaction_target_for_decision(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        image_context: ChatImageContext,
        replied_message: discord.Message | None,
        pending_interaction: PendingInteraction | None,
    ) -> object | None:
        target_id = decision.target_message
        candidates = [message, replied_message, image_context.reaction_target]
        if pending_interaction is not None and pending_interaction.target_message_id is not None:
            target_id = target_id or pending_interaction.target_message_id
        if target_id is not None:
            for candidate in candidates:
                if candidate is not None and getattr(candidate, "id", None) == target_id:
                    return candidate
        if replied_message is not None and decision.reason_code in {"REPLY_CONTINUATION", "IMAGE_ANALYSIS_REQUEST"}:
            return replied_message
        return image_context.reaction_target or message

    async def _is_passive_ai_blocked_channel(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel | discord.Thread,
        settings: object,
    ) -> bool:
        channel_ids = {getattr(channel, "id", None)}
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            channel_ids.add(parent_id)

        blocked_ids: set[int] = set()
        modlog_channel_id = getattr(settings, "modlog_channel_id", None)
        if isinstance(modlog_channel_id, int):
            blocked_ids.add(modlog_channel_id)

        try:
            birthday_settings = await self.bot.db.get_or_create_birthday_guild_settings(guild.id)
            birthday_channel_id = birthday_settings.get("channel_id")
            if isinstance(birthday_channel_id, int):
                blocked_ids.add(birthday_channel_id)
        except Exception:
            logging.exception("Failed to read birthday channel for passive AI block")

        for kind in ("welcome", "goodbye"):
            try:
                announcement_settings = await self.bot.db.get_announcement_settings(guild.id, kind)
                announcement_channel_id = getattr(announcement_settings, "channel_id", None)
                if isinstance(announcement_channel_id, int):
                    blocked_ids.add(announcement_channel_id)
            except Exception:
                logging.exception("Failed to read %s channel for passive AI block", kind)

        return any(channel_id in blocked_ids for channel_id in channel_ids if channel_id is not None)

    @staticmethod
    def _looks_like_command_message(content: str, *, configured_prefix: str) -> bool:
        stripped = content.strip()
        if not stripped:
            return False

        prefix = (configured_prefix or "").strip()
        if prefix and stripped.startswith(prefix):
            return True

        if len(stripped) < 2 or stripped[0] not in "/!?.$-+":
            return False
        if stripped[1].isspace():
            return False
        return bool(re.match(r"^[/!?.$+_-][A-Za-z0-9_][\w-]*\b", stripped))

    def _conversation_mode_for_trigger(
        self,
        replied_message: discord.Message | None,
        *,
        is_active_followup: bool,
        is_reply_to_ai: bool = False,
    ) -> str:
        if is_active_followup:
            return "continuation"
        if replied_message is not None and is_reply_to_ai:
            return "reply"
        return "mention"

    def _should_reply_to_trigger(
        self,
        message: discord.Message,
        *,
        is_direct_trigger: bool,
    ) -> bool:
        return self._send_mode_for_trigger(message, is_direct_trigger=is_direct_trigger) == "reply_to_trigger"

    def _send_mode_for_trigger(
        self,
        message: discord.Message,
        *,
        is_direct_trigger: bool,
    ) -> str:
        if self._channel_is_noisy(message):
            return "reply_to_trigger"
        bot_was_mentioned = self.bot.user is not None and self.bot.user in message.mentions
        if is_direct_trigger and (message.reference is not None or bot_was_mentioned):
            return "reply_to_trigger"
        return "normal"

    def _record_channel_human_activity(self, message: discord.Message) -> None:
        if getattr(message, "guild", None) is None or getattr(message, "channel", None) is None:
            return
        key = self._message_channel_key(message)
        now = time.monotonic()
        self._channel_human_activity[key].append((now, int(getattr(message.author, "id", 0) or 0)))
        self._refresh_channel_noise_state(key, now=now)

    def _record_channel_ai_activity(self, message: discord.Message) -> None:
        if getattr(message, "guild", None) is None or getattr(message, "channel", None) is None:
            return
        key = self._message_channel_key(message)
        now = time.monotonic()
        self._channel_ai_activity[key].append(now)
        self._refresh_channel_noise_state(key, now=now)

    def _channel_is_noisy(self, message: discord.Message) -> bool:
        if getattr(message, "guild", None) is None or getattr(message, "channel", None) is None:
            return False
        key = self._message_channel_key(message)
        now = time.monotonic()
        self._refresh_channel_noise_state(key, now=now)
        return self._channel_noisy_until.get(key, 0.0) > now

    def _refresh_channel_noise_state(self, key: tuple[int, int], *, now: float) -> None:
        window_start = now - self._channel_noise_window_seconds
        humans = self._channel_human_activity.get(key)
        if humans is not None:
            while humans and humans[0][0] < window_start:
                humans.popleft()
        ai_items = self._channel_ai_activity.get(key)
        if ai_items is not None:
            while ai_items and ai_items[0] < window_start:
                ai_items.popleft()
        human_count = len(humans or ())
        distinct_humans = len({author_id for _created, author_id in (humans or ()) if author_id})
        ai_count = len(ai_items or ())
        pending_count = 0
        for pending in self._pending_interactions.values():
            if pending.canceled:
                continue
            if (pending.guild_id, pending.channel_id) == key and pending.created_at >= window_start:
                pending_count += 1
        if (human_count >= 4 and distinct_humans >= 2) or ai_count >= 2 or pending_count >= 2:
            self._channel_noisy_until[key] = now + self._channel_noise_decay_seconds

    def _extract_chat_prompt(self, message: discord.Message) -> str:
        if self.bot.user and self.bot.user in message.mentions:
            return self._remove_bot_mentions(message.content, self.bot.user.id)
        return message.content.strip()

    @staticmethod
    def _extract_supported_image_urls(attachments: list[discord.Attachment]) -> list[str]:
        urls: list[str] = []
        for attachment in attachments:
            content_type = (getattr(attachment, "content_type", "") or "").casefold()
            url = str(getattr(attachment, "url", "") or "").strip()
            if not url:
                continue
            if is_supported_image_url(url, content_type):
                urls.append(url)
            if len(urls) >= 4:
                break
        return urls

    @staticmethod
    def _extract_supported_embed_image_urls(embeds: list[discord.Embed]) -> list[str]:
        urls: list[str] = []
        for embed in embeds or []:
            for attr in ("image", "thumbnail"):
                asset = getattr(embed, attr, None)
                url = str(getattr(asset, "url", "") or "").strip()
                if not url:
                    continue
                content_type = str(getattr(asset, "content_type", "") or "").strip()
                if is_supported_image_url(url, content_type):
                    urls.append(url)
                if len(urls) >= 4:
                    return urls
        return urls

    @classmethod
    def _build_chat_image_context(
        cls,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> ChatImageContext:
        current_direct_urls = cls._extract_supported_image_urls(message.attachments)
        current_preview_urls = cls._extract_supported_embed_image_urls(getattr(message, "embeds", []))
        replied_direct_urls = (
            cls._extract_supported_image_urls(replied_message.attachments)
            if replied_message is not None
            else []
        )
        replied_preview_urls: list[str] = []
        if replied_message is not None:
            replied_preview_urls = cls._extract_supported_embed_image_urls(getattr(replied_message, "embeds", []))

        current_urls = current_direct_urls + current_preview_urls
        replied_urls = replied_direct_urls + replied_preview_urls

        urls: list[str] = []
        seen: set[str] = set()
        for url in current_urls + replied_urls:
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= 4:
                break

        from_replied = bool(replied_urls)
        target = replied_message if from_replied and replied_message is not None else message
        if current_direct_urls:
            preferred_source_kind = "current_message"
            source_message_id = getattr(message, "id", None)
        elif current_preview_urls:
            preferred_source_kind = "source_preview"
            source_message_id = getattr(message, "id", None)
        elif replied_direct_urls and replied_message is not None:
            preferred_source_kind = "reply_target"
            source_message_id = getattr(replied_message, "id", None)
        elif replied_preview_urls and replied_message is not None:
            preferred_source_kind = "reply_source_preview"
            source_message_id = getattr(replied_message, "id", None)
        else:
            preferred_source_kind = "none"
            source_message_id = None
        logging.info(
            "AI visual source identity current_hashes=%s reply_hashes=%s preferred_source_kind=%s source_message_id=%s",
            tuple(hash_text(url) for url in current_direct_urls[:4]),
            tuple(hash_text(url) for url in replied_direct_urls[:4]),
            preferred_source_kind,
            source_message_id,
        )
        prompt_note = ""
        if replied_direct_urls and replied_message is not None:
            prompt_note = (
                "Image context: image attachment(s) are from the replied-to message."
            )
        elif replied_preview_urls and replied_message is not None:
            prompt_note = "Visual context: the replied message contains a preview of an external source."
        return ChatImageContext(
            urls=urls,
            from_replied_message=from_replied,
            reaction_target=target,
            prompt_note=prompt_note,
            current_message_images=tuple(current_urls[:4]),
            reply_target_images=tuple(replied_urls[:4]),
            preferred_source_kind=preferred_source_kind,
            source_message_id=source_message_id,
            current_message_direct_images=tuple(current_direct_urls[:4]),
            current_message_preview_images=tuple(current_preview_urls[:4]),
            reply_target_direct_images=tuple(replied_direct_urls[:4]),
            reply_target_preview_images=tuple(replied_preview_urls[:4]),
            current_message_id=getattr(message, "id", None),
            reply_target_message_id=getattr(replied_message, "id", None) if replied_message is not None else None,
        )

    @staticmethod
    def _apply_image_context_note(prompt: str, image_context: ChatImageContext) -> str:
        if not image_context.prompt_note:
            return prompt
        return f"{prompt}\n\n[{image_context.prompt_note}]"

    @classmethod
    def _build_replied_message_context(
        cls,
        replied_message: discord.Message,
    ) -> RepliedMessageContext:
        lines: list[str] = []
        author = getattr(getattr(replied_message, "author", None), "display_name", "unknown")
        lines.append(f"Author: {author}")

        content = str(getattr(replied_message, "content", "") or "")
        if content:
            excerpt = content if len(content) <= 900 else content[:885] + " [text omitted]"
            lines.append(f"Text: {excerpt}")

        for index, embed in enumerate(getattr(replied_message, "embeds", [])[:2], start=1):
            title = " ".join(str(getattr(embed, "title", "") or "").split())
            description = " ".join(str(getattr(embed, "description", "") or "").split())
            if title:
                lines.append(f"Embed {index} title: {title[:220]}")
            if description:
                lines.append(f"Embed {index} description: {description[:500]}")
            for field in list(getattr(embed, "fields", []) or [])[:4]:
                name = " ".join(str(getattr(field, "name", "") or "").split())
                value = " ".join(str(getattr(field, "value", "") or "").split())
                if name or value:
                    lines.append(f"Embed {index} field: {name[:120]} = {value[:260]}")
            footer = getattr(embed, "footer", None)
            footer_text = " ".join(str(getattr(footer, "text", "") or "").split())
            if footer_text:
                lines.append(f"Embed {index} footer: {footer_text[:220]}")

        image_urls = tuple(
            dict.fromkeys(
                cls._extract_supported_image_urls(getattr(replied_message, "attachments", []))
                + cls._extract_supported_embed_image_urls(getattr(replied_message, "embeds", []))
            )
        )
        if not lines:
            return RepliedMessageContext()
        note = "[UNTRUSTED_REPLIED_MESSAGE_CONTEXT]\n" + "\n".join(lines[:14])
        return RepliedMessageContext(note=note, image_urls=image_urls[:4])

    @staticmethod
    def _apply_replied_message_context(prompt: str, context: RepliedMessageContext) -> str:
        if not context.note:
            return prompt
        return f"{prompt}\n\n{context.note}"


    @staticmethod
    def _image_artifact_terminal_result(
        capability: str,
        image_bytes: object,
        *,
        source: str,
        filename: str,
        provenance: dict[str, object] | None = None,
        scope: dict[str, object] | None = None,
        provider: str = "xai_image",
    ) -> CapabilityTerminalResult:
        if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_RESULT_IMAGE_ARTIFACT_BYTES:
            return normalize_terminal_result(
                capability,
                "PROVIDER_FAILURE",
                provider=provider,
                provenance={"source": source, **dict(provenance or {})},
                scope=scope,
                reason="invalid_image_artifact",
            )
        artifact_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        # The provider decides the encoding: declare what the bytes are, because Discord reports the sniffed type.
        if image_bytes.startswith(b"\xff\xd8\xff"):
            content_type, extension = "image/jpeg", ".jpg"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            content_type, extension = "image/webp", ".webp"
        else:
            content_type, extension = "image/png", ".png"
        filename = filename.rsplit(".", 1)[0] + extension
        return normalize_terminal_result(
            capability,
            "SUCCESS",
            provider=provider,
            payload={
                "artifact": image_bytes,
                "filename": filename,
                "content_type": content_type,
                "artifact_length": len(image_bytes),
                "artifact_hash": artifact_hash,
            },
            provenance={"source": source, **dict(provenance or {})},
            scope={"artifact_hash": artifact_hash, "media_count": 1, **dict(scope or {})},
        )

    async def _execute_image_generation_result_only(
        self,
        prompt: str | None,
    ) -> CapabilityTerminalResult:
        prompt_value = str(prompt or "")
        prompt_hash = hash_text(prompt_value)
        provider_started_at = time.perf_counter()
        endpoint = getattr(self.bot.llm_client, "IMAGE_GENERATION_URL", "/v1/images/generations")
        model = getattr(self.bot.llm_client, "image_model", "unavailable")
        try:
            image_bytes = await self.bot.llm_client.generate_image(prompt_value)
        except Exception:
            provider_duration_ms = int((time.perf_counter() - provider_started_at) * 1000)
            logging.exception("AI result-only image generation failure")
            logging.info(
                "AI image generation instrumentation execution_path=image_generate outcome=PROVIDER_FAILURE "
                "prompt_hash=%s prompt_length=%s endpoint=%s model=%s provider_duration_ms=%s "
                "artifact_length=0 artifact_hash=unavailable",
                prompt_hash,
                len(prompt_value),
                endpoint,
                model,
                provider_duration_ms,
            )
            return normalize_terminal_result(
                "IMAGE_GENERATE",
                "PROVIDER_FAILURE",
                provider="xai_image",
                provenance={"source": "generate_image"},
                reason="provider_failure",
            )
        result = self._image_artifact_terminal_result(
            "IMAGE_GENERATE",
            image_bytes,
            source="generate_image",
            filename="nitori_generated.png",
            provenance={
                "execution_path": "image_generate",
                "prompt_hash": prompt_hash,
                "prompt_length": len(prompt_value),
                "endpoint": endpoint,
                "model": model,
                "provider_duration_ms": int((time.perf_counter() - provider_started_at) * 1000),
            },
        )
        payload = result.payload if isinstance(result.payload, dict) else {}
        logging.info(
            "AI image generation instrumentation execution_path=image_generate outcome=%s "
            "prompt_hash=%s prompt_length=%s endpoint=%s model=%s provider_duration_ms=%s "
            "artifact_length=%s artifact_hash=%s",
            result.status.value,
            prompt_hash,
            len(prompt_value),
            endpoint,
            model,
            result.provenance.get("provider_duration_ms", "unavailable"),
            payload.get("artifact_length", 0),
            payload.get("artifact_hash", "unavailable"),
        )
        return result

    async def _execute_image_edit_result_only(
        self,
        prompt: str | None,
        source_image_url: str | None,
        *,
        source_kind: str,
        source_message_id: int | None,
    ) -> CapabilityTerminalResult:
        source_identity = hash_text(str(source_image_url or "")) if source_image_url else None
        if not source_image_url:
            return normalize_terminal_result(
                "IMAGE_EDIT",
                "VALIDATION_FAILED",
                provider="xai_image",
                provenance={"source": "edit_image", "input_source_kind": source_kind},
                reason="missing_edit_image",
            )
        provider_started_at = time.perf_counter()
        try:
            image_bytes = await self.bot.llm_client.edit_image(str(prompt or ""), source_image_url)
        except Exception:
            provider_duration_ms = int((time.perf_counter() - provider_started_at) * 1000)
            logging.exception("AI result-only image edit failure source_kind=%s", source_kind)
            return normalize_terminal_result(
                "IMAGE_EDIT",
                "PROVIDER_FAILURE",
                provider="xai_image",
                provenance={
                    "source": "edit_image",
                    "input_source_kind": source_kind,
                    "input_context_identity": source_identity,
                    "input_source_message_id": source_message_id,
                    "execution_path": "direct_image_edit",
                    "provider_duration_ms": provider_duration_ms,
                },
                reason="provider_failure",
            )
        source_context = {
            "input_context_identity": source_identity,
            "input_source_message_id": source_message_id,
            "execution_path": "direct_image_edit",
            "provider_duration_ms": int((time.perf_counter() - provider_started_at) * 1000),
        }
        return self._image_artifact_terminal_result(
            "IMAGE_EDIT",
            image_bytes,
            source="edit_image",
            filename="nitori_edited.png",
            provenance={"input_source_kind": source_kind, **source_context},
            scope=source_context,
        )




    @staticmethod
    def _message_channel_key(message: discord.Message) -> tuple[int, int]:
        return (int(message.guild.id), int(message.channel.id))

    @staticmethod
    def _remove_bot_mentions(text: str, bot_id: int) -> str:
        text = text.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "")
        return text.strip()

    @staticmethod
    def _conversation_key(guild_id: int, channel_id: int) -> tuple[int, int]:
        return (guild_id, channel_id)

    def _build_conversation_history(
        self, key: tuple[int, int]
    ) -> list[dict[str, str]]:
        history = self._conversation_history.get(key)
        if not history:
            return []
        return list(history)

    async def _build_branch_conversation_history(
        self,
        key: tuple[int, int],
        message: discord.Message,
        replied_message: discord.Message | None,
        *,
        lease: ContinuationLease | None,
        is_reply_to_ai: bool,
    ) -> list[dict[str, str]]:
        if is_reply_to_ai and replied_message is not None:
            chain = await self._persisted_parent_chain(message.guild.id, message.channel.id, replied_message.id)
            if chain:
                return chain[-28:]

        base = self._build_conversation_history(key)[-16:]
        if lease is not None:
            same_user_history = await self._same_user_branch_history(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                owner_user_id=lease.owner_user_id,
            )
            if same_user_history:
                base = same_user_history

        return base[-28:]

    async def _persisted_parent_chain(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> list[dict[str, str]]:
        getter = getattr(self.bot.db, "get_ai_parent_chain", None)
        if getter is None:
            return []
        try:
            rows = await getter(guild_id, channel_id, message_id, limit=20)
        except Exception:
            logging.exception("Failed to load AI parent chain")
            return []
        return self._history_from_db_rows(rows)

    async def _same_user_branch_history(
        self,
        *,
        guild_id: int,
        channel_id: int,
        owner_user_id: int,
    ) -> list[dict[str, str]]:
        getter = getattr(self.bot.db, "get_ai_conversation_history", None)
        if getter is None:
            return []
        try:
            rows = await getter(guild_id=guild_id, channel_id=channel_id, limit=80)
        except TypeError:
            rows = await getter(guild_id, channel_id, limit=80)
        except Exception:
            logging.exception("Failed to load same-user AI branch history")
            return []
        bot_user_id = getattr(self.bot.user, "id", None)
        filtered = []
        for row in rows:
            author_id = str(row.get("author_user_id", "") or "")
            role = str(row.get("role", "") or "").lower()
            if author_id and author_id not in {str(owner_user_id), str(bot_user_id)}:
                continue
            if not author_id and role == "user":
                continue
            filtered.append(row)
        return self._history_from_db_rows(filtered)

    @staticmethod
    def _history_from_db_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for row in rows:
            role = str(row.get("role", "")).strip().lower()
            content = str(row.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                history.append({"role": role, "content": content})
        return history

    @staticmethod
    def _parent_message_id(message: discord.Message) -> int | None:
        reference = getattr(message, "reference", None)
        parent_id = getattr(reference, "message_id", None)
        if parent_id is None:
            return None
        try:
            return int(parent_id)
        except (TypeError, ValueError):
            return None


    def _author_permissions_text(self, message: discord.Message) -> str:
        authority = self._route_authority_metadata(message)
        flags = (
            ("administrator", "author_has_administrator"), ("manage_guild", "author_has_manage_guild"),
            ("manage_channels", "author_has_manage_channels"), ("manage_roles", "author_has_manage_roles"),
            ("manage_messages", "author_has_manage_messages"), ("moderate_members", "author_has_moderate_members"),
        )
        return ", ".join(f"{name}={'yes' if authority[key] else 'no'}" for name, key in flags)

    def _route_authority_metadata(self, message: discord.Message) -> dict[str, bool]:
        is_owner = bool(getattr(self.bot, "is_owner_user", lambda _user: False)(message.author))
        guild_owner_id = getattr(getattr(message, "guild", None), "owner_id", None)
        is_guild_owner = guild_owner_id is not None and int(getattr(message.author, "id", 0) or 0) == int(guild_owner_id)
        permissions = getattr(message.author, "guild_permissions", None)
        has_admin = bool(getattr(permissions, "administrator", False))
        has_manage_guild = bool(getattr(permissions, "manage_guild", False))
        has_moderate_members = bool(getattr(permissions, "moderate_members", False))
        has_manage_channels = bool(getattr(permissions, "manage_channels", False))
        has_manage_messages = bool(getattr(permissions, "manage_messages", False))
        has_manage_roles = bool(getattr(permissions, "manage_roles", False))
        can_manage = is_owner or is_guild_owner or has_admin or has_manage_guild
        return {
            "author_is_bot_owner": is_owner,
            "author_is_guild_owner": is_guild_owner,
            "author_has_administrator": has_admin,
            "author_has_manage_guild": has_manage_guild,
            "author_has_moderate_members": has_moderate_members,
            "author_has_manage_channels": has_manage_channels,
            "author_has_manage_messages": has_manage_messages,
            "author_has_manage_roles": has_manage_roles,
            "author_can_manage_bot_behavior": can_manage,
            "author_can_use_ai_moderation": is_owner
            or is_guild_owner
            or has_admin
            or has_manage_guild
            or has_moderate_members
            or has_manage_channels
            or has_manage_messages,
        }

    _QUOTED_DELIVERY_TEXT_RE: Final[re.Pattern[str]] = re.compile(
        r"\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|\u00ab[^\u00bb]*\u00bb",
        flags=re.DOTALL,
    )

    @staticmethod
    def _normalize_delivery_evidence(text: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or "")).casefold()).strip()

    def _delivery_evidence_text_for_message(self, message: discord.Message) -> str:
        text = str(getattr(message, "content", "") or "")
        bot_user = getattr(self.bot, "user", None)
        bot_id = getattr(bot_user, "id", None)
        if bot_id is not None:
            text = self._remove_bot_mentions(text, bot_id)
        return self._QUOTED_DELIVERY_TEXT_RE.sub(" ", text)

    def _validated_response_delivery_for_message(
        self,
        message: discord.Message,
        response_delivery: object,
    ) -> VoiceResponseDecision:
        if not isinstance(response_delivery, dict):
            return VoiceResponseDecision(ResponseModality.UNSPECIFIED, False, source="semantic_router", reason="missing_response_delivery")

        raw_modality = str(response_delivery.get("modality", "UNSPECIFIED")).strip().upper()
        raw_source = str(response_delivery.get("source", "UNSPECIFIED")).strip().upper()
        explicit = bool(response_delivery.get("explicit", False))
        evidence = str(response_delivery.get("evidence_span", "") or "").strip()
        semantic_reason = str(response_delivery.get("semantic_reason", "") or "").strip()
        failure_reason = str(response_delivery.get("failure_reason", "") or "").strip()
        ambiguous_reason = str(response_delivery.get("ambiguous_reason", "") or "").strip()

        if raw_modality == "TEXT":
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason=semantic_reason or "explicit_text_or_no_voice_delivery")
        if raw_modality != "VOICE":
            return VoiceResponseDecision(ResponseModality.UNSPECIFIED, False, source="semantic_router", reason=semantic_reason or "unspecified_response_delivery")
        if failure_reason or ambiguous_reason:
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason=failure_reason or ambiguous_reason)
        if not explicit:
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason="voice_delivery_not_explicit")
        if raw_source != "CURRENT_MESSAGE":
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason="voice_delivery_not_current_message")
        if not evidence:
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason="voice_delivery_missing_evidence")

        normalized_evidence = self._normalize_delivery_evidence(evidence)
        normalized_current = self._normalize_delivery_evidence(self._delivery_evidence_text_for_message(message))
        if not normalized_evidence or normalized_evidence not in normalized_current:
            return VoiceResponseDecision(ResponseModality.TEXT, False, source="semantic_router", reason="voice_delivery_evidence_not_in_current_message")
        return VoiceResponseDecision(ResponseModality.VOICE, True, source="current_message", reason=semantic_reason or "explicit_voice_output_request")

    def _remember_response_delivery_decision(self, message: discord.Message, route_decision: RouteDecision) -> None:
        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        decision = self._validated_response_delivery_for_message(message, route_decision.response_delivery)
        message_key = int(message_id)
        existing = self._voice_response_decisions.get(message_key)
        if (
            existing is not None
            and existing.modality == ResponseModality.VOICE
            and route_decision.response_delivery is None
        ):
            decision = existing
        if message_key not in self._voice_response_decisions:
            if len(self._voice_response_decision_ids) >= self._voice_response_decision_ids.maxlen:
                oldest = self._voice_response_decision_ids.popleft()
                self._voice_response_decisions.pop(oldest, None)
            self._voice_response_decision_ids.append(message_key)
        self._voice_response_decisions[message_key] = decision
        logging.info(
            "AI voice modality guild=%s channel=%s message=%s request_hash=%s voice_intent_detected=%s voice_intent_source=%s response_modality=%s reason=%s",
            getattr(getattr(message, "guild", None), "id", None),
            getattr(getattr(message, "channel", None), "id", None),
            getattr(message, "id", None),
            abs(hash(str(getattr(message, "content", "") or ""))) % 100000,
            decision.intent_detected,
            decision.source,
            decision.modality.value,
            decision.reason,
        )


    @staticmethod
    def _voice_failure_visible_text(text: str) -> str:
        tag_names = "|".join(re.escape(tag) for tag in sorted(ALLOWED_TTS_TAGS, key=len, reverse=True))
        if not tag_names:
            return text
        # Display fallback only: successful voice keeps provider tags for TTS.
        cleaned = re.sub(rf"(?<![`\\])\[(?:{tag_names})\](?!`)", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip() or text

    def _response_modality_for_message(self, message: discord.Message) -> ResponseModality:
        message_id = getattr(message, "id", None)
        if message_id in self._voice_response_consumed_set:
            return ResponseModality.TEXT
        if message_id is None:
            return ResponseModality.TEXT
        decision = self._voice_response_decisions.get(int(message_id))
        return decision.modality if decision is not None and decision.modality == ResponseModality.VOICE else ResponseModality.TEXT

    def _consume_voice_response_modality(self, message: discord.Message) -> bool:
        if self._response_modality_for_message(message) != ResponseModality.VOICE:
            return False
        message_id = getattr(message, "id", None)
        if message_id is None:
            return False
        if len(self._voice_response_consumed_ids) >= self._voice_response_consumed_ids.maxlen:
            oldest = self._voice_response_consumed_ids.popleft()
            self._voice_response_consumed_set.discard(oldest)
        self._voice_response_consumed_ids.append(int(message_id))
        self._voice_response_consumed_set.add(int(message_id))
        return True

    async def _send_voice_reply(
        self,
        trigger_message: discord.Message,
        text: str,
    ) -> int:
        guild_id = getattr(getattr(trigger_message, "guild", None), "id", None)
        channel_id = getattr(getattr(trigger_message, "channel", None), "id", None)
        message_id = getattr(trigger_message, "id", None)
        logging.info(
            "AI voice pipeline event=voice_pipeline_entered guild=%s channel=%s message=%s",
            guild_id,
            channel_id,
            message_id,
        )
        llm_client = getattr(self.bot, "llm_client", None)
        if llm_client is None or not hasattr(llm_client, "text_to_speech"):
            raise RuntimeError("voice_output_not_configured")
        tts_text = sanitize_tts_text(text)
        try:
            logging.info("AI voice pipeline event=tts_started guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            audio_bytes, content_type = await llm_client.text_to_speech(tts_text)
            logging.info("AI voice pipeline event=tts_success guild=%s channel=%s message=%s bytes=%s", guild_id, channel_id, message_id, len(audio_bytes))
        except Exception:
            logging.exception("AI voice pipeline event=tts_failure guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            raise
        extension = ".wav" if "wav" in content_type.casefold() else ".mp3"
        processor = getattr(self.bot, "voice_audio_processor", None) or VoiceAudioProcessor()
        try:
            logging.info("AI voice pipeline event=audio_processing_started guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            processed = await processor.process(audio_bytes, source_extension=extension)
            logging.info("AI voice pipeline event=audio_processing_success guild=%s channel=%s message=%s duration=%.3f", guild_id, channel_id, message_id, processed.duration_seconds)
        except Exception:
            logging.exception("AI voice pipeline event=audio_processing_failure guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            raise
        sender = getattr(self.bot, "discord_voice_sender", None) or DiscordVoiceMessageSender(self.bot)
        try:
            logging.info("AI voice pipeline event=discord_voice_upload_started guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            sent_id = await sender.send(trigger_message.channel, processed)
            logging.info("AI voice pipeline event=discord_voice_upload_success guild=%s channel=%s message=%s sent_message=%s", guild_id, channel_id, message_id, sent_id)
            return sent_id
        except Exception:
            logging.exception("AI voice pipeline event=discord_voice_upload_failure guild=%s channel=%s message=%s", guild_id, channel_id, message_id)
            raise



    def _authorized_reply_source(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> tuple[bool, str]:
        reference_id = getattr(getattr(message, "reference", None), "message_id", None)
        target_id = getattr(replied_message, "id", None)
        author = getattr(replied_message, "author", None)
        if (
            reference_id is None
            or target_id is None
            or str(reference_id) != str(target_id)
            or replied_message is None
            or bool(getattr(author, "bot", False))
        ):
            return False, ""
        content = str(getattr(replied_message, "content", "") or "")
        excerpt = content if len(content) <= self._history_entry_max_chars else content[:self._history_entry_max_chars - 15] + " [text omitted]"
        return True, excerpt


    def _active_typed_context_for_message(
        self,
        message: discord.Message,
    ) -> tuple[CapabilityExecutionPlan, CapabilityResultBundle] | None:
        lease = self._valid_lease_for_message(message)
        if lease is None or getattr(message.author, "id", None) != lease.owner_user_id:
            return None
        return lease.active_typed_context

    def _authorized_active_media_for_message(
        self,
        message: discord.Message,
    ) -> tuple[tuple[str, ...], str, int | None]:
        prior_typed_context = self._active_typed_context_for_message(message)
        if prior_typed_context is None:
            return (), "none", None
        _prior_plan, prior_bundle = prior_typed_context
        if not prior_bundle.satisfied:
            return (), "none", None
        for result in prior_bundle.results:
            payload = result.payload
            raw_urls = payload.get("media_urls") if isinstance(payload, dict) else None
            if not isinstance(raw_urls, (list, tuple)) or not 1 <= len(raw_urls) <= 4:
                continue
            media_urls = tuple(str(value or "").strip() for value in raw_urls)
            identity = result.scope.get("context_identity") or result.provenance.get("context_identity")
            source_message_id = result.scope.get("source_message_id")
            try:
                media_count = int(result.scope.get("media_count", 0) or 0)
            except (TypeError, ValueError):
                continue
            if (
                result.status != CapabilityTerminalStatus.SUCCESS
                or not result.terminal
                or not result.satisfies_request
                or result.provenance.get("authorized") is not True
                or not all(media_urls)
                or media_count != len(media_urls)
                or not identity
                or source_message_id is None
            ):
                continue
            if identity != hash_text("|".join(media_urls)):
                continue
            try:
                normalized_source_message_id = int(source_message_id)
            except (TypeError, ValueError):
                continue
            return (
                media_urls,
                str(result.provenance.get("source_kind") or "typed_media_artifact"),
                normalized_source_message_id,
            )
        return (), "none", None


    def _selected_image_source(
        self,
        request: object,
        task_operands: dict[str, object],
        image_context: ChatImageContext | None,
        message: discord.Message,
        label: str,
    ) -> tuple[dict[str, object] | None, str | None]:
        raw_source_refs = getattr(request, "source_refs", ()) or ()
        if not raw_source_refs:
            return None, f"missing_{label}_source_ref"
        if not isinstance(raw_source_refs, (tuple, list)) or len(raw_source_refs) != 1:
            return None, f"invalid_{label}_source_ref"
        selected_ref = raw_source_refs[0]
        if not isinstance(selected_ref, str) or not selected_ref.strip():
            return None, f"missing_{label}_source_ref"
        projection = self._image_source_projection(
            image_context or ChatImageContext(urls=[], from_replied_message=False, reaction_target=message)
        )
        selected_source = next(
            (item for item in projection if str(item.get("ref") or "") == selected_ref.strip()),
            None,
        )
        if selected_source is None:
            return None, f"invalid_{label}_source_ref"
        supplied_source_url = task_operands.get("source_url")
        if supplied_source_url is not None and str(supplied_source_url).strip() != str(selected_source["url"]):
            return None, f"invalid_{label}_source_ref"
        return selected_source, None

    @staticmethod
    def _image_source_projection(image_context: ChatImageContext) -> tuple[dict[str, object], ...]:
        candidates: list[tuple[str, str, int | None]] = []
        candidates.extend(
            ("current_message", url, image_context.current_message_id)
            for url in image_context.current_message_direct_images
        )
        candidates.extend(
            ("reply_target", url, image_context.reply_target_message_id)
            for url in image_context.reply_target_direct_images
        )
        branch_source_kind = image_context.prior_branch_source_kind or image_context.preferred_source_kind
        branch_source_message_id = (
            image_context.prior_branch_source_message_id
            if image_context.prior_branch_source_message_id is not None
            else image_context.source_message_id
        )
        candidates.extend(
            (branch_source_kind, url, branch_source_message_id)
            for url in image_context.prior_branch_images
        )
        entries: list[dict[str, object]] = []
        seen: set[str] = set()
        for index, (source_kind, url, source_message_id) in enumerate(candidates, start=1):
            if not url or url in seen or len(entries) >= 4:
                continue
            seen.add(url)
            entries.append(
                {
                    "ref": f"img:{index}:{hash_text(f'{image_context.current_message_id}|{source_kind}|{source_message_id}|{url}')[:10]}",
                    "url": url,
                    "source_kind": source_kind,
                    "source_message_id": source_message_id,
                }
            )
        return tuple(entries)


    @staticmethod
    def _anon_id(value: object) -> str:
        return hex(abs(hash(str(value))) % 0xFFFFFF)[2:].zfill(6)

    @staticmethod
    def _mention_identity_signatures(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        user_ids = tuple(dict.fromkeys(re.findall(r"<@!?([0-9]{15,22})>", str(text or ""))))[:8]
        names = tuple(
            dict.fromkeys(
                hash_text(match.casefold())
                for match in re.findall(r"(?<![\w<])@([A-Za-z0-9_.-]{2,64})", str(text or ""))
                if not match.isdigit()
            )
        )[:8]
        return user_ids, names


    async def _channel_history_intent_context(self, message: discord.Message) -> list[dict[str, Any]]:
        history = getattr(message.channel, "history", None)
        if history is None:
            return []
        current_id = getattr(message, "id", None)
        context: list[dict[str, Any]] = []
        try:
            async for item in history(limit=12):
                if item is message or getattr(item, "id", None) == current_id:
                    continue
                if self._is_slash_command_response_message(item):
                    continue
                content = " ".join(str(getattr(item, "content", "") or "").strip().split())
                if not content:
                    continue
                author = getattr(item, "author", None)
                author_name = (
                    getattr(author, "display_name", None)
                    or getattr(author, "name", None)
                    or "unknown"
                )
                reference = getattr(item, "reference", None)
                context.append(
                    {
                        "author": str(author_name),
                        "author_id": getattr(author, "id", None),
                        "is_bot": bool(getattr(author, "bot", False)),
                        "content": content[:200],
                        "reply_to": getattr(reference, "message_id", None),
                        "mentions": [
                            getattr(member, "id", None)
                            for member in (getattr(item, "mentions", []) or [])
                        ],
                        "has_supported_image": bool(
                            self._extract_supported_image_urls(getattr(item, "attachments", []))
                            or self._extract_supported_embed_image_urls(getattr(item, "embeds", []))
                        ),
                    }
                )
        except Exception:
            logging.exception("Failed to read channel history for AI intent classifier")
            return []
        context.reverse()
        return context

    async def _ensure_history_loaded(
        self,
        key: tuple[int, int],
        guild_id: int,
        channel_id: int,
    ) -> None:
        history = self._conversation_history.get(key)
        if history and len(history) > 0:
            return
        rows = await self.bot.db.get_ai_conversation_history(
            guild_id=guild_id,
            channel_id=channel_id,
            limit=120,
        )
        loaded = deque(maxlen=120)
        for row in rows:
            role = str(row.get("role", "")).strip().lower()
            content = str(row.get("content", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            loaded.append({"role": role, "content": content})
        self._conversation_history[key] = loaded

    def _append_conversation_turn(
        self,
        key: tuple[int, int],
        *,
        role: str,
        speaker: str,
        content: str,
    ) -> str | None:
        cleaned = content
        if not cleaned.strip():
            return None
        if len(cleaned) > self._history_entry_max_chars:
            cleaned = cleaned[: self._history_entry_max_chars - 15] + " [text omitted]"
        # The assistant role already names the bot; a name label in its text gets copied into new replies.
        prefixed = cleaned if role == "assistant" else f"{speaker}: {cleaned}"
        history = self._conversation_history[key]
        if history:
            last = history[-1]
            if last.get("role") == role and last.get("content") == prefixed:
                return None
        history.append({"role": role, "content": prefixed})
        return prefixed

    async def _persist_conversation_turn(
        self,
        *,
        guild_id: int,
        channel_id: int,
        role: str,
        speaker: str,
        content: str,
        message_id: int | None = None,
        author_user_id: int | None = None,
        parent_message_id: int | None = None,
        action_type: str | None = None,
        resolved_request: str | None = None,
    ) -> None:
        await self.bot.db.add_ai_conversation_turn(
            guild_id=guild_id,
            channel_id=channel_id,
            role=role,
            speaker=speaker,
            content=content,
            message_id=message_id,
            author_user_id=author_user_id,
            parent_message_id=parent_message_id,
            action_type=action_type,
            resolved_request=resolved_request,
        )

    @staticmethod
    def _channel_reference(channel: discord.abc.GuildChannel | discord.Thread) -> str:
        return f"<#{channel.id}>"

    async def _send_long_reply(
        self,
        trigger_message: discord.Message,
        text: str,
        *,
        mention_author: bool,
        reply_to_trigger: bool | None = None,
        send_mode: str | None = None,
        delete_after: float | None = None,
        attachment: discord.File | None = None,
        files: list[discord.File] | None = None,
        on_first_delivery: Any = None,
    ) -> int | object | None:
        if attachment is not None or files:
            self._consume_voice_response_modality(trigger_message)
        elif self._consume_voice_response_modality(trigger_message):
            try:
                message_id = await self._send_voice_reply(trigger_message, text)
                self._remember_chat_response_message(message_id)
                self._record_channel_ai_activity(trigger_message)
                return message_id
            except Exception as exc:
                logging.exception(
                    "AI voice response failed guild=%s channel=%s message=%s",
                    getattr(getattr(trigger_message, "guild", None), "id", None),
                    getattr(getattr(trigger_message, "channel", None), "id", None),
                    getattr(trigger_message, "id", None),
                )
                if isinstance(exc, XAITTSAuthorizationError):
                    text = (
                        "Voice output is not authorized for the configured xAI API key, so here is the text instead.\n\n"
                        f"{self._voice_failure_visible_text(text)}"
                    )
                else:
                    text = (
                        "I could not send that as a voice message, so here is the text instead.\n\n"
                        f"{self._voice_failure_visible_text(text)}"
                    )
        parts = self._split_for_discord(text, limit=1900)
        if not parts:
            return None
        mention_ids, mention_name_hashes = self._mention_identity_signatures(text)
        logging.info(
            "AI Discord delivery identity output_hash=%s mention_ids=%s mention_name_hashes=%s part_count=%s",
            hash_text(text),
            mention_ids,
            mention_name_hashes,
            len(parts),
        )
        allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
        # Cited links stay clickable without Discord preview cards, so a sourced answer stays compact.
        if send_mode is None:
            send_mode = "reply_to_trigger" if (True if reply_to_trigger is None else reply_to_trigger) else "normal"
        should_reply = send_mode in {"reply_to_trigger", "force_reply"}
        if send_mode == "force_channel":
            should_reply = False
        if should_reply:
            try:
                first = await trigger_message.reply(
                    parts[0],
                    **({"files": files} if files else {"file": attachment} if attachment is not None else {}),
                    mention_author=mention_author,
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    delete_after=delete_after,
                )
            except Exception:
                logging.exception(
                    "AI Discord reply failed; falling back to channel send guild=%s channel=%s message=%s",
                    getattr(getattr(trigger_message, "guild", None), "id", None),
                    getattr(getattr(trigger_message, "channel", None), "id", None),
                    getattr(trigger_message, "id", None),
                )
                first = await trigger_message.channel.send(
                    parts[0],
                    **({"files": files} if files else {"file": attachment} if attachment is not None else {}),
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    delete_after=delete_after,
                )
        else:
            first = await trigger_message.channel.send(
                parts[0],
                **({"files": files} if files else {"file": attachment} if attachment is not None else {}),
                allowed_mentions=allowed_mentions,
                suppress_embeds=True,
                delete_after=delete_after,
            )
        if on_first_delivery is not None:
            on_first_delivery(first)
        self._remember_chat_response_message(first.id)
        self._record_channel_ai_activity(trigger_message)
        for part in parts[1:]:
            extra = await trigger_message.channel.send(
                part,
                allowed_mentions=allowed_mentions,
                suppress_embeds=True,
            )
            self._remember_chat_response_message(extra.id)
        return first if attachment is not None or files else int(first.id)

    def _is_chat_response_message(self, message_id: int) -> bool:
        return message_id in self._chat_response_id_set

    def _remember_chat_response_message(self, message_id: int) -> None:
        if message_id in self._chat_response_id_set:
            return
        if len(self._chat_response_ids) >= self._chat_response_id_limit:
            oldest = self._chat_response_ids.popleft()
            self._chat_response_id_set.discard(oldest)
        self._chat_response_ids.append(message_id)
        self._chat_response_id_set.add(message_id)

    def clear_guild_history(self, guild_id: int) -> None:
        for key in list(self._conversation_history.keys()):
            if key[0] == guild_id:
                del self._conversation_history[key]
        for key in list(self._continuation_leases.keys()):
            if key[0] == guild_id:
                del self._continuation_leases[key]
        for key in list(self._football_turn_contexts.keys()):
            if key[0] == guild_id:
                del self._football_turn_contexts[key]



    @staticmethod
    def _bounded_diagnostic_excerpt(
        text: object,
        *,
        forbidden: tuple[str, ...] = (),
        limit: int = 240,
    ) -> str:
        excerpt = " ".join(str(text or "").split())
        folded_excerpt = excerpt.casefold()
        for value in forbidden:
            candidate = " ".join(str(value or "").split())
            if candidate and candidate.casefold() in folded_excerpt:
                return "[redacted-forbidden-content]"
        excerpt = re.sub(r"https?://\S+", "[redacted-url]", excerpt)
        excerpt = re.sub(r"(?i)\b(?:bearer\s+|api[_ -]?key[=:]\s*)\S+", "[redacted-secret]", excerpt)
        excerpt = re.sub(r"\b(?:sk|xai)-[A-Za-z0-9_-]{8,}\b", "[redacted-secret]", excerpt)
        excerpt = re.sub(r"(?i)data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[redacted-image-data]", excerpt)
        excerpt = re.sub(r"<@!?\d{15,22}>|(?<![\w<])@[^\s@#<>]+", "[redacted-mention]", excerpt)
        return excerpt[:limit] or "[empty]"

    @staticmethod
    def _split_for_discord(text: str, *, limit: int) -> list[str]:
        if not text:
            return []
        chunks: list[str] = []
        remaining = text
        fence = ""
        marker = ""
        closing_size = max((len(match.group(1)) for match in re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})", text)), default=3) + 1
        while remaining:
            prefix = fence + "\n" if fence else ""
            if len(prefix) + len(remaining) <= limit:
                chunks.append(prefix + remaining)
                break
            available = max(1, limit - len(prefix) - closing_size)
            cut = remaining.rfind("\n", 0, available) + 1
            if not cut:
                cut = remaining.rfind(" ", 0, available) + 1 or available
            if cut < available // 2:
                cut = available
            piece, remaining = remaining[:cut], remaining[cut:]
            for match in re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})([^\r\n]*)", piece):
                token, language = match.groups()
                if not fence and len(token + language) + len(token) + 2 < limit:
                    fence, marker = token + language, token
                elif token[0] == marker[0] and len(token) >= len(marker) and not language.strip():
                    fence = marker = ""
            suffix = ("" if piece.endswith("\n") else "\n") + marker if fence else ""
            chunks.append(prefix + piece + suffix)
        return chunks


    @staticmethod
    async def _get_replied_message(message: discord.Message) -> discord.Message | None:
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved

        try:
            return await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _allowed_by_throttle(
        self,
        message: discord.Message,
        anchor_type: str,
        *,
        pending_context: bool = False,
    ) -> bool:
        if pending_context:
            return True
        if anchor_type in {"SAME_USER_CONTINUATION", "REPLY_TO_AI", "REPLY_TO_WATCH"}:
            return True
        if anchor_type in {"CANCEL_PENDING", "MODIFY_PENDING"}:
            return True
        key = (message.guild.id, message.channel.id, message.author.id)
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < self._cooldown_seconds:
            logging.info(
                "AI throttle blocked guild=%s channel=%s author=%s anchor=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                anchor_type,
            )
            return False
        self._cooldowns[key] = now
        return True

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _is_ai_limit_error(error: Exception) -> bool:
        msg = str(error).lower()
        markers = (
            "429",
            "rate limit",
            "too many requests",
            "tokens per minute",
            "request too large",
            "context length",
            "maximum context length",
            "token limit",
            "quota",
            "insufficient_quota",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _is_empty_completion_error(error: Exception) -> bool:
        return "empty completion" in str(error).lower()

    @staticmethod
    def _is_ai_image_access_error(error: Exception) -> bool:
        message = str(error).casefold()
        markers = (
            "image",
            "vision",
            "input_image",
            "unsupported",
            "model",
            "permission",
            "access",
            "endpoint",
        )
        return any(marker in message for marker in markers)





    @staticmethod
    def _serialize_text_channels(guild: discord.Guild) -> list[str]:
        channels = sorted(guild.text_channels, key=lambda c: c.position)
        serialized: list[str] = []
        for channel in channels[:60]:
            serialized.append(f"#{channel.name} (<#{channel.id}>)")
        return serialized

    @staticmethod
    def _serialize_custom_emojis(guild: discord.Guild) -> list[str]:
        serialized: list[str] = []
        for emoji in getattr(guild, "emojis", []):
            if not emoji.available or not emoji.is_usable():
                continue
            prefix = "a" if emoji.animated else ""
            serialized.append(f"{emoji.name}: <{prefix}:{emoji.name}:{emoji.id}>")
            if len(serialized) >= 80:
                break
        return serialized


    @staticmethod
    def _translate_usage_message(lang: str) -> str:
        return tr(lang, "Use: `/translate <language> [text]`", "Usa: `/translate <idioma> [texto]`")



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIChatCog(bot))
