from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
import difflib
import json
import logging
import re
import time
import unicodedata
from typing import Any, Final

import discord
from discord.ext import commands

from services.football_analysis_context import build_fixture_context, build_player_context, build_standings_context, build_team_context, football_grounding_prompt
from services.football_formatter import fixture_score, fixture_status, fixture_teams
from services.football_query_service import FootballQueryOperation, build_operation, clean_entity_candidates, looks_like_raw_sentence, season_hint_to_int
from services.football_resolver import canonical_team_query, normalize_league_key, pick_team
from services.football_watch import (
    FootballWatchSnapshot,
    build_watch_updates,
    is_terminal_status,
    should_fetch_lineups,
    should_fetch_statistics,
    snapshot_from_fixture,
)
from services import football_resolver
from services.server_memory import ServerMemoryInput, ServerMemoryService
from services.server_memory_context import ServerMemoryContextBuilder
from services.web_research import WebResearchRequest, WebResearchService
from services.web_research_context import football_web_grounding_prompt, format_web_research_context, web_grounding_prompt
from utils.discord_helpers import parse_user_id_from_text
from utils.i18n import tr


@dataclass(frozen=True)
class ChatImageContext:
    urls: list[str]
    from_replied_message: bool
    reaction_target: object
    prompt_note: str = ""
    current_message_images: tuple[str, ...] = ()
    reply_target_images: tuple[str, ...] = ()
    prior_branch_images: tuple[str, ...] = ()
    preferred_source_kind: str = "none"
    source_message_id: int | None = None


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
    pending_operation: str | None = None
    memory: dict[str, str] | None = None
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
class MissedResponseCandidate:
    guild_id: int
    channel_id: int
    author_user_id: int
    message_id: int | None
    created_at: float
    anchor_type: str
    reason: str
    snippet: str
    author_name: str


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
AMBIGUOUS_ANCHORS: Final[set[str]] = {"NAME_REFERENCE", "SAME_USER_CONTINUATION", "PENDING_FOLLOWUP", "MISSED_RESPONSE_REPAIR"}
AMBIGUOUS_ROUTING_CONFIDENCE: Final[float] = 0.85
IMAGE_ACTION_CONFIDENCE: Final[float] = 0.85
CONTINUATION_LEASE_SECONDS: Final[float] = 120.0
PENDING_INTERACTION_SECONDS: Final[float] = 90.0
PENDING_ACTIONS: Final[set[str]] = {"CHAT", "GENERATE_IMAGE", "EDIT_IMAGE", "ANALYZE_IMAGE", "CLARIFY"}
ROUTE_PARTICIPATIONS: Final[set[str]] = {"RESPOND", "REACT_ONLY", "IGNORE"}
ROUTE_ACTIONS: Final[set[str]] = {
    "CHAT",
    "ADD_REACTION",
    "REACT_ONLY",
    "GENERATE_IMAGE",
    "EDIT_IMAGE",
    "ANALYZE_IMAGE",
    "CLARIFY",
    "CANCEL_PENDING",
    "MODIFY_PENDING",
    "IGNORE",
    "NONE",
    "FOOTBALL_LOOKUP",
    "FOOTBALL_TABLE",
    "FOOTBALL_MATCH_CENTER",
    "FOOTBALL_TEAM_QUERY",
    "FOOTBALL_PLAYER_QUERY",
    "FOOTBALL_FIXTURE_QUERY",
    "FOOTBALL_PREVIEW",
    "FOOTBALL_SUMMARY",
    "FOOTBALL_COMPARISON",
    "FOOTBALL_WATCH_TODAY",
    "FOOTBALL_LIVE_WATCH_START",
    "FOOTBALL_LIVE_WATCH_STOP",
    "FOOTBALL_EXPLAIN_RESULT",
    "WEB_LOOKUP",
    "SERVER_MEMORY_LOOKUP",
    "SERVER_MEMORY_WRITE",
    "SERVER_MEMORY_UPDATE",
    "SERVER_MEMORY_DELETE",
    "SERVER_MEMORY_CLARIFY",
}
ROUTE_REASON_CODES: Final[set[str]] = {
    "DIRECT_REQUEST",
    "REPLY_CONTINUATION",
    "NAME_AT_START_REQUEST",
    "NAME_REFERENCE_REQUEST",
    "SAME_USER_CONTINUATION",
    "IMAGE_GENERATION_REQUEST",
    "IMAGE_ANALYSIS_REQUEST",
    "IMAGE_CONTEXT_EDIT",
    "REACTION_ACK",
    "CLARIFICATION_NEEDED",
    "ADDRESSED_TO_OTHER_USER",
    "QUOTING_OR_DISCUSSING_BOT",
    "NO_MEANINGFUL_CONTENT",
    "COMMAND_TRAFFIC",
    "UNRELATED_HUMAN_CHAT",
    "ROUTER_FAILURE",
    "MISSED_RESPONSE_REPAIR",
    "SERVER_MEMORY_REQUEST",
}
AMBIGUOUS_ALLOWED_REASON_CODES: Final[set[str]] = {
    "SAME_USER_CONTINUATION",
    "NAME_REFERENCE_REQUEST",
    "MISSED_RESPONSE_REPAIR",
    "IMAGE_CONTEXT_EDIT",
    "IMAGE_GENERATION_REQUEST",
    "IMAGE_ANALYSIS_REQUEST",
    "CLARIFICATION_NEEDED",
    "REACTION_ACK",
    "DIRECT_REQUEST",
}


SUPPORTED_TRANSLATE_LANGUAGES: Final[tuple[str, ...]] = (
    "english",
    "spanish",
    "german",
    "japanese",
    "russian",
    "french",
    "italian",
    "portuguese",
)

TRANSLATE_LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "english": "english",
    "en": "english",
    "ingles": "english",
    "spanish": "spanish",
    "es": "spanish",
    "espanol": "spanish",
    "german": "german",
    "de": "german",
    "aleman": "german",
    "japanese": "japanese",
    "ja": "japanese",
    "japones": "japanese",
    "russian": "russian",
    "ru": "russian",
    "ruso": "russian",
    "french": "french",
    "fr": "french",
    "frances": "french",
    "italian": "italian",
    "it": "italian",
    "italiano": "italian",
    "portuguese": "portuguese",
    "pt": "portuguese",
    "portugues": "portuguese",
}

COMMAND_HINTS: Final[dict[str, str]] = {
    "help": "help",
    "setup": "setup",
    "setservercontext": "setservercontext",
    "resetservercontext": "resetservercontext",
    "viewservercontext": "viewservercontext",
    "setprefix": "setprefix",
    "prefix": "setprefix",
    "language": "language",
    "code": "code",
    "codelangs": "codelangs",
    "runlangs": "codelangs",
    "srvstatus": "srvstatus",
    "delete": "message delete",
    "mute": "user mute",
    "unmute": "user unmute",
    "kick": "user kick",
    "ban": "user ban",
    "unban": "user unban",
    "tempmute": "user tempmute",
    "tempban": "user tempban",
    "warn": "user warn",
    "unwarn": "user unwarn",
    "warnings": "user warnings",
    "clearwarnings": "user clearwarnings",
    "userinfo": "user info",
    "setnick": "user setnick",
    "channeladd": "channel add",
    "channeldel": "channel delete",
    "clonechannel": "channel clone",
    "clearchannel": "channel clear",
    "channel": "channel add",
    "lock": "channel lock",
    "unlock": "channel unlock",
    "slowmode": "channel slowmode",
    "purgeuser": "message purgeuser",
    "roleadd": "role add",
    "roleremove": "role remove",
    "createrole": "role create",
    "colorsetup": "color setup",
    "colors": "color list",
    "colorchannel": "color channel",
    "colorreload": "color reload",
    "coloradd": "color add",
    "colorremove": "color remove",
    "translate": "translate",
    "roast": "roast",
    "roastme": "roastme",
    "joke": "joke",
    "dadjoke": "dadjoke",
    "advice": "advice",
    "whois": "whois",
    "convert": "convert",
    "remindme": "remindme",
    "unremindme": "unremindme",
    "meme": "meme create",
    "memerandom": "meme random",
    "memecustom": "meme custom",
    "memetemplates": "meme templates",
    "memefonts": "meme fonts",
    "memehelp": "meme help",
    "speech": "speech",
    "football": "football live ligamx",
    "footballlive": "football live ligamx",
    "footballtoday": "football today ligamx",
    "footballnext": "football next ligamx",
    "footballlast": "football last ligamx",
    "footballtable": "football table ligamx",
    "footballteam": "football team ligamx",
    "footballscorers": "football scorers ligamx",
    "ligamx": "football live ligamx",
    "premier": "football live premier",
    "laliga": "football live laliga",
    "concacaf": "football live concacaf",
    "ligamxlive": "football live ligamx",
    "ligamxtoday": "football today ligamx",
    "ligamxnext": "football next ligamx",
    "ligamxlast": "football last ligamx",
    "ligamxtable": "football table ligamx",
    "ligamxteam": "football team ligamx",
    "ligamxscorers": "football scorers ligamx",
}


class AIChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int, int], float] = {}
        self._cooldown_seconds = 4.0
        self._continuation_lease_seconds = CONTINUATION_LEASE_SECONDS
        self._continuation_leases: dict[tuple[int, int], ContinuationLease] = {}
        self._pending_interactions: dict[tuple[int, int, int, int | None], PendingInteraction] = {}
        self._pending_versions: dict[tuple[int, int, int, int | None], int] = {}
        self._missed_response_ttl_seconds = 90.0
        self._missed_response_candidates: dict[tuple[int, int, int], MissedResponseCandidate] = {}
        self._ambiguous_routing_shadow_mode = False
        self._football_live_watches: dict[tuple[int, int], FootballLiveWatch] = {}
        self._football_live_watch_poll_seconds = 30.0
        self._football_live_watch_max_seconds = 7200.0
        self._channel_human_activity: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(lambda: deque(maxlen=80))
        self._channel_ai_activity: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=40))
        self._channel_noisy_until: dict[tuple[int, int], float] = {}
        self._channel_noise_window_seconds = 20.0
        self._channel_noise_decay_seconds = 20.0
        self._reaction_cooldown_seconds = 45.0
        self._reaction_cooldowns: dict[tuple[int, int], float] = {}
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
        settings = getattr(self.bot, "settings", None)
        self._web_research = getattr(self.bot, "web_research_service", None) or WebResearchService(
            getattr(self.bot, "llm_client", None),
            enabled=bool(getattr(settings, "xai_web_search_enabled", False)),
            x_search_enabled=bool(getattr(settings, "xai_x_search_enabled", False)),
            max_sources=int(getattr(settings, "xai_web_search_max_sources", 3) or 3),
            cooldown_seconds=float(getattr(settings, "xai_web_search_cooldown_seconds", 30.0) or 30.0),
        )
        self._history_entry_max_chars = 900
        self._chat_response_id_limit = 600
        self._chat_response_ids: deque[int] = deque(maxlen=self._chat_response_id_limit)
        self._chat_response_id_set: set[int] = set()

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
        self._invalidate_missed_response_for_intervening_human(message)

        settings = await self.bot.db.get_or_create_guild_settings(message.guild.id)
        prefix = settings.prefix
        lang = settings.language_code

        if await self._handle_mention_translate(message, lang):
            self._invalidate_owner_lease_for_rejection(message, "mention_translate")
            return

        channel_allowed = await self._is_ai_allowed_in_channel(
            message.guild,
            message.channel,
        )
        if not channel_allowed:
            self._invalidate_owner_lease_for_rejection(message, "channel_not_allowed")
            return

        replied_message = await self._get_replied_message(message)

        if self._is_slash_command_response_message(replied_message):
            self._invalidate_owner_lease_for_rejection(message, "slash_output_reply")
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
        if self.bot.user in message.mentions:
            hinted_command = self._detect_command_hint(user_prompt)
            if hinted_command:
                await message.reply(
                    tr(
                        lang,
                        f"Hey, there is a command for this! Use: `/{hinted_command}` or `{prefix}{hinted_command}`",
                        f"Oye, hay un comando para esto. Usa: `/{hinted_command}` o `{prefix}{hinted_command}`",
                    ),
                    mention_author=True,
                )
                self._invalidate_owner_lease_for_rejection(message, "command_hint")
                return
        convo_key = self._conversation_key(message.guild.id, message.channel.id)
        await self._ensure_history_loaded(convo_key, message.guild.id, message.channel.id)
        bot_name = (
            getattr(self.bot.user, "display_name", None)
            or getattr(self.bot.user, "name", None)
            or "Nitori"
        )
        image_context = self._build_chat_image_context(message, replied_message)
        image_urls = image_context.urls
        if image_urls:
            hinted_command = self._detect_command_hint(user_prompt)
            if hinted_command and hinted_command.startswith("meme"):
                await message.reply(
                    tr(
                        lang,
                        f"Hey, there is a command for this! Use: `/{hinted_command}` or `{prefix}{hinted_command}`",
                        f"Oye, hay un comando para esto. Usa: `/{hinted_command}` o `{prefix}{hinted_command}`",
                    ),
                    mention_author=True,
                )
                self._invalidate_owner_lease_for_rejection(message, "meme_hint")
                return
        local_reaction = self._local_reaction_only_decision(message, self._serialize_custom_emojis(message.guild))
        if local_reaction is not None and anchor_type in STRONG_ANCHORS | {"PENDING_FOLLOWUP", "SAME_USER_CONTINUATION", "MISSED_RESPONSE_REPAIR"}:
            logging.info(
                "AI local reaction precheck guild=%s channel=%s message=%s local_no_text_reaction_precheck=true emoji=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                self._anon_id(local_reaction.emoji or ""),
            )
            if pending_context:
                self._cancel_pending_interaction(pending_interaction, event="reaction_precheck")
            await self._execute_reaction_action(
                message,
                local_reaction,
                image_context=image_context,
                replied_message=replied_message,
                pending_interaction=pending_interaction,
            )
            self._clear_missed_response_candidate(message, "reaction_precheck")
            return
        route_decision = await self._route_ai_decision(
            message=message,
            anchor_type=anchor_type,
            bot_name=str(bot_name),
            image_context=image_context,
            replied_message=replied_message,
            pending_interaction=pending_interaction,
            available_emojis=self._serialize_custom_emojis(message.guild),
        )
        self._log_route_decision(message, anchor_type, route_decision)
        if anchor_type in AMBIGUOUS_ANCHORS and self._ambiguous_routing_shadow_mode:
            self._log_shadow_route_decision(message, anchor_type, route_decision)
            self._invalidate_owner_lease_for_rejection(message, "shadow_mode")
            return
        if not self._route_allows_response(anchor_type, route_decision):
            self._store_missed_response_candidate(message, anchor_type, route_decision.failure_reason or route_decision.reason_code)
            self._invalidate_owner_lease_for_rejection(message, "route_ignore")
            return
        if route_decision.action == "CANCEL_PENDING":
            self._cancel_pending_interaction(pending_interaction, event="cancel_request")
            return
        if route_decision.action == "MODIFY_PENDING" and pending_interaction is not None:
            self._cancel_pending_interaction(pending_interaction, event="modify_request")
            if route_decision.emoji or route_decision.emojis:
                await self._execute_reaction_action(
                    message,
                    route_decision,
                    image_context=image_context,
                    replied_message=replied_message,
                    pending_interaction=pending_interaction,
                )
            return
        if route_decision.participation == "REACT_ONLY" or route_decision.action in {"ADD_REACTION", "REACT_ONLY"}:
            if pending_context:
                self._cancel_pending_interaction(pending_interaction, event="reaction_only")
            await self._execute_reaction_action(
                message,
                route_decision,
                image_context=image_context,
                replied_message=replied_message,
                pending_interaction=pending_interaction,
            )
            return
        if route_decision.action.startswith("SERVER_MEMORY_"):
            if self._server_memory is not None:
                await self._execute_server_memory_action(
                    message,
                    route_decision,
                    lang=lang,
                    replied_message=replied_message,
                    reply_to_trigger=self._should_reply_to_trigger(
                        message,
                        is_direct_trigger=anchor_type in STRONG_ANCHORS,
                    ),
                )
            return
        if route_decision.action == "WEB_LOOKUP":
            await self._execute_web_lookup(
                message,
                route_decision,
                settings=settings,
                lang=lang,
                user_prompt=user_prompt,
                reply_to_trigger=self._should_reply_to_trigger(
                    message,
                    is_direct_trigger=anchor_type in STRONG_ANCHORS,
                ),
            )
            return
        if route_decision.action in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP"}:
            await self._execute_football_live_watch_action(
                message,
                route_decision,
                lang=lang,
                user_prompt=user_prompt,
                reply_to_trigger=self._should_reply_to_trigger(
                    message,
                    is_direct_trigger=anchor_type in STRONG_ANCHORS,
                ),
            )
            return
        if route_decision.action.startswith("FOOTBALL_"):
            await self._execute_football_action(
                message,
                route_decision,
                settings=settings,
                lang=lang,
                user_prompt=user_prompt,
                reply_to_trigger=self._should_reply_to_trigger(
                    message,
                    is_direct_trigger=anchor_type in STRONG_ANCHORS,
                ),
                create_lease=anchor_type != "REPLY_TO_WATCH",
            )
            return
        if route_decision.action == "CLARIFY":
            clarify_text = self._route_clarification_text(route_decision, lang)
            pending_for_send = self._set_pending_interaction(
                message,
                route_decision=route_decision,
                target_message_id=self._route_target_message_id(message, route_decision, image_context, replied_message),
            )
            if not self._pending_can_send(pending_for_send):
                return
            sent_message_id = await self._send_long_reply(
                message,
                clarify_text,
                reply_to_trigger=self._should_reply_to_trigger(
                    message,
                    is_direct_trigger=anchor_type in STRONG_ANCHORS,
                ),
                mention_author=True,
            )
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
                    action_type="CLARIFY",
                    resolved_request=route_decision.resolved_request,
                )
            bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
            prefixed_bot = self._append_conversation_turn(
                convo_key,
                role="assistant",
                speaker=bot_speaker,
                content=clarify_text,
            )
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
                    action_type="CLARIFY",
                    resolved_request=route_decision.resolved_request,
                )
            self._create_or_renew_lease(
                message,
                last_bot_response_id=sent_message_id,
                action="CLARIFY",
                resolved_request=route_decision.resolved_request,
            )
            self._clear_pending_interaction(pending_for_send, event="success")
            return
        if route_decision.action == "GENERATE_IMAGE":
            pending_for_send = self._set_pending_interaction(
                message,
                route_decision=route_decision,
                target_message_id=self._route_target_message_id(message, route_decision, image_context, replied_message),
            )
            await self._handle_image_generation_request(
                message,
                route_decision.resolved_request or user_prompt,
                lang=lang,
                parent_message_id=getattr(message, "id", None),
                resolved_request=route_decision.resolved_request,
                pending=pending_for_send,
            )
            return
        if route_decision.action == "EDIT_IMAGE":
            source_url = self._image_edit_source_url(image_context)
            if not source_url:
                if not image_context.prior_branch_images:
                    logging.info(
                        "AI image edit source missing guild=%s channel=%s message=%s prior_branch_image_unavailable=true",
                        getattr(message.guild, "id", None),
                        getattr(message.channel, "id", None),
                        getattr(message, "id", None),
                    )
                await self._send_long_reply(
                    message,
                    tr(
                        lang,
                        "Reply to the message with the image or attach it so I can edit it.",
                        "Respondeme al mensaje con la imagen o adjuntala para poder editarla.",
                    ),
                    reply_to_trigger=self._should_reply_to_trigger(
                        message,
                        is_direct_trigger=anchor_type in STRONG_ANCHORS,
                    ),
                    mention_author=True,
                )
                self._invalidate_owner_lease_for_rejection(message, "missing_edit_image")
                return
            pending_for_send = self._set_pending_interaction(
                message,
                route_decision=route_decision,
                target_message_id=image_context.source_message_id
                or self._route_target_message_id(message, route_decision, image_context, replied_message),
            )
            await self._handle_image_edit_request(
                message,
                route_decision.resolved_request or user_prompt,
                source_url,
                lang=lang,
                parent_message_id=getattr(message, "id", None),
                resolved_request=route_decision.resolved_request,
                source_kind=image_context.preferred_source_kind,
                pending=pending_for_send,
            )
            return
        if route_decision.action == "ANALYZE_IMAGE" and not image_urls:
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "Send or reply to a supported image first, then ask me to analyze it.",
                    "Manda o responde a una imagen compatible primero, y luego pideme analizarla.",
                ),
                reply_to_trigger=self._should_reply_to_trigger(
                    message,
                    is_direct_trigger=anchor_type in STRONG_ANCHORS,
                ),
                mention_author=True,
            )
            self._invalidate_owner_lease_for_rejection(message, "missing_image")
            return

        replied_context = (
            self._build_replied_message_context(replied_message)
            if anchor_type in STRONG_ANCHORS and replied_message is not None
            else RepliedMessageContext()
        )
        llm_prompt = self._apply_replied_message_context(user_prompt, replied_context)
        llm_prompt = self._apply_image_context_note(llm_prompt, image_context)
        llm_prompt = self._apply_resolved_request_context(llm_prompt, route_decision)
        available_emojis = self._serialize_custom_emojis(message.guild)
        if (
            replied_message is not None
            and self.bot.user is not None
            and replied_message.author.id == self.bot.user.id
            and is_reply_to_ai
            and replied_message.content.strip()
        ):
            cleaned_replied = " ".join(replied_message.content.strip().split())
            if len(cleaned_replied) > self._history_entry_max_chars:
                cleaned_replied = cleaned_replied[: self._history_entry_max_chars - 3].rstrip() + "..."
            expected_prefixed = f"{self.bot.user.display_name}: {cleaned_replied}"
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

        relay_context = await self._resolve_relay_target_from_prompt(
            guild=message.guild,
            author=message.author,
            prompt=user_prompt,
            lang=lang,
        )
        conversation_mode = self._conversation_mode_for_trigger(
            replied_message,
            is_active_followup=anchor_type == "SAME_USER_CONTINUATION",
            is_reply_to_ai=is_reply_to_ai,
        )
        if anchor_type == "SAME_USER_CONTINUATION":
            conversation_mode = "continuation"

        pending_for_send = self._set_pending_interaction(
            message,
            route_decision=route_decision,
            target_message_id=self._route_target_message_id(message, route_decision, image_context, replied_message),
        )
        enhanced_server_context = await self._server_context_with_memory(
            message,
            base_context=settings.server_context,
            route_action=route_decision.action,
            current_text=user_prompt,
            replied_message=replied_message,
        )
        await message.channel.typing()
        reply_to_trigger = self._should_reply_to_trigger(
            message,
            is_direct_trigger=anchor_type in STRONG_ANCHORS,
        )
        try:
            reply = await self.bot.llm_client.chat(
                server_context=enhanced_server_context,
                user_message=llm_prompt,
                author_name=message.author.display_name,
                channel_name=getattr(message.channel, "name", "unknown"),
                channel_reference=self._channel_reference(message.channel),
                available_channels=self._serialize_text_channels(message.guild),
                available_emojis=available_emojis,
                conversation_history=await self._build_branch_conversation_history(
                    convo_key,
                    message,
                    replied_message,
                    lease=self._continuation_leases.get(lease_key),
                    is_reply_to_ai=is_reply_to_ai,
                ),
                mention_hints=self._build_mention_hints(relay_context),
                relay_instruction=self._build_relay_instruction(relay_context, lang),
                is_owner=self.bot.is_owner_user(message.author),
                conversation_mode=conversation_mode,
                image_urls=image_urls,
            )
        except Exception as exc:
            logging.exception("AI chat failure in guild=%s channel=%s", message.guild.id, message.channel.id)
            if not self._pending_can_send(pending_for_send):
                return
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
                        "I can see you attached an image, but this xAI model/API access is not enabled for image understanding. Ask an admin to set `XAI_VISION_MODEL` to a vision-capable model and confirm image input access.",
                        "Veo que adjuntaste una imagen, pero este modelo/acceso de xAI no esta habilitado para entender imagenes. Pide a un admin configurar `XAI_VISION_MODEL` con un modelo con vision y revisar el acceso a imagenes.",
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
            self._store_missed_response_candidate(message, anchor_type, "chat_error")
            self._clear_pending_interaction(pending_for_send, event="error")
            return

        if not self._pending_can_send(pending_for_send):
            self._log_pending_event(message, "stale_suppressed", pending_for_send)
            return
        reply = await self._normalize_discord_references(reply, message.guild)
        reply = self._apply_relay_postprocess(reply, relay_context)
        reply = self._sanitize_visible_ai_output(reply)
        reply = self._dearm_mass_mentions(reply)
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
                action_type=route_decision.action,
                resolved_request=route_decision.resolved_request,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        prefixed_bot = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=reply,
        )
        sent_message_id = await self._send_long_reply(
            message,
            reply,
            reply_to_trigger=reply_to_trigger,
            mention_author=True,
        )
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
                action_type=route_decision.action,
                resolved_request=route_decision.resolved_request,
            )
        if anchor_type != "REPLY_TO_WATCH":
            self._create_or_renew_lease(
                message,
                last_bot_response_id=sent_message_id,
                action=route_decision.action,
                resolved_request=route_decision.resolved_request,
            )
        self._clear_missed_response_candidate(message, "success")
        self._clear_pending_interaction(pending_for_send, event="success")
        await self._maybe_add_ai_reaction(
            target=image_context.reaction_target,
            reaction_key=convo_key,
            user_prompt=user_prompt,
            assistant_reply=reply,
            channel_name=getattr(message.channel, "name", "unknown"),
            available_emojis=available_emojis,
            conversation_mode=conversation_mode,
        )

    @commands.hybrid_command(
        name="roast",
        description="Generate a playful roast using the server vibe context.",
    )
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
            target_member=ctx.author,  # type: ignore[arg-type]
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

        reply = await self._normalize_discord_references(reply, guild)
        reply = self._sanitize_visible_ai_output(reply)
        reply = self._dearm_mass_mentions(reply)
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
        # Strip common speaker prefixes the model may prepend.
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
            # If the model duplicated the mention at start, reduce to one.
            doubled = f"{target_mention} {target_mention}"
            if cleaned.startswith(doubled):
                return f"{target_mention} {cleaned[len(doubled):].lstrip()}"
            return cleaned
        return f"{target_mention} {cleaned}"

    @staticmethod
    def _member_name_variants(member: discord.Member) -> list[str]:
        variants: list[str] = []
        if member.name:
            variants.append(member.name)
        if member.display_name:
            variants.append(member.display_name)
        global_name = getattr(member, "global_name", None)
        if isinstance(global_name, str) and global_name:
            variants.append(global_name)
        discriminator = getattr(member, "discriminator", None)
        if isinstance(discriminator, str) and discriminator not in {"", "0"}:
            variants.append(f"{member.name}#{discriminator}")
        return variants

    async def _resolve_relay_target_from_prompt(
        self,
        *,
        guild: discord.Guild,
        author: discord.Member,
        prompt: str,
        lang: str,
    ) -> dict[str, object] | None:
        lowered = prompt.casefold()
        relay_markers = (
            "tell ",
            "let ",
            "notify ",
            "remind ",
            "ask ",
            "ping ",
            "message ",
            "dile ",
            "avisale",
            "avísale",
            "notifica",
            "recuerdale",
            "recuérdale",
            "preguntale",
            "pregúntale",
            "avisa ",
        )
        if not any(marker in lowered for marker in relay_markers):
            return None

        if self.bot.user is not None:
            for raw_id in re.findall(r"<@!?(\d{15,22})>", prompt):
                try:
                    user_id = int(raw_id)
                except ValueError:
                    continue
                if user_id in {author.id, self.bot.user.id}:
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                if member is not None:
                    return self._build_relay_context(author, member, raw_query=member.display_name)

        for query in self._extract_relay_name_candidates(prompt):
            member, _ = await self._resolve_target_member(guild, query, lang)
            if member is None:
                continue
            if member.id == author.id:
                continue
            if self.bot.user is not None and member.id == self.bot.user.id:
                continue
            return self._build_relay_context(author, member, raw_query=query)
        return None

    @staticmethod
    def _extract_relay_name_candidates(prompt: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            cleaned = " ".join(value.strip().split())
            if not cleaned:
                return
            key = cleaned.casefold()
            if key in seen:
                return
            seen.add(key)
            candidates.append(cleaned)

        patterns = (
            r"\b(?:tell|let|notify|remind|ask|ping|message)\s+([@A-Za-z0-9_.\-]{2,32})(?:\s+(?:know|that|to)\b|[^\w]|$)",
            r"\b(?:dile|avisale|avísale|notifica(?:le)?|recuerdale|recuérdale|preguntale|pregúntale)\s+a?\s*([@A-Za-z0-9_.\-]{2,32})(?:\s+que\b|[^\w]|$)",
            r"\b(?:avisa|avisar)\s+a?\s*([@A-Za-z0-9_.\-]{2,32})(?:\s+que\b|[^\w]|$)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, prompt, flags=re.IGNORECASE):
                push(match.group(1).lstrip("@"))

        for at_name in re.findall(r"(?<!<)@([A-Za-z0-9_.\-]{2,64})", prompt):
            push(at_name)

        # Also consider quoted names.
        for quoted in re.findall(r"[\"'“”‘’]([A-Za-z0-9_.\-]{2,64})[\"'“”‘’]", prompt):
            push(quoted)

        return candidates[:10]

    def _build_relay_context(
        self,
        requester: discord.Member,
        target: discord.Member,
        *,
        raw_query: str,
    ) -> dict[str, object]:
        aliases = [target.display_name, target.name]
        global_name = getattr(target, "global_name", None)
        if isinstance(global_name, str) and global_name:
            aliases.append(global_name)
        aliases.append(raw_query)
        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            clean = alias.strip()
            if not clean:
                continue
            key = clean.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(clean)
        return {
            "target_id": target.id,
            "target_mention": target.mention,
            "target_names": deduped,
            "requester_id": requester.id,
            "requester_mention": requester.mention,
            "requester_name": requester.display_name,
            "raw_query": raw_query.strip(),
        }

    @staticmethod
    def _build_mention_hints(relay_context: dict[str, object] | None) -> list[str] | None:
        if not relay_context:
            return None
        target_mention = str(relay_context.get("target_mention", "")).strip()
        raw_query = str(relay_context.get("raw_query", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        requester_name = str(relay_context.get("requester_name", "")).strip()
        hints: list[str] = []
        if raw_query and target_mention:
            hints.append(f"{raw_query} -> {target_mention}")
        names = relay_context.get("target_names")
        if isinstance(names, list):
            for name in names[:3]:
                if isinstance(name, str) and name.strip() and target_mention:
                    hints.append(f"{name.strip()} -> {target_mention}")
        if requester_mention and requester_name:
            hints.append(f"requester {requester_name} -> {requester_mention}")
        return hints or None

    @staticmethod
    def _build_relay_instruction(
        relay_context: dict[str, object] | None,
        lang: str,
    ) -> str | None:
        if not relay_context:
            return None
        target_mention = str(relay_context.get("target_mention", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        if not target_mention or not requester_mention:
            return None
        return tr(
            lang,
            (
                f"Resolved relay target: {target_mention}. "
                f"Requester: {requester_mention}. "
                "Deliver the message to the target. Mention target once. "
                "Do not ping requester unless explicitly asked."
            ),
            (
                f"Objetivo de relevo resuelto: {target_mention}. "
                f"Solicitante: {requester_mention}. "
                "Entrega el mensaje al objetivo. Menciona al objetivo una sola vez. "
                "No etiquetes al solicitante a menos que te lo pidan explícitamente."
            ),
        )

    def _apply_relay_postprocess(
        self,
        text: str,
        relay_context: dict[str, object] | None,
    ) -> str:
        if not relay_context:
            return text

        target_mention = str(relay_context.get("target_mention", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        requester_name = str(relay_context.get("requester_name", "")).strip()
        target_names = relay_context.get("target_names")
        if not target_mention:
            return text

        updated = text

        # Fix malformed mentions containing the target alias.
        if isinstance(target_names, list):
            for alias in target_names:
                if not isinstance(alias, str):
                    continue
                alias_clean = alias.strip()
                if len(alias_clean) < 3:
                    continue
                malformed = re.compile(
                    rf"<@!?[^>]*{re.escape(alias_clean)}[^>]*>",
                    flags=re.IGNORECASE,
                )
                updated = malformed.sub(target_mention, updated)

        # Avoid pinging requester in relay replies unless explicitly requested.
        if requester_mention and requester_name and requester_mention in updated:
            updated = updated.replace(requester_mention, requester_name)

        # Ensure target is explicitly mentioned at least once.
        if target_mention not in updated and isinstance(target_names, list):
            for alias in target_names:
                if not isinstance(alias, str):
                    continue
                alias_clean = alias.strip()
                if len(alias_clean) < 3:
                    continue
                plain_alias = alias_clean.lstrip("@")
                name_re = re.compile(
                    rf"(?<![\w<@])@?{re.escape(plain_alias)}(?![\w>])",
                    flags=re.IGNORECASE,
                )
                updated, count = name_re.subn(target_mention, updated, count=1)
                if count:
                    break
        if target_mention not in updated:
            updated = f"{target_mention} {updated}".strip()

        updated = self._dedupe_repeated_mentions(updated)
        return updated

    async def _resolve_target_member(
        self,
        guild: discord.Guild,
        user_query: str,
        lang: str,
    ) -> tuple[discord.Member | None, str | None]:
        query = user_query.strip()
        if not query:
            return None, tr(
                lang,
                "Provide a user mention, ID, username, or display name.",
                "Proporciona una mencion, ID, nombre de usuario o nombre visible.",
            )

        user_id = parse_user_id_from_text(query)
        if user_id is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member, None
            try:
                member = await guild.fetch_member(user_id)
                return member, None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None, tr(
                    lang,
                    "I couldn't find a server member with that ID.",
                    "No pude encontrar un miembro del servidor con esa ID.",
                )

        query_cf = query.casefold()

        def matches_exact(member: discord.Member) -> bool:
            return any(name.casefold() == query_cf for name in self._member_name_variants(member))

        def matches_prefix(member: discord.Member) -> bool:
            return any(name.casefold().startswith(query_cf) for name in self._member_name_variants(member))

        exact_cached = [m for m in guild.members if matches_exact(m)]
        if len(exact_cached) == 1:
            return exact_cached[0], None
        if len(exact_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        prefix_cached = [m for m in guild.members if matches_prefix(m)]
        if len(prefix_cached) == 1:
            return prefix_cached[0], None
        if len(prefix_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        try:
            queried = await guild.query_members(query=query, limit=25)
        except (discord.Forbidden, discord.HTTPException):
            queried = []

        exact_queried = [m for m in queried if matches_exact(m)]
        if len(exact_queried) == 1:
            return exact_queried[0], None
        if len(exact_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        prefix_queried = [m for m in queried if matches_prefix(m)]
        if len(prefix_queried) == 1:
            return prefix_queried[0], None
        if len(prefix_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        return None, tr(
            lang,
            "User not found in this server. Use @mention, user ID, username, or display name.",
            "No encontre al usuario en este servidor. Usa @mencion, ID, nombre de usuario o nombre visible.",
        )

    @commands.hybrid_command(
        name="translate",
        description="Translate replied text or provided text.",
    )
    async def translate_cmd(
        self,
        ctx: commands.Context,
        language: str,
        *,
        text: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        target = self._normalize_target_language(language)
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

        if self._is_same_language(source_text, target):
            await ctx.send(
                tr(
                    lang,
                    "The original text it's already on that language!",
                    "El mensaje original ya estaba en ese idioma!",
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

        translated = await self._normalize_discord_references(translated, ctx.guild)
        await ctx.send(translated[:1900], allowed_mentions=discord.AllowedMentions.none())

    async def _handle_mention_translate(self, message: discord.Message, lang: str) -> bool:
        if self.bot.user is None:
            return False
        if self.bot.user not in message.mentions:
            return False

        original = await self._get_replied_message(message)
        if original is None:
            return False
        if self.bot.user and original.author.id == self.bot.user.id:
            return False

        content_wo_mention = self._remove_bot_mentions(message.content, self.bot.user.id)
        if not content_wo_mention:
            return False

        raw = content_wo_mention.strip()
        lowered = raw.lower()

        language_input: str | None = None
        explicit_translate = lowered.startswith("translate")
        if explicit_translate:
            language_input = raw[9:].strip()
            if language_input.startswith(":"):
                language_input = language_input[1:].strip()
            if not language_input:
                await message.reply(
                    self._translate_usage_message(lang),
                    mention_author=True,
                )
                return True
        else:
            # Shortcut mode only accepts a single token language name.
            if " " in raw:
                return False
            language_input = raw.strip(".,!?;:()[]{}\"'`")
            if not language_input:
                return False

        if not language_input:
            return False

        target = self._normalize_target_language(language_input)
        if not target:
            if explicit_translate:
                await message.reply(
                    self._translate_usage_message(lang),
                    mention_author=True,
                )
                return True
            return False

        await self._run_translation(message, original, target, lang)
        return True

    async def _run_translation(
        self,
        trigger_message: discord.Message,
        original: discord.Message,
        language: str,
        lang: str,
    ) -> None:
        if not self._allowed_by_cooldown(trigger_message):
            return
        if not original.content.strip():
            await trigger_message.reply(
                "The replied message has no text to translate.",
                mention_author=True,
            )
            return
        if self._is_same_language(original.content, language):
            await trigger_message.reply(
                tr(
                    lang,
                    "The original text it's already on that language!",
                    "El mensaje original ya estaba en ese idioma!",
                ),
                mention_author=True,
            )
            return

        await trigger_message.channel.typing()
        try:
            translated = await self.bot.llm_client.translate(
                text=original.content,
                target_language=language,
            )
        except Exception as exc:
            logging.exception(
                "AI mention-translate failure in guild=%s channel=%s",
                trigger_message.guild.id if trigger_message.guild else None,
                trigger_message.channel.id,
            )
            if self._is_ai_limit_error(exc):
                await trigger_message.reply(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                    ),
                    mention_author=True,
                )
            elif self._is_empty_completion_error(exc):
                await trigger_message.reply(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    ),
                    mention_author=True,
                )
            else:
                await trigger_message.reply(
                    tr(
                        lang,
                        "I hit an internal AI error while translating. Please try again.",
                        "Tuve un error interno de IA al traducir. Intenta de nuevo.",
                    ),
                    mention_author=True,
                )
            return

        translated = await self._normalize_discord_references(translated, trigger_message.guild)
        translated = self._sanitize_visible_ai_output(translated)
        translated = self._dearm_mass_mentions(translated)
        await trigger_message.reply(translated[:1900], mention_author=True)

    async def _is_chat_trigger(
        self,
        message: discord.Message,
        replied_message: discord.Message | None = None,
        *,
        is_reply_to_ai: bool | None = None,
    ) -> bool:
        if self.bot.user is None:
            return False

        if self.bot.user in message.mentions:
            stripped = self._remove_bot_mentions(message.content, self.bot.user.id)
            return bool(stripped)
        if self._is_addressed_to_bot_by_name(message.content):
            return True

        ref_msg = replied_message
        if ref_msg is None:
            ref_msg = await self._get_replied_message(message)
        if is_reply_to_ai is None:
            is_reply_to_ai = await self._is_reply_to_ai_message(message, ref_msg)
        return bool(
            ref_msg
            and self.bot.user
            and ref_msg.author.id == self.bot.user.id
            and is_reply_to_ai
        )

    def _is_slash_command_response_message(self, message: discord.Message | None) -> bool:
        if message is None:
            return False
        if getattr(message, "interaction", None) or getattr(message, "interaction_metadata", None):
            return True
        application_id = getattr(message, "application_id", None)
        bot_application_id = getattr(self.bot, "application_id", None)
        if application_id is None or bot_application_id is None:
            return False
        try:
            return int(application_id) == int(bot_application_id)
        except (TypeError, ValueError):
            return application_id == bot_application_id

    async def _is_reply_to_ai_message(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> bool:
        if self.bot.user is None or replied_message is None:
            return False
        if replied_message.author.id != self.bot.user.id:
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
        if self.bot.user in message.mentions:
            stripped = self._remove_bot_mentions(message.content, self.bot.user.id)
            if stripped and self._is_missed_response_repair_message(message) and self._valid_missed_response_candidate(message) is not None:
                return "MISSED_RESPONSE_REPAIR"
            return "DIRECT_MENTION" if stripped else "NONE"
        if is_reply_to_ai:
            if self._is_missed_response_repair_message(message) and self._valid_missed_response_candidate(message) is not None:
                return "MISSED_RESPONSE_REPAIR"
            return "REPLY_TO_AI"
        if self._is_reply_to_watch_message(message) and self._watch_reply_has_meaningful_request(message):
            return "REPLY_TO_WATCH"
        if self._is_addressed_to_bot_by_name(message.content):
            if self._is_missed_response_repair_message(message) and self._valid_missed_response_candidate(message) is not None:
                return "MISSED_RESPONSE_REPAIR"
            return "NAME_AT_START"
        if await self._is_same_user_continuation_candidate(
            message,
            prefix=prefix,
            settings=settings,
            is_command_context_message=is_command_context_message,
        ):
            if self._is_missed_response_repair_message(message) and self._valid_missed_response_candidate(message) is not None:
                return "MISSED_RESPONSE_REPAIR"
            return "SAME_USER_CONTINUATION"
        if self._alias_reference_match(message.content):
            if self._is_missed_response_repair_message(message) and self._valid_missed_response_candidate(message) is not None:
                return "MISSED_RESPONSE_REPAIR"
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
        if self._is_noise_only_followup(content):
            logging.info(
                "AI continuation rejected guild=%s channel=%s message=%s author=%s continuation_rejected_reason=noise",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message, "id", None),
                getattr(message.author, "id", None),
            )
            return False
        return not await self._is_passive_ai_blocked_channel(message.guild, message.channel, settings)

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

    def _watch_reply_has_meaningful_request(self, message: discord.Message) -> bool:
        content = self._remove_bot_mentions(message.content, self.bot.user.id) if self.bot.user else message.content
        cleaned = " ".join(str(content or "").split())
        if not cleaned or self._is_noise_only_followup(cleaned):
            return False
        lowered = cleaned.casefold()
        if "?" in cleaned:
            return True
        request_markers = (
            "quien",
            "quién",
            "que",
            "qué",
            "como",
            "cómo",
            "donde",
            "dónde",
            "cuando",
            "cuándo",
            "por que",
            "por qué",
            "gol",
            "goles",
            "marcador",
            "resultado",
            "minuto",
            "roja",
            "amarilla",
            "cambio",
            "estadistica",
            "stats",
            "posesion",
            "possession",
            "explica",
            "resume",
            "dime",
            "di",
        )
        return any(marker in lowered for marker in request_markers)

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

    def _missed_response_key(self, message: discord.Message) -> tuple[int, int, int]:
        return (
            int(message.guild.id),  # type: ignore[union-attr]
            int(message.channel.id),  # type: ignore[union-attr]
            int(message.author.id),
        )

    def _valid_missed_response_candidate(self, message: discord.Message) -> MissedResponseCandidate | None:
        key = self._missed_response_key(message)
        candidate = self._missed_response_candidates.get(key)
        if candidate is None:
            return None
        if time.monotonic() - candidate.created_at > self._missed_response_ttl_seconds:
            self._missed_response_candidates.pop(key, None)
            return None
        return candidate

    def _store_missed_response_candidate(self, message: discord.Message, anchor_type: str, reason: str) -> None:
        allowed = {"NAME_REFERENCE", "SAME_USER_CONTINUATION", "PENDING_FOLLOWUP"}
        strong_failure_reasons = {"api_exception", "http_error", "timeout", "invalid_json", "validation_exception", "chat_error"}
        if anchor_type not in allowed and anchor_type != "REPLY_TO_AI" and not (anchor_type in STRONG_ANCHORS and reason in strong_failure_reasons):
            return
        snippet = " ".join(str(getattr(message, "content", "") or "").split())[:220]
        if not snippet:
            return
        self._missed_response_candidates[self._missed_response_key(message)] = MissedResponseCandidate(
            guild_id=int(message.guild.id),  # type: ignore[union-attr]
            channel_id=int(message.channel.id),  # type: ignore[union-attr]
            author_user_id=int(message.author.id),
            message_id=getattr(message, "id", None),
            created_at=time.monotonic(),
            anchor_type=anchor_type,
            reason=reason,
            snippet=snippet,
            author_name=getattr(message.author, "display_name", "unknown"),
        )
        logging.info(
            "AI missed response candidate stored guild=%s channel=%s message=%s author=%s anchor=%s reason=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            getattr(message.author, "id", None),
            anchor_type,
            reason,
        )

    def _clear_missed_response_candidate(self, message: discord.Message, event: str) -> None:
        candidate = self._missed_response_candidates.pop(self._missed_response_key(message), None)
        if candidate is None:
            return
        logging.info(
            "AI missed response candidate cleared guild=%s channel=%s message=%s author=%s event=%s",
            candidate.guild_id,
            candidate.channel_id,
            candidate.message_id,
            candidate.author_user_id,
            event,
        )

    def _invalidate_missed_response_for_intervening_human(self, message: discord.Message) -> None:
        guild_id = int(message.guild.id)  # type: ignore[union-attr]
        channel_id = int(message.channel.id)  # type: ignore[union-attr]
        author_id = int(message.author.id)
        for key, candidate in list(self._missed_response_candidates.items()):
            if candidate.guild_id == guild_id and candidate.channel_id == channel_id and candidate.author_user_id != author_id:
                self._missed_response_candidates.pop(key, None)
                logging.info(
                    "AI missed response candidate cleared guild=%s channel=%s message=%s author=%s event=intervening_human",
                    candidate.guild_id,
                    candidate.channel_id,
                    candidate.message_id,
                    candidate.author_user_id,
                )

    def _is_missed_response_repair_message(self, message: discord.Message) -> bool:
        content = self._normalize_alias_text(str(getattr(message, "content", "") or ""))
        if not content:
            return False
        patterns = (
            "ignorame",
            "no me pelaste",
            "me ignoraste",
            "ya ni contestas",
            "ok no me respondas",
            "no me respondas",
            "no hay falla",
            "tons no",
            "entonces no",
            "me dejaste hablando solo",
        )
        return any(pattern in content for pattern in patterns)

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
    ) -> None:
        self._continuation_leases[self._message_channel_key(message)] = ContinuationLease(
            owner_user_id=int(message.author.id),
            last_user_message_id=getattr(message, "id", None),
            last_bot_response_id=last_bot_response_id,
            expires_at=time.monotonic() + self._continuation_lease_seconds,
            last_action=action,
            resolved_request=resolved_request,
        )

    def _pending_key(
        self,
        message: discord.Message,
        target_message_id: int | None = None,
    ) -> tuple[int, int, int, int | None]:
        return (
            int(message.guild.id),  # type: ignore[union-attr]
            int(message.channel.id),
            int(message.author.id),
            target_message_id,
        )

    def _pending_keys_for_message(self, message: discord.Message) -> list[tuple[int, int, int, int | None]]:
        guild_id = int(message.guild.id)  # type: ignore[union-attr]
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
            guild_id=int(message.guild.id),  # type: ignore[union-attr]
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
        if pending.guild_id != int(message.guild.id) or pending.channel_id != int(message.channel.id):  # type: ignore[union-attr]
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

    def _route_pending_metadata(self, pending: PendingInteraction | None) -> dict[str, object]:
        if pending is None:
            return {"active": False}
        return {
            "active": True,
            "owner_user_id": pending.owner_user_id,
            "target_message_id": pending.target_message_id,
            "action": pending.action,
            "version": pending.version,
            "resolved_request": pending.route_decision.resolved_request,
        }

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

    async def _execute_server_memory_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        lang: str,
        replied_message: discord.Message | None,
        reply_to_trigger: bool,
    ) -> None:
        guild = message.guild
        if guild is None or self._server_memory is None:
            return
        payload = decision.memory or {}
        if decision.action == "SERVER_MEMORY_CLARIFY" or not payload:
            await self._send_long_reply(
                message,
                tr(lang, "Tell me exactly what to remember, who it is about, and the value.", "Dime exactamente que debo recordar, de quien es y cual es el valor."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return
        try:
            memory_type = ServerMemoryService.normalize_memory_type(payload.get("memory_type", "SERVER_FACT"))
        except ValueError:
            await self._send_long_reply(
                message,
                tr(lang, "That memory type is not supported.", "Ese tipo de memoria no esta soportado."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return
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
            await self._send_long_reply(
                message,
                tr(lang, "I cannot change server behavior from that message.", "No puedo cambiar el comportamiento del servidor con ese mensaje."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return
        subject_user_id = self._memory_subject_user_id(message, replied_message, payload)
        subject_channel_id = self._memory_subject_channel_id(message, payload)
        key = payload.get("key") or self._default_memory_key(memory_type, subject_user_id, subject_channel_id)

        if decision.action == "SERVER_MEMORY_LOOKUP":
            rows = await self._server_memory.list_memories(
                guild.id,
                memory_type=memory_type,
                subject_user_id=subject_user_id,
                subject_channel_id=subject_channel_id,
                status="active",
                limit=10,
            )
            normalized_key = ServerMemoryService.normalize_key(key)
            rows = [row for row in rows if str(row.get("key")) == normalized_key] if normalized_key else rows
            if not rows:
                await self._send_long_reply(
                    message,
                    tr(lang, "I do not have that stored for this server.", "No tengo eso guardado para este servidor."),
                    reply_to_trigger=reply_to_trigger,
                    mention_author=True,
                )
                return
            await self._send_long_reply(
                message,
                self._format_memory_lookup(rows, lang),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        if decision.action in {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE"}:
            value = payload.get("value", "")
            if memory_type.startswith("USER_") and subject_user_id is None:
                await self._send_long_reply(
                    message,
                    tr(lang, "Mention the user this memory is about.", "Menciona al usuario de quien es esta memoria."),
                    reply_to_trigger=reply_to_trigger,
                    mention_author=True,
                )
                return
            if not value:
                await self._send_long_reply(
                    message,
                    tr(lang, "What value should I remember?", "Que valor debo recordar?"),
                    reply_to_trigger=reply_to_trigger,
                    mention_author=True,
                )
                return
            needs_approval = ServerMemoryService.should_require_approval(
                memory_type,
                value,
                created_for_other=bool(subject_user_id and subject_user_id != getattr(message.author, "id", None)),
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
            text = (
                tr(lang, f"I made this a pending memory proposal (`{row.get('id')}`). The target user or an admin can approve it.", f"Deje esto como propuesta pendiente (`{row.get('id')}`). El usuario objetivo o un admin puede aprobarla.")
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
            await self._send_long_reply(message, text, reply_to_trigger=reply_to_trigger, mention_author=True)
            return

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
                await self._send_long_reply(
                    message,
                    tr(lang, "I could not find that memory.", "No encontre esa memoria."),
                    reply_to_trigger=reply_to_trigger,
                    mention_author=True,
                )
                return
            await self._server_memory.archive_memory(guild.id, int(target["id"]))
            await self._send_long_reply(
                message,
                tr(lang, "Forgot that server memory.", "Olvide esa memoria del servidor."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )

    async def _server_context_with_memory(
        self,
        message: discord.Message,
        *,
        base_context: str,
        route_action: str,
        current_text: str,
        replied_message: discord.Message | None,
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
        replied_message: discord.Message | None,
        payload: dict[str, str],
    ) -> int | None:
        subject = str(payload.get("subject", "")).casefold()
        non_bot_mentions = [user for user in message.mentions if not getattr(user, "bot", False)]
        if non_bot_mentions:
            return int(non_bot_mentions[0].id)
        if subject in {"author", "self", "me", "yo"}:
            return int(message.author.id)
        if subject in {"replied_user", "reply_author"} and replied_message is not None:
            return int(replied_message.author.id)
        return None

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

    @staticmethod
    def _format_memory_lookup(rows: list[dict[str, object]], lang: str) -> str:
        lines = [tr(lang, "Stored server memories:", "Memorias guardadas del servidor:")]
        for row in rows[:10]:
            key = str(row.get("key", "memory")).replace("_", " ")
            lines.append(f"- `{int(row.get('id', 0))}` {row.get('memory_type')}: {key}: {row.get('value')}")
        return "\n".join(lines)

    async def _execute_web_lookup(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        settings: object,
        lang: str,
        user_prompt: str,
        reply_to_trigger: bool,
    ) -> None:
        query = " ".join(str(decision.resolved_request or user_prompt or "").split())[:500]
        request = WebResearchRequest(
            query=query,
            lookup_type=self._web_lookup_type_from_text(query),
            max_sources=int(getattr(getattr(self.bot, "settings", None), "xai_web_search_max_sources", 3) or 3),
        )
        result = await self._web_research.research(
            request,
            guild_id=getattr(message.guild, "id", None),
            user_id=getattr(message.author, "id", None),
        )
        web_context = format_web_research_context(result, max_sources=request.max_sources)
        if not web_context:
            await self._send_long_reply(
                message,
                tr(lang, "That current detail is not showing for me right now.", "Ese dato actual no me sale ahora mismo."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        grounded_prompt = web_grounding_prompt(query, web_context)
        try:
            enhanced_server_context = await self._server_context_with_memory(
                message,
                base_context=getattr(settings, "server_context", ""),
                route_action="WEB_LOOKUP",
                current_text=query,
                replied_message=None,
            )
            reply = await self.bot.llm_client.chat(
                server_context=enhanced_server_context,
                user_message=grounded_prompt,
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
                image_urls=[],
            )
        except Exception:
            logging.exception("AI web lookup rendering failed")
            await self._send_long_reply(
                message,
                tr(lang, "That current detail is not showing for me right now.", "Ese dato actual no me sale ahora mismo."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return
        reply = self._sanitize_visible_ai_output(reply)
        sent_message_id = await self._send_long_reply(
            message,
            self._dearm_mass_mentions(reply),
            reply_to_trigger=reply_to_trigger,
            mention_author=True,
        )
        self._create_or_renew_lease(message, last_bot_response_id=sent_message_id, action="WEB_LOOKUP", resolved_request=query[:900])

    @staticmethod
    def _web_lookup_type_from_text(text: str) -> str:
        lowered = text.casefold()
        if any(marker in lowered for marker in ("football", "soccer", "futbol", "fÃºtbol", "liga", "match", "partido")):
            return "sports"
        if any(marker in lowered for marker in ("price", "precio", "cost", "cuesta")):
            return "price"
        if any(marker in lowered for marker in ("outage", "status", "caido", "caÃ­do")):
            return "status"
        if any(marker in lowered for marker in ("release", "version", "update", "launch")):
            return "release"
        return "general"

    async def _execute_football_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        settings: object,
        lang: str,
        user_prompt: str,
        reply_to_trigger: bool,
        create_lease: bool = True,
    ) -> None:
        client = getattr(self.bot, "api_football_client", None)
        if client is None:
            await self._send_long_reply(
                message,
                tr(lang, "API-Football is not configured.", "API-Football no esta configurada."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return
        request = self._football_request_with_lease_context(message, decision.resolved_request or user_prompt)
        action = self._football_effective_action(decision.action, request)
        plan = await self._football_request_plan(message, action=action, request=request)
        operation = build_operation(action, request, plan)
        action = self._football_effective_action_from_plan(action, request, plan)
        league_key = self._football_league_from_operation_or_text(operation, request)
        try:
            league_id: int | None = None
            season: int | None = None
            if league_key is not None:
                league_id = await client.resolve_league_id(league_key)
                season = season_hint_to_int(operation.season_hint) or await client.get_current_season(league_id)
            fixtures: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            stats: list[dict[str, Any]] = []
            lineups: list[dict[str, Any]] = []
            endpoints = ["/leagues"] if league_id is not None else []
            football_notes: list[str] = []
            player_context_row: dict[str, Any] | None = None
            player_stat_focus: str | None = None
            team_context_row: dict[str, Any] | None = None
            standing_row: dict[str, Any] | None = None
            standings_table: list[dict[str, Any]] = []
            generic_rows: list[dict[str, Any]] = []
            generic_label: str | None = None
            data_focus = self._football_data_focus_from_plan_or_text(plan, action, request)
            logging.info(
                "AI football plan action=%s data_focus=%s league_candidates=%s team_candidates=%s player_candidates=%s selected_league=%s",
                action,
                data_focus,
                self._football_plan_list(plan, "league_candidates")[:4],
                list(operation.team_candidates)[:4],
                list(operation.player_candidates)[:4],
                league_key,
            )

            if action == "FOOTBALL_PLAYER_QUERY" or (
                data_focus not in {"scorers", "standings", "injuries", "transfers", "events", "lineups", "statistics"}
                and any(name in request.casefold() for name in ("mbappe", "haaland"))
            ):
                lookup = await football_resolver.resolve_player(
                    client,
                    request,
                    league_id=league_id,
                    season=season,
                    explicit_context=league_key is not None,
                    canonicalizer=self._football_player_canonicalizer(),
                    alias_cache=self._football_player_alias_cache(),
                    candidate_names=list(operation.player_candidates),
                    stat_focus=operation.stat_focus,
                )
                if league_key is not None and lookup.resolution.selected is None and not lookup.resolution.ambiguous:
                    fallback_lookup = await football_resolver.resolve_player(
                        client,
                        request,
                        canonicalizer=self._football_player_canonicalizer(),
                        alias_cache=self._football_player_alias_cache(),
                        candidate_names=list(operation.player_candidates),
                        stat_focus=operation.stat_focus,
                    )
                    if fallback_lookup.resolution.selected is not None or fallback_lookup.resolution.ambiguous:
                        football_notes.append("explicit_league_player_lookup_empty_global_fallback_used")
                        lookup = fallback_lookup
                picked = lookup.resolution
                logging.info(
                    "AI football player lookup action=%s raw_request=%s normalized_query=%s stat_focus=%s inferred_league=%s inferred_season=%s endpoint=/players response_count=%s top_candidates=%s resolver_decision=%s",
                    action,
                    request[:160],
                    " | ".join(lookup.query.candidates)[:160],
                    lookup.query.stat_focus,
                    league_key,
                    season,
                    len(lookup.rows),
                    self._football_player_candidate_names(list(lookup.rows)),
                    "ambiguous" if picked.ambiguous else ("selected" if picked.selected else "not_found"),
                )
                player_stat_focus = lookup.query.stat_focus
                if picked.selected is not None:
                    player_context_row = picked.selected
                    player_stats = picked.selected.get("statistics") if isinstance(picked.selected, dict) else []
                    first_stats = player_stats[0] if isinstance(player_stats, list) and player_stats else {}
                    team = first_stats.get("team") if isinstance(first_stats, dict) else {}
                    team_id = team.get("id") if isinstance(team, dict) else None
                    if isinstance(team_id, int) and league_id is not None and season is not None:
                        fixtures = await client.get_next_fixtures(league_id=league_id, season=season, next_count=3, team_id=team_id)
                    player = picked.selected.get("player") if isinstance(picked.selected, dict) else {}
                    football_notes.append(f"player_found={player.get('name') if isinstance(player, dict) else 'unknown'}")
                    if lookup.query.stat_focus == "penalties":
                        football_notes.append("penalty_specific_data=missing")
                elif picked.ambiguous:
                    names = self._football_player_candidate_names(list(picked.matches))
                    football_notes.append(f"player_ambiguous={', '.join(names)}")
                else:
                    football_notes.append("player_not_found")
                endpoints.append("/players")
            elif action == "FOOTBALL_TABLE":
                league_id, season = await self._football_default_league_context(client, league_id, season)
                standings_raw = await client.get_standings(league_id=league_id, season=season)
                standings_table = self._football_extract_table_rows(standings_raw)
                endpoints.append("/standings")
            elif data_focus == "scorers":
                league_id, season = await self._football_default_league_context(client, league_id, season)
                generic_rows = await client.get_top_scorers(league_id=league_id, season=season)
                generic_label = "FOOTBALL_SCORERS"
                endpoints.append("/players/topscorers")
            elif data_focus in {"next_fixtures", "last_fixtures", "season_start"}:
                team_context_row = await self._football_team_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                team = team_context_row.get("team") if isinstance(team_context_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if league_id is None:
                    league_id, season = await self._football_default_league_context(client, league_id, season)
                elif season is None:
                    season = await client.get_current_season(league_id)
                if data_focus == "last_fixtures":
                    fixtures = await client.get_last_fixtures(league_id=league_id, season=season, last_count=3, team_id=team_id if isinstance(team_id, int) else None)
                else:
                    fixtures = await client.get_next_fixtures(league_id=league_id, season=season, next_count=5 if data_focus == "season_start" else 3, team_id=team_id if isinstance(team_id, int) else None)
                endpoints.extend(["/teams"] if team_context_row is not None else [])
                endpoints.append("/fixtures")
            elif data_focus in {"h2h", "comparison"} or action == "FOOTBALL_COMPARISON":
                team_rows = await self._football_resolve_team_candidates(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                    max_teams=2,
                )
                team_ids = [row.get("team", {}).get("id") for row in team_rows if isinstance(row.get("team"), dict)]
                if len(team_ids) >= 2 and all(isinstance(item, int) for item in team_ids[:2]):
                    generic_rows = await client.get_head_to_head(team_a_id=team_ids[0], team_b_id=team_ids[1], last=10)
                    generic_label = "FOOTBALL_H2H"
                    endpoints.extend(["/teams", "/fixtures/headtohead"])
                else:
                    football_notes.append("h2h_teams_not_found")
                    endpoints.append("/teams")
            elif data_focus in {"injuries", "transfers"}:
                team_context_row = await self._football_team_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                team = team_context_row.get("team") if isinstance(team_context_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if isinstance(team_id, int) and data_focus == "injuries":
                    if league_id is not None and season is None:
                        season = await client.get_current_season(league_id)
                    generic_rows = await client.get_injuries(league_id=league_id, season=season, team_id=team_id)
                    generic_label = "FOOTBALL_INJURIES"
                    endpoints.extend(["/teams", "/injuries"])
                elif isinstance(team_id, int):
                    generic_rows = await client.get_transfers(team_id=team_id)
                    generic_label = "FOOTBALL_TRANSFERS"
                    endpoints.extend(["/teams", "/transfers"])
                else:
                    football_notes.append("team_not_found")
                    endpoints.append("/teams")
            elif action == "FOOTBALL_TEAM_QUERY":
                team_context_row = await self._football_team_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                team = team_context_row.get("team") if isinstance(team_context_row, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if isinstance(team_id, int) and league_id is not None and season is not None:
                    standings_raw = await client.get_standings(league_id=league_id, season=season)
                    standing_row = self._football_find_team_row(self._football_extract_table_rows(standings_raw), team_id)
                    fixtures = await client.get_next_fixtures(league_id=league_id, season=season, next_count=1, team_id=team_id)
                    if not fixtures:
                        fixtures = await client.get_last_fixtures(league_id=league_id, season=season, last_count=1, team_id=team_id)
                    endpoints.extend(["/teams", "/standings", "/fixtures"])
                elif isinstance(team_id, int):
                    fixtures = await client.get_fixtures_on_date(team_id=team_id, date_iso=date.today().isoformat())
                    endpoints.extend(["/teams", "/fixtures?date"])
                else:
                    football_notes.append("team_not_found")
                    endpoints.append("/teams")
            elif action == "FOOTBALL_MATCH_CENTER":
                if league_id is not None and season is None:
                    season = await client.get_current_season(league_id)
                team_id = await self._football_team_id_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                fixtures = await client.get_live_fixtures(league_id=league_id, team_id=team_id)
                endpoints.append("/fixtures?live=all")
                if not fixtures:
                    fixtures = await client.get_fixtures_on_date(
                        league_id=league_id,
                        season=season,
                        team_id=team_id,
                        date_iso=date.today().isoformat(),
                    )
                    endpoints.append("/fixtures?date")
            elif decision.action in {"FOOTBALL_EXPLAIN_RESULT", "FOOTBALL_SUMMARY", "FOOTBALL_TEAM_QUERY"} or any(word in request.casefold() for word in ("perdio", "perdió", "lost", "gano", "ganó", "won")):
                league_id, season = await self._football_default_league_context(client, league_id, season)
                team_id = await self._football_team_id_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                fixtures = await client.get_last_fixtures(league_id=league_id, season=season, last_count=1, team_id=team_id)
                endpoints.append("/fixtures")
            elif self._football_request_is_live_or_preseason(request):
                team_id = await self._football_team_id_from_text(
                    client,
                    request=request,
                    league_id=league_id,
                    season=season,
                    candidate_names=list(operation.team_candidates),
                )
                if team_id is None and league_id is None:
                    football_notes.append("team_not_found")
                fixtures = await client.get_live_fixtures(league_id=league_id, team_id=team_id)
                endpoints.append("/fixtures?live=all")
                if not fixtures:
                    if league_id is not None and season is None:
                        season = await client.get_current_season(league_id)
                    fixtures = await client.get_fixtures_on_date(
                        league_id=league_id,
                        season=season,
                        team_id=team_id,
                        date_iso=date.today().isoformat(),
                    )
                    endpoints.append("/fixtures?date")
                    football_notes.append(
                        "live_status_missing_same_day_fixture_found"
                        if fixtures
                        else "live_status_missing_no_same_day_fixture"
                    )
            else:
                if league_key is None and self._football_plan_has_entity_context(plan):
                    season = await client.get_current_season(league_id) if league_id is not None and season is None else season
                else:
                    league_id, season = await self._football_default_league_context(client, league_id, season)
                fixtures = await client.get_fixtures_on_date(league_id=league_id, season=season, date_iso=date.today().isoformat())
                endpoints.append("/fixtures")

            fixture_id = self._first_fixture_id(fixtures)
            if fixture_id is not None and data_focus != "fixtures" and self._football_request_needs_match_details(action, request):
                events = await client.get_fixture_events(fixture_id=fixture_id)
                stats = await client.get_fixture_statistics(fixture_id=fixture_id)
                lineups = await client.get_fixture_lineups(fixture_id=fixture_id)
                endpoints.extend(["/fixtures/events", "/fixtures/statistics", "/fixtures/lineups"])

            if action == "FOOTBALL_PLAYER_QUERY":
                context = build_player_context(
                    label=action,
                    player_row=player_context_row,
                    stat_focus=player_stat_focus,
                    fixtures=fixtures,
                    notes=football_notes,
                    source_endpoints=endpoints,
                )
            elif action == "FOOTBALL_TABLE":
                context = build_standings_context(
                    label=action,
                    standings=standings_table,
                    source_endpoints=endpoints,
                )
            elif action == "FOOTBALL_TEAM_QUERY" and team_context_row is not None:
                context = build_team_context(
                    team=team_context_row,
                    standings_row=standing_row,
                    fixtures=fixtures,
                    source_endpoints=endpoints,
                )
            elif generic_label is not None:
                context = self._football_generic_context(
                    label=generic_label,
                    rows=generic_rows,
                    team=team_context_row,
                    notes=football_notes,
                    source_endpoints=endpoints,
                )
            else:
                context = build_fixture_context(
                    label=action,
                    fixtures=fixtures,
                    events=events,
                    statistics=stats,
                    lineups=lineups,
                    source_endpoints=endpoints,
                )
                if football_notes:
                    context = f"{context}\nnotes={'; '.join(football_notes)[:800]}"
            web_context = ""
            has_api_data = bool(
                fixtures
                or events
                or stats
                or lineups
                or player_context_row
                or team_context_row
                or standings_table
                or generic_rows
            )
            if WebResearchService.should_try_football_web_fallback(
                request=request,
                action=action,
                has_api_data=has_api_data,
            ):
                try:
                    web_result = await self._web_research.research(
                        WebResearchRequest(
                            query=request,
                            lookup_type="sports",
                            max_sources=int(getattr(getattr(self.bot, "settings", None), "xai_web_search_max_sources", 3) or 3),
                        ),
                        guild_id=getattr(message.guild, "id", None),
                        user_id=getattr(message.author, "id", None),
                    )
                    web_context = format_web_research_context(web_result)
                except Exception:
                    logging.exception("AI football web fallback failed")
                    web_context = ""
            grounded_prompt = (
                football_web_grounding_prompt(request, context, web_context)
                if web_context
                else football_grounding_prompt(request, context)
            )
            enhanced_server_context = await self._server_context_with_memory(
                message,
                base_context=getattr(settings, "server_context", ""),
                route_action=action,
                current_text=request,
                replied_message=None,
            )
            reply = await self.bot.llm_client.chat(
                server_context=enhanced_server_context,
                user_message=grounded_prompt,
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
                image_urls=[],
            )
        except Exception:
            logging.exception("AI football action failed")
            await self._send_long_reply(
                message,
                tr(lang, "I cannot see that football detail right now.", "No me sale ese dato de futbol ahora mismo."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        reply = self._sanitize_visible_ai_output(reply)
        sent_message_id = await self._send_long_reply(
            message,
            self._dearm_mass_mentions(reply),
            reply_to_trigger=reply_to_trigger,
            mention_author=True,
        )
        if create_lease:
            self._create_or_renew_lease(message, last_bot_response_id=sent_message_id, action=action, resolved_request=request[:900])

    async def _football_request_plan(self, message: discord.Message, *, action: str, request: str) -> dict[str, Any] | None:
        planner = getattr(getattr(self.bot, "llm_client", None), "plan_football_request", None)
        if planner is None:
            return None
        try:
            plan = await planner(
                user_request=request,
                route_action=action,
                prior_context=self._football_prior_lease_context(message),
                replied_context=None,
            )
        except Exception:
            logging.exception("AI football request planner failed")
            return None
        return plan if isinstance(plan, dict) else None

    def _football_prior_lease_context(self, message: discord.Message) -> str | None:
        watch = self._watch_for_replied_message(message)
        if watch is not None:
            return f"Watched fixture: {watch.fixture_label}. Original watch request: {watch.request}"[:450]
        lease = self._valid_lease_for_message(message)
        if lease is None or not str(lease.last_action).startswith("FOOTBALL_"):
            return None
        prior = " ".join(str(lease.resolved_request or "").split())
        return prior[:450] if prior else None

    @staticmethod
    def _football_plan_list(plan: dict[str, Any] | None, key: str) -> list[str]:
        if not isinstance(plan, dict):
            return []
        values = plan.get(key)
        if not isinstance(values, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in values[:8]:
            cleaned = " ".join(str(value or "").split())[:120]
            lookup = football_resolver.normalize_key(cleaned)
            if cleaned and lookup and lookup not in seen:
                result.append(cleaned)
                seen.add(lookup)
        return result

    @staticmethod
    def _football_plan_text(plan: dict[str, Any] | None, key: str) -> str | None:
        if not isinstance(plan, dict):
            return None
        cleaned = " ".join(str(plan.get(key) or "").split())[:120]
        return cleaned or None

    def _football_effective_action_from_plan(self, action: str, request: str, plan: dict[str, Any] | None) -> str:
        normalized_action = self._football_effective_action(action, request)
        data_focus = self._football_plan_text(plan, "data_focus")
        if data_focus:
            focus_key = data_focus.casefold()
            if focus_key == "standings":
                return "FOOTBALL_TABLE"
            if focus_key == "player":
                return "FOOTBALL_PLAYER_QUERY"
            if focus_key == "team":
                return "FOOTBALL_TEAM_QUERY"
            if focus_key in {"scorers", "injuries", "transfers"}:
                return normalized_action
            if focus_key in {"events", "lineups", "statistics", "summary", "fixtures"}:
                return "FOOTBALL_MATCH_CENTER"
        intent = self._football_plan_text(plan, "intent")
        if normalized_action in {"FOOTBALL_LOOKUP", "FOOTBALL_FIXTURE_QUERY"} and intent:
            intent_key = intent.upper()
            if intent_key == "TABLE":
                return "FOOTBALL_TABLE"
            if intent_key == "PLAYER":
                return "FOOTBALL_PLAYER_QUERY"
            if intent_key == "TEAM":
                return "FOOTBALL_TEAM_QUERY"
            if intent_key in {"FIXTURE", "MATCH_CENTER", "LIVE", "SUMMARY"}:
                return "FOOTBALL_MATCH_CENTER"
        return normalized_action

    def _football_league_from_plan_or_text(self, plan: dict[str, Any] | None, text: str) -> str | None:
        for candidate in self._football_plan_list(plan, "league_candidates"):
            league_key = normalize_league_key(candidate)
            if league_key:
                return league_key
        return self._football_league_from_text(text)

    def _football_league_from_operation_or_text(self, operation: FootballQueryOperation, text: str) -> str | None:
        for candidate in operation.league_candidates:
            league_key = normalize_league_key(candidate)
            if league_key:
                return league_key
        return self._football_league_from_text(text)

    def _football_league_from_text(self, text: str) -> str | None:
        for token in (
            "world cup",
            "fifa world cup",
            "worldcup",
            "mundial",
            "copa mundial",
            "tercer lugar",
            "third place",
            "champions",
            "premier",
            "laliga",
            "liga de expansion mx",
            "liga de expansión mx",
            "liga expansion mx",
            "expansion mx",
            "liga mx",
            "ligamx",
            "concacaf",
        ):
            lookup_token = "world cup" if token in {"mundial", "copa mundial", "tercer lugar", "third place"} else token
            league_key = normalize_league_key(lookup_token)
            if league_key and token in text.casefold():
                return league_key
        return None

    def _football_effective_action(self, action: str, request: str) -> str:
        normalized_action = str(action or "").upper()
        if normalized_action == "FOOTBALL_LOOKUP" and self._football_request_is_table(request):
            return "FOOTBALL_TABLE"
        return normalized_action

    def _football_data_focus_from_plan_or_text(self, plan: dict[str, Any] | None, action: str, request: str) -> str | None:
        planned = self._football_plan_text(plan, "data_focus")
        if planned:
            return planned.casefold()
        lowered = request.casefold()
        if self._football_request_is_table(request) or action == "FOOTBALL_TABLE":
            return "standings"
        if any(marker in lowered for marker in ("goleador", "goleadores", "top scorer", "topscorer", "scorers", "tabla de goleo")):
            return "scorers"
        if any(marker in lowered for marker in ("lesion", "lesionado", "lesionados", "injury", "injuries")):
            return "injuries"
        if any(marker in lowered for marker in ("transfer", "transfers", "fichaje", "fichajes")):
            return "transfers"
        if action == "FOOTBALL_COMPARISON" or any(marker in lowered for marker in ("historial", "head to head", "h2h")):
            return "h2h"
        if any(marker in lowered for marker in ("cuando empieza", "when starts", "start of season", "season start", "inicio de temporada")):
            return "season_start"
        if any(marker in lowered for marker in ("proximo", "proximos", "cuando juega", "next match", "next game")):
            return "next_fixtures"
        if any(marker in lowered for marker in ("ultimo", "ultimos", "last match", "last game", "previous match")):
            return "last_fixtures"
        if any(marker in lowered for marker in ("alineacion", "lineup", "lineups")):
            return "lineups"
        if any(marker in lowered for marker in ("estadistica", "stats", "statistics")):
            return "statistics"
        if any(marker in lowered for marker in ("gol", "goles", "eventos", "events", "quien metio")):
            return "events"
        return None
    def _football_plan_has_entity_context(self, plan: dict[str, Any] | None) -> bool:
        if not isinstance(plan, dict):
            return False
        return bool(
            self._football_plan_list(plan, "league_candidates")
            or self._football_plan_list(plan, "team_candidates")
            or self._football_plan_list(plan, "player_candidates")
            or self._football_plan_text(plan, "fixture_focus")
        )

    @staticmethod
    def _football_request_is_table(text: str) -> bool:
        lowered = text.casefold()
        return any(marker in lowered for marker in ("tabla", "table", "standings", "posiciones", "clasificacion", "clasificación"))

    def _football_request_with_lease_context(self, message: discord.Message, request: str) -> str:
        cleaned = " ".join(str(request or "").split())
        watch = self._watch_for_replied_message(message)
        if watch is not None:
            prior_watch = f"Watched football fixture: {watch.fixture_label}. Original live-watch request: {watch.request}"
            return f"{cleaned}\nPrevious football context: {prior_watch}"[:900]
        lease = self._valid_lease_for_message(message)
        if lease is None or not str(lease.last_action).startswith("FOOTBALL_") or not lease.resolved_request:
            return cleaned
        prior = " ".join(str(lease.resolved_request).split())
        if not prior or prior.casefold() in cleaned.casefold():
            return cleaned
        return f"{cleaned}\nPrevious football context: {prior}"[:900]

    @staticmethod
    def _football_request_needs_match_details(action: str, request: str) -> bool:
        if action in {"FOOTBALL_MATCH_CENTER", "FOOTBALL_PREVIEW", "FOOTBALL_SUMMARY", "FOOTBALL_EXPLAIN_RESULT"}:
            return True
        lowered = request.casefold()
        return any(
            marker in lowered
            for marker in (
                "minuto",
                "minute",
                "gol",
                "goles",
                "goals",
                "quien metio",
                "quién metió",
                "scorer",
                "events",
                "eventos",
                "estadisticas",
                "stats",
                "alineacion",
                "lineup",
            )
        )

    @staticmethod
    async def _football_default_league_context(client: object, league_id: int | None, season: int | None) -> tuple[int, int]:
        if league_id is None:
            league_id = await client.resolve_league_id("ligamx")
        if season is None:
            season = await client.get_current_season(league_id)
        return league_id, season

    @staticmethod
    def _football_request_is_live_or_preseason(text: str) -> bool:
        lowered = text.casefold()
        return any(
            marker in lowered
            for marker in (
                "ahora",
                "ahorita",
                "now",
                "live",
                "en vivo",
                "pretemporada",
                "pre temporada",
                "preseason",
                "amistoso",
                "friendly",
            )
        )

    async def _execute_football_live_watch_action(
        self,
        message: discord.Message,
        decision: RouteDecision,
        *,
        lang: str,
        user_prompt: str,
        reply_to_trigger: bool,
    ) -> None:
        key = (int(message.guild.id), int(message.channel.id))
        if decision.action == "FOOTBALL_LIVE_WATCH_STOP":
            stopped = self._cancel_football_live_watch(key, reason="user_stop")
            text = (
                tr(lang, "Stopped live football updates here.", "Ya paré las actualizaciones en vivo aquí.")
                if stopped
                else tr(lang, "There are no live football updates active here.", "No hay actualizaciones en vivo activas aquí.")
            )
            await self._send_long_reply(message, text, reply_to_trigger=reply_to_trigger, mention_author=True)
            return

        client = getattr(self.bot, "api_football_client", None)
        if client is None:
            await self._send_long_reply(
                message,
                tr(lang, "API-Football is not configured.", "API-Football no esta configurada."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        request = self._football_request_with_lease_context(message, decision.resolved_request or user_prompt)
        action = "FOOTBALL_MATCH_CENTER"
        plan = await self._football_request_plan(message, action=action, request=request)
        operation = build_operation(decision.action, request, plan)
        fixture = await self._resolve_football_live_watch_fixture(client, request=request, plan=plan, operation=operation)
        if fixture is None:
            await self._send_long_reply(
                message,
                tr(
                    lang,
                    "I need the exact live match to watch.",
                    "Necesito el partido exacto en vivo para seguirlo.",
                ),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        fixture_obj = fixture.get("fixture") if isinstance(fixture, dict) else {}
        fixture_id = fixture_obj.get("id") if isinstance(fixture_obj, dict) else None
        if not isinstance(fixture_id, int):
            await self._send_long_reply(
                message,
                tr(lang, "I could not identify that match.", "No pude identificar ese partido."),
                reply_to_trigger=reply_to_trigger,
                mention_author=True,
            )
            return

        self._cancel_football_live_watch(key, reason="replace")
        watch = self._create_football_live_watch(message, fixture=fixture, request=request[:900])
        watch.task = asyncio.create_task(self._run_football_live_watch(key))
        self._football_live_watches[key] = watch
        logging.info(
            "AI football live watch started guild=%s channel=%s owner=%s fixture=%s label=%s",
            watch.guild_id,
            watch.channel_id,
            watch.owner_user_id,
            watch.fixture_id,
            self._anon_id(watch.fixture_label),
        )
        await self._send_long_reply(
            message,
            tr(
                lang,
                f"Okay, I will post live updates here for {watch.fixture_label}. Tell me to stop updates when you want me to stop.",
                f"Va, voy a mandar actualizaciones aquí de {watch.fixture_label}. Dime que pare las actualizaciones cuando quieras.",
            ),
            reply_to_trigger=reply_to_trigger,
            mention_author=True,
        )
        self._create_or_renew_lease(message, last_bot_response_id=None, action=decision.action, resolved_request=request[:900])

    async def _resolve_football_live_watch_fixture(
        self,
        client: object,
        *,
        request: str,
        plan: dict[str, Any] | None,
        operation: FootballQueryOperation | None = None,
    ) -> dict[str, Any] | None:
        operation = operation or build_operation("FOOTBALL_LIVE_WATCH_START", request, plan)
        league_key = self._football_league_from_operation_or_text(operation, request)
        league_id: int | None = None
        season: int | None = None
        if league_key is not None:
            league_id = await client.resolve_league_id(league_key)
            season = await client.get_current_season(league_id)
        team_id = await self._football_team_id_from_text(
            client,
            request=request,
            league_id=league_id,
            season=season,
            candidate_names=list(operation.team_candidates),
        )
        fixtures = await client.get_live_fixtures(league_id=league_id, team_id=team_id)
        if not fixtures:
            fixtures = await client.get_fixtures_on_date(
                league_id=league_id,
                season=season,
                team_id=team_id,
                date_iso=date.today().isoformat(),
            )
        if len(fixtures) == 1:
            return fixtures[0]
        if not fixtures:
            return None
        focus = self._football_plan_text(plan, "fixture_focus") or request
        picked = self._pick_football_fixture(fixtures, focus)
        if picked is not None:
            return picked
        return fixtures[0] if team_id is not None and len(fixtures) <= 2 else None

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
            fixtures = await client.get_fixture_by_id(fixture_id=watch.fixture_id)
            fixture = fixtures[0] if fixtures else None
            events = await client.get_fixture_events(fixture_id=watch.fixture_id)
            statistics: list[dict[str, Any]] = []
            lineups: list[dict[str, Any]] = []
            if fixture is not None:
                current_snapshot = snapshot_from_fixture(fixture)
                if should_fetch_lineups(current_snapshot, lineups_fetched=watch.lineups_fetched):
                    lineups_method = getattr(client, "get_fixture_lineups", None)
                    if lineups_method is not None:
                        lineups = await lineups_method(fixture_id=watch.fixture_id)
                    watch.lineups_fetched = True
                if should_fetch_statistics(current_snapshot, emitted_checkpoints=watch.emitted_checkpoints):
                    stats_method = getattr(client, "get_fixture_statistics", None)
                    if stats_method is not None:
                        statistics = await stats_method(fixture_id=watch.fixture_id)
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

    def _football_live_watch_updates(
        self,
        watch: FootballLiveWatch,
        fixture: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> list[str]:
        updates: list[str] = []
        score = fixture_score(fixture)
        status = fixture_status(fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {})
        for event in events[:60]:
            if not self._football_event_is_meaningful(event):
                continue
            event_key = self._football_event_key(event)
            if event_key in watch.seen_event_keys:
                continue
            watch.seen_event_keys.add(event_key)
            updates.append(self._football_format_event_update(event, fixture))
        if score != watch.last_score and not updates:
            updates.append(f"{watch.fixture_label}: {score} ({status})")
        elif self._football_status_changed_meaningfully(watch.last_status, status):
            updates.append(f"{watch.fixture_label}: {score} ({status})")
        watch.last_score = score
        watch.last_status = status
        return [item for item in updates if item]

    @staticmethod
    def _football_event_is_meaningful(event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "").casefold()
        detail = str(event.get("detail") or "").casefold()
        return event_type == "goal" or (event_type == "card" and "red" in detail)

    @staticmethod
    def _football_event_key(event: dict[str, Any]) -> str:
        time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
        team = event.get("team") if isinstance(event.get("team"), dict) else {}
        player = event.get("player") if isinstance(event.get("player"), dict) else {}
        return "|".join(
            str(part or "")
            for part in (
                time_info.get("elapsed"),
                time_info.get("extra"),
                team.get("id") or team.get("name"),
                player.get("id") or player.get("name"),
                event.get("type"),
                event.get("detail"),
            )
        )

    @staticmethod
    def _football_format_event_update(event: dict[str, Any], fixture: dict[str, Any]) -> str:
        time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
        team = event.get("team") if isinstance(event.get("team"), dict) else {}
        player = event.get("player") if isinstance(event.get("player"), dict) else {}
        elapsed = time_info.get("elapsed")
        extra = time_info.get("extra")
        minute = f"{elapsed}'" if isinstance(elapsed, int) else ""
        if isinstance(extra, int):
            minute = f"{elapsed}+{extra}'" if isinstance(elapsed, int) else f"+{extra}'"
        event_type = str(event.get("type") or "")
        detail = str(event.get("detail") or "")
        label = "Gol" if event_type.casefold() == "goal" else "Roja"
        team_name = str(team.get("name") or "Equipo")
        player_name = str(player.get("name") or "Jugador")
        return f"{label} {minute} {team_name} - {player_name}. Marcador: {fixture_score(fixture)}".strip()

    @staticmethod
    def _football_status_changed_meaningfully(previous: str, current: str) -> bool:
        previous_short = previous.split()[0] if previous else ""
        current_short = current.split()[0] if current else ""
        return current_short != previous_short and current_short in {"1H", "HT", "2H", "ET", "BT", "P", "FT", "AET", "PEN"}

    @staticmethod
    def _football_status_is_terminal(status: str) -> bool:
        short = status.split()[0] if status else ""
        return short in {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "PST"}

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

    @staticmethod
    def _football_team_candidates_from_text(text: str, candidate_names: list[str] | None = None) -> list[str]:
        cleaned_request = re.sub(
            r"(?i)\b(?:podrias?|puedes|dame|darme|informacion|info|sobre|del?|la|el|los|las|que|juega|en|de|liga|expansion|mx|mexico|fc|club)\b",
            " ",
            text,
        )
        candidates = list(candidate_names or [])
        normalized = football_resolver.normalize_key(text)
        for alias, canonical in football_resolver.TEAM_ALIASES.items():
            if alias and alias in normalized:
                candidates.insert(0, canonical)
        if not looks_like_raw_sentence(cleaned_request, text):
            candidates.extend([canonical_team_query(cleaned_request), cleaned_request.strip()])
        else:
            logging.info("AI football raw team sentence rejected request_hash=%s", abs(hash(normalized)) % 100000)
        for side in re.split(r"(?i)\b(?:vs|contra|versus)\b", text):
            side_cleaned = re.sub(
                r"(?i)\b(?:historial|h2h|head\s+to\s+head|partido|juego|goles|quien|metio|de|del|la|el|los|las)\b",
                " ",
                side,
            )
            if not looks_like_raw_sentence(side_cleaned, text):
                candidates.append(canonical_team_query(side_cleaned))
        candidates = clean_entity_candidates("team", candidates, text)
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            cleaned = " ".join(str(candidate or "").split())
            key = football_resolver.normalize_key(cleaned)
            if cleaned and key and key not in seen:
                deduped.append(cleaned)
                seen.add(key)
        return deduped

    async def _football_resolve_team_candidates(
        self,
        client: object,
        *,
        request: str,
        league_id: int | None,
        season: int | None,
        candidate_names: list[str] | None = None,
        max_teams: int = 2,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for candidate in self._football_team_candidates_from_text(request, candidate_names):
            for scoped_league_id, scoped_season in ((league_id, season), (None, None)):
                try:
                    teams = await client.search_teams(name=candidate, league_id=scoped_league_id, season=scoped_season)
                except Exception:
                    continue
                picked = pick_team(teams, candidate)
                if picked.selected is None:
                    continue
                team = picked.selected.get("team") if isinstance(picked.selected, dict) else {}
                team_id = team.get("id") if isinstance(team, dict) else None
                if isinstance(team_id, int) and team_id in seen_ids:
                    continue
                if isinstance(team_id, int):
                    seen_ids.add(team_id)
                rows.append(picked.selected)
                break
            if len(rows) >= max_teams:
                break
        return rows

    def _football_player_alias_cache(self) -> dict[str, dict[str, Any]]:
        cache = getattr(self.bot, "football_player_alias_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self.bot, "football_player_alias_cache", cache)
        return cache

    def _football_player_canonicalizer(self):
        method = getattr(getattr(self.bot, "llm_client", None), "canonicalize_football_player_query", None)
        if method is None:
            return None

        async def _canonicalize(query: football_resolver.PlayerQuery) -> dict[str, Any] | None:
            clean_query = query.candidates[0] if query.candidates else query.raw
            return await method(
                original_query=query.raw,
                clean_query=clean_query,
                stat_focus=query.stat_focus,
            )

        return _canonicalize

    @staticmethod
    def _football_player_candidate_names(rows: list[dict[str, Any]]) -> list[str]:
        names = []
        for item in rows[:5]:
            player = item.get("player") if isinstance(item, dict) else {}
            if isinstance(player, dict):
                names.append(f"{player.get('name')}#{player.get('id')}"[:120])
        return names

    async def _football_team_from_text(
        self,
        client: object,
        *,
        request: str,
        league_id: int | None,
        season: int | None,
        candidate_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        for candidate in self._football_team_candidates_from_text(request, candidate_names):
            for scoped_league_id, scoped_season in ((league_id, season), (None, None)):
                try:
                    teams = await client.search_teams(name=candidate, league_id=scoped_league_id, season=scoped_season)
                except Exception:
                    continue
                picked = pick_team(teams, candidate)
                if picked.selected is None:
                    continue
                return picked.selected
        return None

    async def _football_team_id_from_text(
        self,
        client: object,
        *,
        request: str,
        league_id: int | None,
        season: int | None,
        candidate_names: list[str] | None = None,
    ) -> int | None:
        selected = await self._football_team_from_text(
            client,
            request=request,
            league_id=league_id,
            season=season,
            candidate_names=candidate_names,
        )
        team = selected.get("team") if isinstance(selected, dict) else {}
        team_id = team.get("id") if isinstance(team, dict) else None
        if isinstance(team_id, int):
            return team_id
        return None

    @staticmethod
    def _football_generic_context(
        *,
        label: str,
        rows: list[dict[str, Any]],
        team: dict[str, Any] | None = None,
        notes: list[str] | None = None,
        source_endpoints: list[str] | None = None,
    ) -> str:
        payload = {
            "label": label,
            "team": team or {},
            "rows": rows[:12],
            "notes": notes or [],
            "source_endpoints": source_endpoints or [],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]

    @staticmethod
    def _football_extract_table_rows(standings_response: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not standings_response:
            return []
        first = standings_response[0]
        league = first.get("league") if isinstance(first, dict) else None
        standings = league.get("standings") if isinstance(league, dict) else None
        if not isinstance(standings, list) or not standings:
            return []
        if isinstance(standings[0], list):
            rows: list[dict[str, Any]] = []
            for group in standings:
                if isinstance(group, list):
                    rows.extend(row for row in group if isinstance(row, dict))
            return rows
        return [row for row in standings if isinstance(row, dict)]

    @staticmethod
    def _football_find_team_row(rows: list[dict[str, Any]], team_id: int | None) -> dict[str, Any] | None:
        if not isinstance(team_id, int):
            return None
        for row in rows:
            team = row.get("team") if isinstance(row, dict) else {}
            if isinstance(team, dict) and team.get("id") == team_id:
                return row
        return None

    @staticmethod
    def _first_fixture_id(fixtures: list[dict[str, Any]]) -> int | None:
        if not fixtures:
            return None
        fixture = fixtures[0].get("fixture") if isinstance(fixtures[0], dict) else {}
        fixture_id = fixture.get("id") if isinstance(fixture, dict) else None
        return fixture_id if isinstance(fixture_id, int) else None

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
    def _is_noise_only_followup(content: str) -> bool:
        normalized = content.strip().casefold()
        normalized = re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)
        if not normalized:
            return True
        if normalized in {
            "ok",
            "okay",
            "k",
            "kk",
            "va",
            "si",
            "sí",
            "yes",
            "no",
            "nah",
            "simon",
            "simón",
            "aja",
            "lol",
            "lmao",
            "xd",
            "haha",
            "hahaha",
            "hehe",
            "jaja",
            "jajaja",
            "jajajaja",
            "jajajajaja",
            "jeje",
            "jejeje",
            "jejejeje",
            "hmm",
            "mmm",
            "ayyy",
            "ayy",
            "nope",
            "nop",
            "wtf",
            "omg",
            "gg",
            "wp",
            "alch",
            "alchwe",
            "we",
            "literal",
            "buenarola",
            "nomames",
        }:
            return True
        return len(normalized) <= 2

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

    @staticmethod
    def _conversation_mode_for_anchor(anchor_type: str) -> str:
        if anchor_type == "SAME_USER_CONTINUATION":
            return "continuation"
        if anchor_type == "REPLY_TO_AI":
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
            filename = (getattr(attachment, "filename", "") or "").casefold()
            url = str(getattr(attachment, "url", "") or "").strip()
            if not url:
                continue
            if content_type in {"image/jpeg", "image/jpg", "image/png"} or filename.endswith(
                (".jpg", ".jpeg", ".png")
            ):
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
                lowered = url.casefold().split("?", 1)[0]
                if lowered.endswith((".jpg", ".jpeg", ".png")):
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
        current_urls = cls._extract_supported_image_urls(message.attachments)
        current_urls.extend(cls._extract_supported_embed_image_urls(getattr(message, "embeds", [])))
        replied_urls = (
            cls._extract_supported_image_urls(replied_message.attachments)
            if replied_message is not None
            else []
        )
        if replied_message is not None:
            replied_urls.extend(
                cls._extract_supported_embed_image_urls(getattr(replied_message, "embeds", []))
            )

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
        if current_urls:
            preferred_source_kind = "current_message"
            source_message_id = getattr(message, "id", None)
        elif replied_urls and replied_message is not None:
            preferred_source_kind = "reply_target"
            source_message_id = getattr(replied_message, "id", None)
        else:
            preferred_source_kind = "none"
            source_message_id = None
        prompt_note = ""
        if from_replied and replied_message is not None:
            author_name = getattr(getattr(replied_message, "author", None), "display_name", "another user")
            prompt_note = (
                "Image context: the user is asking about image attachment(s) "
                f"from a replied-to message sent by {author_name}."
            )
        return ChatImageContext(
            urls=urls,
            from_replied_message=from_replied,
            reaction_target=target,
            prompt_note=prompt_note,
            current_message_images=tuple(current_urls[:4]),
            reply_target_images=tuple(replied_urls[:4]),
            preferred_source_kind=preferred_source_kind,
            source_message_id=source_message_id,
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

        content = " ".join(str(getattr(replied_message, "content", "") or "").split())
        if content:
            lines.append(f"Text: {content[:900]}")

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
        if image_urls:
            lines.append(f"Image URLs: {', '.join(image_urls[:4])}")
        if not lines:
            return RepliedMessageContext()
        note = "[UNTRUSTED_REPLIED_MESSAGE_CONTEXT]\n" + "\n".join(lines[:14])
        return RepliedMessageContext(note=note, image_urls=image_urls[:4])

    @staticmethod
    def _apply_replied_message_context(prompt: str, context: RepliedMessageContext) -> str:
        if not context.note:
            return prompt
        return f"{prompt}\n\n{context.note}"

    async def _handle_image_generation_request(
        self,
        message: discord.Message,
        prompt: str,
        *,
        lang: str,
        parent_message_id: int | None = None,
        resolved_request: str | None = None,
        pending: PendingInteraction | None = None,
    ) -> None:
        await message.channel.typing()
        try:
            image_bytes = await self.bot.llm_client.generate_image(prompt)
        except Exception:
            logging.exception("AI image generation failure in guild=%s channel=%s", message.guild.id, message.channel.id)
            if pending is not None and not self._pending_can_send(pending):
                return
            await message.reply(
                tr(
                    lang,
                    "I could not generate that image right now. Ask an admin to check `XAI_IMAGE_MODEL` and xAI image generation access.",
                    "No pude generar esa imagen ahora. Pide a un admin revisar `XAI_IMAGE_MODEL` y el acceso a generacion de imagenes de xAI.",
                ),
                mention_author=True,
            )
            self._clear_pending_interaction(pending, event="error")
            return

        if pending is not None and not self._pending_can_send(pending):
            self._log_pending_event(message, "stale_suppressed", pending)
            return
        file = discord.File(BytesIO(image_bytes), filename="nitori_generated.png")
        caption = tr(lang, "Here, I made this.", "Va, hice esto.")
        sent = await message.reply(
            caption,
            file=file,
            mention_author=True,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        self._remember_chat_response_message(sent.id)

        convo_key = self._conversation_key(message.guild.id, message.channel.id)
        canonical_request = resolved_request or prompt
        user_turn = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=message.author.display_name,
            content=f"Generate image: {canonical_request}",
        )
        if user_turn is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="user",
                speaker=message.author.display_name,
                content=user_turn,
                message_id=getattr(message, "id", None),
                author_user_id=getattr(message.author, "id", None),
                parent_message_id=self._parent_message_id(message),
                action_type="GENERATE_IMAGE",
                resolved_request=canonical_request,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        bot_turn = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=f"Generated image: {canonical_request}",
        )
        if bot_turn is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=bot_turn,
                message_id=sent.id,
                author_user_id=getattr(self.bot.user, "id", None),
                parent_message_id=parent_message_id or getattr(message, "id", None),
                action_type="GENERATE_IMAGE",
                resolved_request=canonical_request,
            )
        self._create_or_renew_lease(
            message,
            last_bot_response_id=sent.id,
            action="GENERATE_IMAGE",
            resolved_request=canonical_request,
        )
        self._clear_pending_interaction(pending, event="success")

    async def _handle_image_edit_request(
        self,
        message: discord.Message,
        prompt: str,
        source_image_url: str,
        *,
        lang: str,
        parent_message_id: int | None = None,
        resolved_request: str | None = None,
        source_kind: str = "none",
        pending: PendingInteraction | None = None,
    ) -> None:
        await message.channel.typing()
        try:
            image_bytes = await self.bot.llm_client.edit_image(prompt, source_image_url)
        except Exception:
            logging.exception(
                "AI image edit failure in guild=%s channel=%s source_kind=%s",
                message.guild.id,
                message.channel.id,
                source_kind,
            )
            if pending is not None and not self._pending_can_send(pending):
                return
            await message.reply(
                tr(
                    lang,
                    "I could not edit that image right now. If it keeps happening, ask an admin to check `XAI_IMAGE_MODEL` and xAI image editing access.",
                    "No pude editar esa imagen ahora. Si sigue pasando, pide a un admin revisar `XAI_IMAGE_MODEL` y el acceso de edicion de imagenes de xAI.",
                ),
                mention_author=True,
            )
            self._clear_pending_interaction(pending, event="error")
            return

        if pending is not None and not self._pending_can_send(pending):
            self._log_pending_event(message, "stale_suppressed", pending)
            return
        file = discord.File(BytesIO(image_bytes), filename="nitori_edited.png")
        caption = tr(lang, "Here, I edited it.", "Va, la edite.")
        sent = await message.reply(
            caption,
            file=file,
            mention_author=True,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        self._remember_chat_response_message(sent.id)

        convo_key = self._conversation_key(message.guild.id, message.channel.id)
        canonical_request = resolved_request or prompt
        user_turn = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=message.author.display_name,
            content=f"Edit image: {canonical_request}",
        )
        if user_turn is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="user",
                speaker=message.author.display_name,
                content=user_turn,
                message_id=getattr(message, "id", None),
                author_user_id=getattr(message.author, "id", None),
                parent_message_id=self._parent_message_id(message),
                action_type="EDIT_IMAGE",
                resolved_request=canonical_request,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        bot_turn = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=f"Edited image: {canonical_request}",
        )
        if bot_turn is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=bot_turn,
                message_id=sent.id,
                author_user_id=getattr(self.bot.user, "id", None),
                parent_message_id=parent_message_id or getattr(message, "id", None),
                action_type="EDIT_IMAGE",
                resolved_request=canonical_request,
            )
        self._create_or_renew_lease(
            message,
            last_bot_response_id=sent.id,
            action="EDIT_IMAGE",
            resolved_request=canonical_request,
        )
        self._clear_pending_interaction(pending, event="success")

    @staticmethod
    def _image_edit_source_url(image_context: ChatImageContext) -> str | None:
        for urls in (
            image_context.current_message_images,
            image_context.reply_target_images,
            image_context.prior_branch_images,
        ):
            for url in urls:
                cleaned = str(url or "").strip()
                if cleaned:
                    return cleaned
        return None

    def _strip_bot_name_prefix(self, text: str) -> str:
        cleaned = text.strip()
        if self.bot.user is None:
            return cleaned
        names = {
            str(getattr(self.bot.user, "name", "") or "").strip(),
            str(getattr(self.bot.user, "display_name", "") or "").strip(),
            str(getattr(self.bot.user, "global_name", "") or "").strip(),
        }
        for name in sorted((item for item in names if item), key=len, reverse=True):
            updated = re.sub(
                rf"^@?{re.escape(name)}(?:\b|[\s,:;!?-])\s*",
                "",
                cleaned,
                count=1,
                flags=re.IGNORECASE,
            )
            if updated != cleaned:
                return updated.strip()
        return cleaned

    async def _maybe_add_ai_reaction(
        self,
        *,
        target: object,
        reaction_key: tuple[int, int],
        user_prompt: str,
        assistant_reply: str,
        channel_name: str,
        available_emojis: list[str],
        conversation_mode: str,
    ) -> None:
        if not hasattr(target, "add_reaction"):
            return
        if not self._consume_reaction_cooldown(reaction_key):
            return
        suggest_reaction = getattr(self.bot.llm_client, "suggest_reaction", None)
        if suggest_reaction is None:
            return
        try:
            reaction = await suggest_reaction(
                user_message=user_prompt,
                assistant_reply=assistant_reply,
                channel_name=channel_name,
                available_emojis=available_emojis,
                conversation_mode=conversation_mode,
            )
            if not reaction:
                return
            emoji: str | discord.PartialEmoji = reaction
            if reaction.startswith("<") and reaction.endswith(">"):
                emoji = discord.PartialEmoji.from_str(reaction)
            await target.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Failed to add AI reaction")
        except Exception:
            logging.exception("AI reaction suggestion failed")

    def _consume_reaction_cooldown(self, key: tuple[int, int]) -> bool:
        now = time.monotonic()
        next_allowed = self._reaction_cooldowns.get(key, 0.0)
        if next_allowed > now:
            return False
        self._reaction_cooldowns[key] = now + self._reaction_cooldown_seconds
        return True

    @staticmethod
    def _message_channel_key(message: discord.Message) -> tuple[int, int]:
        return (int(message.guild.id), int(message.channel.id))  # type: ignore[union-attr]

    @staticmethod
    def _remove_bot_mentions(text: str, bot_id: int) -> str:
        text = text.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "")
        return text.strip()

    @staticmethod
    def _detect_command_hint(prompt: str) -> str | None:
        lowered = prompt.strip().casefold()
        if not lowered:
            return None
        lowered = lowered.lstrip("/.!")
        parts = re.findall(r"[a-z0-9]+", lowered)
        if not parts:
            return None

        first = parts[0]
        second = parts[1] if len(parts) > 1 else ""
        if first == "channel":
            if second in {"delete", "del", "remove"}:
                return "channel delete"
            if second == "add":
                return "channel add"
            if second in {"clear", "clone", "lock", "unlock", "slowmode"}:
                return f"channel {second}"
        if first == "message" and second in {"delete", "clear", "purgeuser"}:
            return f"message {second}"
        if first in {"member", "user"} and second in {
            "info",
            "setnick",
            "mute",
            "unmute",
            "kick",
            "ban",
            "unban",
            "tempmute",
            "tempban",
            "warn",
            "unwarn",
            "warnings",
            "clearwarnings",
        }:
            return f"user {second}"
        if first == "role" and second in {"add", "remove", "create"}:
            return f"role {second}"
        if first == "color" and second in {"setup", "list", "channel", "reload", "add", "remove"}:
            return f"color {second}"
        compact = f"{first}{second}" if second else first
        for key in (compact, first):
            hinted = COMMAND_HINTS.get(key)
            if hinted:
                return hinted
        return None

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

        repair_candidate = self._valid_missed_response_candidate(message)
        if repair_candidate is not None and self._is_missed_response_repair_message(message):
            history = [
                {
                    "role": "user",
                    "content": f"{repair_candidate.author_name}: {repair_candidate.snippet}",
                }
            ]
            history.extend(self._build_conversation_history(key)[-12:])
            return history[-28:]

        if lease is not None:
            same_user_history = await self._same_user_branch_history(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                owner_user_id=lease.owner_user_id,
            )
            if same_user_history:
                return same_user_history[-28:]

        return self._build_conversation_history(key)[-16:]

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

    async def _recover_contextual_image_request(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
        resolved_request: str | None,
    ) -> str | None:
        cleaned = " ".join(str(resolved_request or "").split())
        if cleaned and cleaned.casefold() not in {"it", "that", "this", "eso", "esto", "la imagen", "the image"}:
            return cleaned[:900]

        prior = await self._prior_resolved_image_request(message, replied_message)
        if not prior:
            return cleaned[:900] if cleaned else None
        change = cleaned or message.content.strip()
        return f"{prior}; apply this change: {change}"[:900]

    async def _prior_resolved_image_request(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> str | None:
        db = getattr(self.bot, "db", None)
        getter = getattr(db, "get_ai_turn_by_message_id", None)
        if getter is not None and replied_message is not None:
            try:
                row = await getter(message.guild.id, message.channel.id, replied_message.id)
                prior = str((row or {}).get("resolved_request", "") or "").strip()
                if prior:
                    return prior
            except Exception:
                logging.exception("Failed to read replied image request context")
        lease = self._valid_lease_for_message(message)
        if lease is not None and lease.resolved_request:
            return lease.resolved_request
        return None

    def _recent_intent_context(self, key: tuple[int, int]) -> list[dict[str, str]]:
        rows = self._conversation_history.get(key)
        if not rows:
            return []
        context: list[dict[str, str]] = []
        for item in list(rows)[-8:]:
            content = str(item.get("content", "")).strip()
            if ": " in content:
                author, text = content.split(": ", 1)
            else:
                author, text = "unknown", content
            if text:
                context.append({"author": author or "unknown", "content": text})
        return context

    async def _route_ai_decision(
        self,
        *,
        message: discord.Message,
        anchor_type: str,
        bot_name: str,
        image_context: ChatImageContext,
        replied_message: discord.Message | None,
        pending_interaction: PendingInteraction | None = None,
        available_emojis: list[str] | None = None,
    ) -> RouteDecision:
        route_method = getattr(self.bot.llm_client, "route_ai_interaction", None)
        if route_method is None:
            if anchor_type in STRONG_ANCHORS:
                local_reaction = self._local_reaction_only_decision(message, available_emojis or [])
                if local_reaction is not None:
                    return local_reaction
            return self._fallback_route_decision(anchor_type, failure_reason="api_exception")
        logging.info(
            "AI visual context guild=%s channel=%s message=%s current_count=%s reply_target_count=%s prior_branch_count=%s preferred_source_kind=%s source_message_id=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            len(image_context.current_message_images),
            len(image_context.reply_target_images),
            len(image_context.prior_branch_images),
            image_context.preferred_source_kind,
            image_context.source_message_id,
        )
        try:
            raw = await route_method(
                bot_name=bot_name,
                bot_id=getattr(self.bot.user, "id", None),
                known_aliases=sorted({self._normalize_alias_text(alias) for alias in self._bot_aliases() if alias}),
                matched_alias=self._matched_alias_for_anchor(message.content, anchor_type),
                author_name=message.author.display_name,
                author_id=getattr(message.author, "id", None),
                current_message=message.content,
                anchor_type=anchor_type,
                recent_context=await self._channel_history_intent_context(message),
                mentions=self._route_mentions(message),
                reply_metadata=self._route_reply_metadata(message, replied_message),
                lease_metadata=self._route_lease_metadata(message),
                repair_metadata=self._route_missed_response_metadata(message),
                image_metadata=self._route_image_metadata(image_context),
                pending_metadata=self._route_pending_metadata(pending_interaction),
                authority_metadata=self._route_authority_metadata(message),
                available_emojis=available_emojis or [],
            )
        except Exception:
            logging.exception("AI route classifier failure")
            if anchor_type in STRONG_ANCHORS:
                local_reaction = self._local_reaction_only_decision(message, available_emojis or [])
                if local_reaction is not None:
                    return local_reaction
            return self._fallback_route_decision(anchor_type, failure=True, failure_reason="api_exception")
        decision = self._coerce_route_decision(raw)
        local_behavior_update = self._local_trusted_behavior_update_decision(message, decision)
        if local_behavior_update is not None:
            return local_behavior_update
        local_visual = self._local_visual_action_decision(message, anchor_type, image_context, decision)
        if local_visual is not None:
            return local_visual
        if self._has_no_text_reaction_intent(message.content):
            local_reaction = self._local_reaction_only_decision(message, available_emojis or [])
            if local_reaction is not None:
                return local_reaction
            if anchor_type in STRONG_ANCHORS and decision.action == "CHAT":
                return RouteDecision(
                    participation="RESPOND",
                    action="CLARIFY",
                    participation_confidence=max(decision.participation_confidence, 1.0),
                    action_confidence=0.0,
                    reason_code="CLARIFICATION_NEEDED",
                    valid=True,
                    failure=False,
                    send_text=True,
                )
            if decision.action == "CHAT":
                return RouteDecision(
                    participation="IGNORE",
                    action="NONE",
                    participation_confidence=0.0,
                    action_confidence=0.0,
                    reason_code="NO_MEANINGFUL_CONTENT",
                    valid=True,
                    failure=False,
                    send_text=False,
                )
        if (decision.failure or not decision.valid) and anchor_type in STRONG_ANCHORS:
            local_reaction = self._local_reaction_only_decision(message, available_emojis or [])
            if local_reaction is not None:
                return local_reaction
        if (decision.failure or not decision.valid) and anchor_type in STRONG_ANCHORS:
            return self._fallback_route_decision(
                anchor_type,
                failure=decision.failure,
                failure_reason="fallback_strong_anchor_chat",
            )
        if (
            decision.valid
            and decision.action in {"GENERATE_IMAGE", "EDIT_IMAGE", "ANALYZE_IMAGE"}
            and decision.action_confidence < IMAGE_ACTION_CONFIDENCE
            and anchor_type in STRONG_ANCHORS
        ):
            return RouteDecision(
                participation="RESPOND",
                action="CHAT",
                participation_confidence=decision.participation_confidence,
                action_confidence=decision.action_confidence,
                reason_code=decision.reason_code,
                resolved_request=decision.resolved_request,
                valid=True,
                failure=False,
                target_message=decision.target_message,
                emoji=decision.emoji,
                emojis=decision.emojis,
                send_text=decision.send_text,
                pending_operation=decision.pending_operation,
                failure_reason=decision.failure_reason,
            )
        if decision.valid and decision.action == "GENERATE_IMAGE":
            recovered = await self._recover_contextual_image_request(
                message,
                replied_message,
                decision.resolved_request,
            )
            if recovered:
                decision = RouteDecision(
                    participation=decision.participation,
                    action=decision.action,
                    participation_confidence=decision.participation_confidence,
                    action_confidence=decision.action_confidence,
                    reason_code=decision.reason_code,
                    resolved_request=recovered,
                    valid=decision.valid,
                    failure=decision.failure,
                    target_message=decision.target_message,
                    emoji=decision.emoji,
                    emojis=decision.emojis,
                    send_text=decision.send_text,
                    pending_operation=decision.pending_operation,
                    failure_reason=decision.failure_reason,
                )
        return decision

    def _local_visual_action_decision(
        self,
        message: discord.Message,
        anchor_type: str,
        image_context: ChatImageContext,
        decision: RouteDecision,
    ) -> RouteDecision | None:
        if anchor_type not in STRONG_ANCHORS | {"SAME_USER_CONTINUATION", "PENDING_FOLLOWUP", "MISSED_RESPONSE_REPAIR"}:
            return None
        if decision.valid and not decision.failure and decision.action not in {"CHAT", "CLARIFY", "NONE"}:
            return None
        content = str(getattr(message, "content", "") or "")
        prompt = self._clean_visual_request_text(content)
        if self._has_visual_analysis_intent(content):
            return RouteDecision(
                participation="RESPOND",
                action="ANALYZE_IMAGE",
                participation_confidence=1.0,
                action_confidence=1.0,
                reason_code="IMAGE_ANALYSIS_REQUEST",
                resolved_request=prompt or None,
                valid=True,
                failure=False,
            )
        if self._has_visual_edit_intent(content, has_image=bool(image_context.urls)):
            return RouteDecision(
                participation="RESPOND",
                action="EDIT_IMAGE",
                participation_confidence=1.0,
                action_confidence=1.0,
                reason_code="IMAGE_CONTEXT_EDIT",
                resolved_request=prompt or "Edit the referenced image as requested.",
                valid=True,
                failure=False,
            )
        return None

    def _clean_visual_request_text(self, content: str) -> str:
        cleaned = re.sub(r"<@!?\d+>", " ", str(content or ""))
        cleaned = self._strip_bot_name_prefix(cleaned)
        return " ".join(cleaned.split())[:900]

    def _has_visual_analysis_intent(self, content: str) -> bool:
        normalized = self._normalize_alias_text(content)
        return any(
            marker in normalized
            for marker in (
                "que ves",
                "analiza",
                "describe la imagen",
                "describe esta imagen",
                "what do you see",
                "analyze this image",
                "describe this image",
            )
        )

    def _has_visual_edit_intent(self, content: str, *, has_image: bool) -> bool:
        normalized = self._normalize_alias_text(content)
        edit_markers = (
            "hazlo mas grande",
            "hazla mas grande",
            "agranda",
            "quita",
            "quitale",
            "ponle",
            "cambia",
            "modifica",
            "edita",
            "edit",
            "modify",
            "remove",
            "add",
            "make bigger",
            "change",
        )
        if not any(marker in normalized for marker in edit_markers):
            return False
        if has_image:
            return True
        return any(
            marker in normalized
            for marker in (
                "imagen",
                "foto",
                "dibujo",
                "image",
                "picture",
                "photo",
                "hazlo",
                "hazla",
                "this",
                "it",
            )
        )

    def _route_authority_metadata(self, message: discord.Message) -> dict[str, bool]:
        is_owner = bool(getattr(self.bot, "is_owner_user", lambda _user: False)(message.author))
        guild_owner_id = getattr(getattr(message, "guild", None), "owner_id", None)
        is_guild_owner = guild_owner_id is not None and int(getattr(message.author, "id", 0) or 0) == int(guild_owner_id)
        permissions = getattr(message.author, "guild_permissions", None)
        has_admin = bool(getattr(permissions, "administrator", False))
        has_manage_guild = bool(getattr(permissions, "manage_guild", False))
        can_manage = is_owner or is_guild_owner or has_admin or has_manage_guild
        return {
            "author_is_bot_owner": is_owner,
            "author_is_guild_owner": is_guild_owner,
            "author_has_administrator": has_admin,
            "author_has_manage_guild": has_manage_guild,
            "author_can_manage_bot_behavior": can_manage,
        }

    def _author_can_manage_bot_behavior(self, message: discord.Message) -> bool:
        return bool(self._route_authority_metadata(message).get("author_can_manage_bot_behavior"))

    def _local_trusted_behavior_update_decision(
        self,
        message: discord.Message,
        decision: RouteDecision,
    ) -> RouteDecision | None:
        if not self._author_can_manage_bot_behavior(message):
            return None
        if decision.action.startswith("SERVER_MEMORY_") and decision.memory:
            return None
        payload = self._local_bot_behavior_memory_payload(message.content)
        if payload is None:
            return None
        if decision.action not in {"CHAT", "CLARIFY", "NONE"} and decision.valid and not decision.failure:
            return None
        return RouteDecision(
            participation="RESPOND",
            action="SERVER_MEMORY_WRITE",
            participation_confidence=1.0,
            action_confidence=1.0,
            reason_code="SERVER_MEMORY_REQUEST",
            memory=payload,
            valid=True,
            failure=False,
        )

    @staticmethod
    def _local_bot_behavior_memory_payload(content: str) -> dict[str, str] | None:
        raw_content = " ".join(str(content or "").split())
        lowered = raw_content.casefold()
        normalized = "".join(
            char for char in unicodedata.normalize("NFKD", lowered)
            if not unicodedata.combining(char)
        )
        behavior_markers = (
            "ya no uses",
            "no uses",
            "usa menos",
            "usa mas",
            "usa más",
            "deja de usar",
            "no empieces",
            "no digas",
            "responde",
            "contesta",
            "se mas",
            "sé mas",
            "cambia el tono",
            "cambia como respondes",
            "cuando pregunt",
            "en este canal",
            "en este servidor",
            "change your style",
            "change how you reply",
            "reply more",
            "answer more",
            "don't use",
            "dont use",
            "stop using",
            "stop saying",
        )
        bot_style_markers = ("orale", "órale", "muletilla", "frase", "respuestas", "replies", "style")
        if not any(marker in lowered or marker in normalized for marker in behavior_markers):
            return None
        broader_style_markers = (
            "contestas",
            "tono",
            "formal",
            "seco",
            "emoji",
            "emojis",
            "futbol",
            "football",
            "canal",
            "channel",
            "server",
            "servidor",
            "comportamiento",
            "behavior",
            "habit",
        )
        if not any(marker in lowered or marker in normalized for marker in bot_style_markers + broader_style_markers):
            return None
        if "orale" in normalized or "órale" in lowered:
            value = "Do not start replies with 'Orale wey'. Prefer 'wey' or no fixed opener."
            key = "style.opening_phrase"
        elif "emoji" in normalized:
            value = f"Follow this trusted admin emoji/style instruction: {raw_content[:760]}"
            key = "style.emoji_usage"
        elif "futbol" in normalized or "football" in normalized:
            value = f"Follow this trusted admin football response instruction: {raw_content[:760]}"
            key = "football.response_style"
        elif "formal" in normalized or "seco" in normalized or "tono" in normalized:
            value = f"Follow this trusted admin tone instruction: {raw_content[:760]}"
            key = "style.tone"
        else:
            value = f"Follow this trusted admin behavior instruction: {raw_content[:760]}"
            key = "bot.behavior_rule"
        return {
            "memory_type": "BOT_BEHAVIOR_RULE",
            "key": key,
            "value": value,
            "scope": "channel" if "este canal" in normalized or "this channel" in normalized else "guild",
        }

    def _coerce_route_decision(self, raw: object) -> RouteDecision:
        if not isinstance(raw, dict):
            return RouteDecision("IGNORE", "NONE", 0.0, 0.0, "ROUTER_FAILURE", valid=False, failure=True, failure_reason="validation_exception")
        try:
            participation = str(raw.get("participation", "IGNORE")).upper()
            action = str(raw.get("action", "NONE")).upper()
            if participation == "IGNORE" and action == "IGNORE":
                action = "NONE"
            if participation == "REACT_ONLY" and action == "REACT_ONLY":
                participation = "RESPOND"
            raw_failure = bool(raw.get("failure", False)) or not bool(raw.get("valid", False))
            default_reason = "ROUTER_FAILURE" if raw_failure else "UNRELATED_HUMAN_CHAT"
            decision = RouteDecision(
                participation=participation,
                action=action,
                participation_confidence=float(raw.get("participation_confidence", 0.0)),
                action_confidence=float(raw.get("action_confidence", 0.0)),
                reason_code=str(raw.get("reason_code", default_reason)).upper(),
                resolved_request=(
                    str(raw.get("resolved_request")).strip()
                    if raw.get("resolved_request") is not None
                    else None
                ),
                target_message=self._coerce_optional_int(raw.get("target_message")),
                emoji=(str(raw.get("emoji")).strip() if raw.get("emoji") is not None else None),
                emojis=tuple(
                    str(item).strip()
                    for item in raw.get("emojis", [])
                    if isinstance(raw.get("emojis", []), list) and str(item).strip()
                ),
                send_text=bool(raw.get("send_text", True)),
                pending_operation=(
                    str(raw.get("pending_operation")).strip().upper()
                    if raw.get("pending_operation") is not None
                    else None
                ),
                memory=self._coerce_memory_payload(raw.get("memory")),
                valid=bool(raw.get("valid", False)),
                failure=bool(raw.get("failure", False)),
                failure_reason=(
                    str(raw.get("failure_reason")).strip()
                    if raw.get("failure_reason") is not None
                    else None
                ),
            )
            if not self._route_decision_shape_is_valid(decision):
                return RouteDecision("IGNORE", "NONE", 0.0, 0.0, "ROUTER_FAILURE", valid=False, failure=True, failure_reason=decision.failure_reason or "validation_exception")
            return decision
        except (TypeError, ValueError):
            return RouteDecision("IGNORE", "NONE", 0.0, 0.0, "ROUTER_FAILURE", valid=False, failure=True, failure_reason="validation_exception")

    @staticmethod
    def _coerce_memory_payload(value: object) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        result: dict[str, str] = {}
        for key in ("memory_type", "subject", "key", "value", "scope"):
            raw = value.get(key)
            if raw is None:
                continue
            cleaned = " ".join(str(raw).split())[:900 if key == "value" else 120]
            if cleaned:
                result[key] = cleaned
        return result or None

    @staticmethod
    def _coerce_optional_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _route_decision_shape_is_valid(decision: RouteDecision) -> bool:
        if decision.participation not in ROUTE_PARTICIPATIONS:
            return False
        if decision.action not in ROUTE_ACTIONS:
            return False
        if decision.reason_code not in ROUTE_REASON_CODES:
            return False
        if not 0.0 <= decision.participation_confidence <= 1.0:
            return False
        if not 0.0 <= decision.action_confidence <= 1.0:
            return False
        if decision.participation == "IGNORE" and decision.action != "NONE":
            return False
        if decision.action == "IGNORE":
            return False
        if decision.participation == "RESPOND" and decision.action == "NONE":
            return False
        if decision.participation == "RESPOND" and decision.action == "IGNORE":
            return False
        if decision.action == "REACT_ONLY" and decision.send_text:
            return False
        if decision.action in {"ADD_REACTION", "REACT_ONLY"} and not (decision.emoji or decision.emojis):
            return False
        if decision.participation == "REACT_ONLY" and decision.action != "NONE":
            return False
        if decision.action.startswith("SERVER_MEMORY_") and decision.action != "SERVER_MEMORY_CLARIFY":
            if not decision.memory:
                return False
            if decision.action in {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE"} and not decision.memory.get("value"):
                return False
        return True

    def _matched_alias_for_anchor(self, content: str, anchor_type: str) -> str | None:
        if anchor_type == "NAME_AT_START":
            return self._alias_at_start_match(content)
        if anchor_type == "NAME_REFERENCE":
            return self._alias_reference_match(content)
        return None

    def _local_reaction_only_decision(
        self,
        message: discord.Message,
        available_emojis: list[str],
    ) -> RouteDecision | None:
        emoji = self._extract_local_reaction_only_emoji(message.content, available_emojis)
        if emoji is None:
            return None
        return RouteDecision(
            participation="RESPOND",
            action="REACT_ONLY",
            participation_confidence=1.0,
            action_confidence=1.0,
            reason_code="REACTION_ACK",
            emoji=emoji,
            send_text=False,
            valid=True,
            failure=False,
        )

    def _extract_local_reaction_only_emoji(self, content: str, available_emojis: list[str]) -> str | None:
        if not self._has_no_text_reaction_intent(content):
            return None
        for token in re.findall(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>", content):
            if any(token in item for item in available_emojis):
                return token
        for token in reversed(content.strip().split()):
            cleaned = token.strip(".,;:!?()[]{}\"'")
            if re.fullmatch(r"<[@#][!&]?\d+>", cleaned):
                continue
            if self._looks_like_unicode_emoji(cleaned):
                return cleaned
        return None

    def _has_no_text_reaction_intent(self, content: str) -> bool:
        lowered = content.casefold()
        normalized = self._normalize_alias_text(content)
        no_text_markers = (
            "no digas nada",
            "no contestes",
            "sin responder",
            "dont reply",
            "don't reply",
            "don t reply",
            "do not reply",
            "just react",
            "solo reacciona",
        )
        reaction_markers = ("reacciona", "react", "ponle", "pon", "with", "con")
        return (
            any(marker in lowered or marker in normalized for marker in no_text_markers)
            and any(marker in lowered or marker in normalized for marker in reaction_markers)
        )

    @staticmethod
    def _looks_like_unicode_emoji(value: str) -> bool:
        if not value:
            return False
        compact = value.replace("\ufe0f", "")
        return len(compact) <= 8 and not re.fullmatch(r"[\w\s]+", compact, flags=re.UNICODE)

    def _fallback_route_decision(
        self,
        anchor_type: str,
        *,
        failure: bool = True,
        failure_reason: str | None = None,
    ) -> RouteDecision:
        if anchor_type in STRONG_ANCHORS:
            return RouteDecision(
                participation="RESPOND",
                action="CHAT",
                participation_confidence=1.0,
                action_confidence=0.0,
                reason_code="DIRECT_REQUEST",
                valid=True,
                failure=failure,
                failure_reason=failure_reason or "fallback_strong_anchor_chat",
            )
        return RouteDecision(
            participation="IGNORE",
            action="NONE",
            participation_confidence=0.0,
            action_confidence=0.0,
            reason_code="ROUTER_FAILURE",
            valid=False,
            failure=failure,
            failure_reason=failure_reason or "fallback_ambiguous_ignore",
        )

    def _route_allows_response(self, anchor_type: str, decision: RouteDecision) -> bool:
        if decision.failure or not decision.valid:
            return anchor_type in STRONG_ANCHORS
        if decision.participation == "IGNORE":
            return False
        if anchor_type in AMBIGUOUS_ANCHORS:
            if decision.participation_confidence < AMBIGUOUS_ROUTING_CONFIDENCE:
                return False
            if decision.reason_code not in AMBIGUOUS_ALLOWED_REASON_CODES:
                return False
        if decision.action in {"GENERATE_IMAGE", "EDIT_IMAGE", "ANALYZE_IMAGE"} and decision.action_confidence < IMAGE_ACTION_CONFIDENCE:
            return False
        return True

    def _route_mentions(self, message: discord.Message) -> list[dict[str, object]]:
        mentions: list[dict[str, object]] = []
        for member in getattr(message, "mentions", []) or []:
            mentions.append(
                {
                    "id": getattr(member, "id", None),
                    "name": getattr(member, "display_name", None) or getattr(member, "name", "unknown"),
                    "is_bot": bool(getattr(member, "bot", False)),
                    "is_self": self.bot.user is not None and getattr(member, "id", None) == self.bot.user.id,
                }
            )
        return mentions

    def _route_reply_metadata(
        self,
        message: discord.Message,
        replied_message: discord.Message | None,
    ) -> dict[str, object]:
        reference = getattr(message, "reference", None)
        watch = self._watch_for_replied_message(message)
        return {
            "message_id": getattr(replied_message, "id", None),
            "reference_message_id": getattr(reference, "message_id", None),
            "author_id": getattr(getattr(replied_message, "author", None), "id", None),
            "author_is_bot": bool(getattr(getattr(replied_message, "author", None), "bot", False)),
            "is_ai_response": bool(replied_message and self._is_chat_response_message(getattr(replied_message, "id", 0))),
            "is_slash_output": self._is_slash_command_response_message(replied_message),
            "is_watch_update": watch is not None,
            "watch_fixture_id": watch.fixture_id if watch is not None else None,
            "watch_fixture_label": watch.fixture_label if watch is not None else None,
        }

    def _route_lease_metadata(self, message: discord.Message) -> dict[str, object]:
        lease = self._valid_lease_for_message(message)
        if lease is None:
            return {"active": False}
        return {
            "active": True,
            "owner_user_id": lease.owner_user_id,
            "last_user_message_id": lease.last_user_message_id,
            "last_bot_response_id": lease.last_bot_response_id,
            "last_action": lease.last_action,
            "resolved_request": lease.resolved_request,
        }

    def _route_missed_response_metadata(self, message: discord.Message) -> dict[str, object]:
        candidate = self._valid_missed_response_candidate(message)
        if candidate is None:
            return {"active": False}
        return {
            "active": True,
            "message_id": candidate.message_id,
            "anchor_type": candidate.anchor_type,
            "reason": candidate.reason,
            "snippet": candidate.snippet,
        }

    @staticmethod
    def _route_image_metadata(image_context: ChatImageContext) -> dict[str, object]:
        return {
            "has_supported_image": bool(image_context.urls),
            "supported_image_urls": image_context.urls[:4],
            "from_replied_message": image_context.from_replied_message,
            "current_count": len(image_context.current_message_images),
            "reply_target_count": len(image_context.reply_target_images),
            "prior_branch_count": len(image_context.prior_branch_images),
            "preferred_source_kind": image_context.preferred_source_kind,
            "source_message_id": image_context.source_message_id,
        }

    def _log_route_decision(
        self,
        message: discord.Message,
        anchor_type: str,
        decision: RouteDecision,
    ) -> None:
        logging.info(
            "AI route guild=%s channel=%s message=%s author=%s anchor=%s participation=%s action=%s p_conf=%.2f a_conf=%.2f reason=%s valid=%s failure=%s failure_reason=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            getattr(message.author, "id", None),
            anchor_type,
            decision.participation,
            decision.action,
            decision.participation_confidence,
            decision.action_confidence,
            decision.reason_code,
            decision.valid,
            decision.failure,
            decision.failure_reason,
        )

    def _log_shadow_route_decision(
        self,
        message: discord.Message,
        anchor_type: str,
        decision: RouteDecision,
    ) -> None:
        logging.info(
            "AI route shadow guild_hash=%s channel_hash=%s message_hash=%s author_hash=%s anchor=%s participation=%s action=%s p_conf=%.2f a_conf=%.2f reason=%s would_respond=%s",
            self._anon_id(getattr(message.guild, "id", None)),
            self._anon_id(getattr(message.channel, "id", None)),
            self._anon_id(getattr(message, "id", None)),
            self._anon_id(getattr(message.author, "id", None)),
            anchor_type,
            decision.participation,
            decision.action,
            decision.participation_confidence,
            decision.action_confidence,
            decision.reason_code,
            self._route_allows_response(anchor_type, decision),
        )

    @staticmethod
    def _anon_id(value: object) -> str:
        return hex(abs(hash(str(value))) % 0xFFFFFF)[2:].zfill(6)

    @staticmethod
    def _route_clarification_text(decision: RouteDecision, lang: str) -> str:
        if decision.action == "CLARIFY" and decision.resolved_request:
            return tr(
                lang,
                f"I need one more detail before I do that: {decision.resolved_request}",
                f"Necesito un detalle mas antes de hacer eso: {decision.resolved_request}",
            )
        return tr(
            lang,
            "I need one more detail before I do that. What exactly should I make or analyze?",
            "Necesito un detalle mas antes de hacer eso. Que exactamente hago o analizo?",
        )

    @staticmethod
    def _apply_resolved_request_context(prompt: str, decision: RouteDecision) -> str:
        if not decision.resolved_request or decision.action not in {"CHAT", "ANALYZE_IMAGE", "CLARIFY"}:
            return prompt
        return f"{prompt}\n\n[UNTRUSTED_RESOLVED_REQUEST]\n{decision.resolved_request}"

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
        cleaned = " ".join(content.strip().split())
        if not cleaned:
            return None
        if len(cleaned) > self._history_entry_max_chars:
            cleaned = cleaned[: self._history_entry_max_chars - 3].rstrip() + "..."
        prefixed = f"{speaker}: {cleaned}"
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
    ) -> int | None:
        parts = self._split_for_discord(text, limit=1900)
        if not parts:
            return None
        allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
        if send_mode is None:
            send_mode = "reply_to_trigger" if (True if reply_to_trigger is None else reply_to_trigger) else "normal"
        should_reply = send_mode in {"reply_to_trigger", "force_reply"}
        if send_mode == "force_channel":
            should_reply = False
        if should_reply:
            try:
                first = await trigger_message.reply(
                    parts[0],
                    mention_author=mention_author,
                    allowed_mentions=allowed_mentions,
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
                    allowed_mentions=allowed_mentions,
                )
        else:
            first = await trigger_message.channel.send(
                parts[0],
                allowed_mentions=allowed_mentions,
            )
        self._remember_chat_response_message(first.id)
        self._record_channel_ai_activity(trigger_message)
        for part in parts[1:]:
            extra = await trigger_message.channel.send(
                part,
                allowed_mentions=allowed_mentions,
            )
            self._remember_chat_response_message(extra.id)
        return int(first.id)

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

    def _sanitize_visible_ai_output(self, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return cleaned

        cleaned = re.sub(
            r"\[\s*UNTRUSTED[^\]]*\]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"(?im)^\s*UNTRUSTED[_\s-]*(?:USER|ASSISTANT|TRANSLATION|MENTION|RELAY)[^\n]*\n?",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"\bUNTRUSTED_(?:USER|ASSISTANT|TRANSLATION|MENTION|RELAY)[A-Z_]*(?:\s+FROM\s+\S+)?\b\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\[\s*/?\s*TRUSTED_FOOTBALL_DATA\s*\]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\bTRUSTED_FOOTBALL_DATA\b\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"\[\s*/?\s*TRUSTED_WEB_RESULTS\s*\]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\bTRUSTED_WEB_RESULTS\b\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:web_search|x_search)_tool\b\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"(?i)\b(?:according to|based on)\s+(?:my\s+)?sources[:,]?\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?i)\bseg[uú]n\s+(?:mis\s+)?fuentes[:,]?\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?i)\bdatos\s+confiables\s+de\s+f[uú]tbol\b",
            "datos de futbol",
            cleaned,
        )
        cleaned = re.sub(
            r"(?i)\btrusted\s+(?:football\s+)?data\b",
            "football data",
            cleaned,
        )
        cleaned = re.sub(r"(?i)\bunavailable\b", "not showing right now", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
        return self._strip_bot_speaker_prefix(cleaned)

    def _strip_bot_speaker_prefix(self, text: str) -> str:
        cleaned = text.strip().strip("`")
        names = {
            "nitori",
            "nitori-buchona",
            "nitori kawashiro",
            "bot",
        }
        if self.bot.user is not None:
            if self.bot.user.name:
                names.add(self.bot.user.name)
            if self.bot.user.display_name:
                names.add(self.bot.user.display_name)

        pattern_names = [name.strip() for name in names if name and name.strip()]
        if not pattern_names:
            return cleaned
        pattern_names.sort(key=len, reverse=True)
        joined = "|".join(re.escape(name) for name in pattern_names)
        prefix_re = re.compile(
            rf"^(?:\*\*)?(?:{joined})(?:\*\*)?\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        while True:
            updated = prefix_re.sub("", cleaned, count=1).lstrip()
            if updated == cleaned:
                break
            cleaned = updated

        # Remove in-line speaker labels like "Nitori:" that models may inject mid-message.
        inline_label_re = re.compile(
            rf"(?:(?<=\s)|^)(?:\*\*)?(?:{joined})(?:\*\*)?\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        cleaned = inline_label_re.sub(" ", cleaned)

        if self.bot.user is not None:
            mention_label_re = re.compile(
                rf"<@!?{self.bot.user.id}>\s*[:\-]\s*",
                flags=re.IGNORECASE,
            )
            cleaned = mention_label_re.sub("", cleaned)

            for bot_name in (self.bot.user.name, self.bot.user.display_name):
                if not bot_name:
                    continue
                plain_at_label_re = re.compile(
                    rf"@{re.escape(bot_name)}\s*[:\-]\s*",
                    flags=re.IGNORECASE,
                )
                cleaned = plain_at_label_re.sub("", cleaned)

        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned

    @staticmethod
    def _split_for_discord(text: str, *, limit: int) -> list[str]:
        cleaned = text.strip()
        if not cleaned:
            return []
        chunks: list[str] = []
        remaining = cleaned
        while len(remaining) > limit:
            cut = remaining.rfind("\n", 0, limit)
            if cut < int(limit * 0.5):
                cut = remaining.rfind(" ", 0, limit)
            if cut < int(limit * 0.5):
                cut = limit
            piece = remaining[:cut].rstrip()
            if piece:
                chunks.append(piece)
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _dearm_mass_mentions(text: str) -> str:
        if not text:
            return text
        updated = re.sub(r"@(?=everyone\b)", "@\u200beveryone", text, flags=re.IGNORECASE)
        updated = re.sub(r"@(?=here\b)", "@\u200bhere", updated, flags=re.IGNORECASE)
        return updated

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

    def _allowed_by_cooldown(self, message: discord.Message) -> bool:
        return self._allowed_by_throttle(message, "COMMAND")

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
        key = (message.guild.id, message.channel.id, message.author.id)  # type: ignore[union-attr]
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

    async def _normalize_discord_references(
        self, text: str, guild: discord.Guild | None
    ) -> str:
        if guild is None:
            return text

        user_prefixed = re.compile(r"(?<!<)@(\d{15,22})(?!>)")
        channel_prefixed = re.compile(r"(?<!<)#(\d{15,22})(?!>)")
        user_name_prefixed = re.compile(r"(?<![<\w])@([^\s@#<>]{1,64})")
        channel_name_prefixed = re.compile(r"(?<![<\w])#([^\s@#<>]{1,100})")
        malformed_user_angle = re.compile(r"<@!?([^\s@#<>]{1,64})>")
        malformed_user_angle_loose = re.compile(r"<@!?([^<>]{1,80})>")
        malformed_channel_angle = re.compile(r"<#([^\s@#<>]{1,100})>")
        bare_id = re.compile(r"(?<![@#<])\b(\d{15,22})\b(?!>)")

        text = user_prefixed.sub(lambda m: f"<@{m.group(1)}>", text)
        text = channel_prefixed.sub(lambda m: f"<#{m.group(1)}>", text)

        member_map: dict[str, int] = {}
        member_compact_candidates: list[tuple[str, int]] = []
        seen_compact_pairs: set[tuple[str, int]] = set()

        def register_member_candidate(member_id: int, candidate: str | None) -> None:
            if not isinstance(candidate, str):
                return
            direct_key = self._normalize_lookup_key(candidate)
            if direct_key and direct_key not in member_map:
                member_map[direct_key] = member_id
            compact_key = self._compact_lookup_key(candidate)
            if compact_key:
                if compact_key not in member_map:
                    member_map[compact_key] = member_id
                pair = (compact_key, member_id)
                if pair not in seen_compact_pairs:
                    seen_compact_pairs.add(pair)
                    member_compact_candidates.append(pair)

        for member in guild.members:
            candidates = [member.name, member.display_name]
            global_name = getattr(member, "global_name", None)
            if isinstance(global_name, str):
                candidates.append(global_name)
            for candidate in candidates:
                register_member_candidate(member.id, candidate)

        channel_map: dict[str, int] = {}
        for channel in guild.text_channels:
            direct_key = self._normalize_lookup_key(channel.name)
            if direct_key and direct_key not in channel_map:
                channel_map[direct_key] = channel.id
            compact_key = self._compact_lookup_key(channel.name)
            if compact_key and compact_key not in channel_map:
                channel_map[compact_key] = channel.id

        def fuzzy_member_id(compact_query: str) -> int | None:
            if len(compact_query) < 3:
                return None
            by_member_id: dict[int, float] = {}
            for compact_key, member_id in member_compact_candidates:
                base = difflib.SequenceMatcher(None, compact_query, compact_key).ratio()
                bonus = 0.0
                if compact_query == compact_key:
                    bonus += 0.45
                if compact_query in compact_key:
                    bonus += 0.25
                elif compact_key in compact_query:
                    bonus += 0.10
                if compact_key.startswith(compact_query) or compact_query.startswith(compact_key):
                    bonus += 0.08
                score = base + bonus
                if score > by_member_id.get(member_id, 0.0):
                    by_member_id[member_id] = score

            if not by_member_id:
                return None
            ranked = sorted(by_member_id.items(), key=lambda kv: kv[1], reverse=True)
            best_id, best_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            if best_score >= 0.78 and (best_score - second_score >= 0.07 or best_score >= 1.05):
                return best_id
            return None

        def resolve_member_id_from_token(token_raw: str) -> int | None:
            for candidate in self._candidate_user_queries(token_raw):
                direct_key = self._normalize_lookup_key(candidate)
                compact_key = self._compact_lookup_key(candidate)
                resolved = member_map.get(direct_key) or member_map.get(compact_key)
                if resolved is not None:
                    return resolved
                if compact_key:
                    fuzzy = fuzzy_member_id(compact_key)
                    if fuzzy is not None:
                        if direct_key:
                            member_map[direct_key] = fuzzy
                        if compact_key:
                            member_map[compact_key] = fuzzy
                        return fuzzy
            return None

        unresolved_user_tokens: set[str] = set()
        for match in user_name_prefixed.finditer(text):
            token_core, _ = self._split_token(match.group(1))
            if not token_core:
                continue
            if token_core.lower() in {"everyone", "here"}:
                continue
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            if direct_key and direct_key in member_map:
                continue
            if compact_key and compact_key in member_map:
                continue
            unresolved_user_tokens.add(token_core)
        for match in malformed_user_angle.finditer(text):
            token = match.group(1)
            if token.isdigit():
                continue
            token_core, _ = self._split_token(token)
            if not token_core:
                continue
            if token_core.lower() in {"everyone", "here"}:
                continue
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            if direct_key and direct_key in member_map:
                continue
            if compact_key and compact_key in member_map:
                continue
            unresolved_user_tokens.add(token_core)
        for match in malformed_user_angle_loose.finditer(text):
            token = match.group(1).strip()
            if not token or token.isdigit():
                continue
            unresolved_user_tokens.add(token)

        for token in list(unresolved_user_tokens)[:6]:
            resolved_id = resolve_member_id_from_token(token)
            if resolved_id is None:
                resolved_id = await self._query_member_id(guild, token)
            if resolved_id is None:
                continue
            for candidate in self._candidate_user_queries(token):
                register_member_candidate(resolved_id, candidate)

        def resolve_user_name(match: re.Match[str]) -> str:
            token = match.group(1)
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            if token_core.lower() in {"everyone", "here"}:
                return match.group(0)
            member_id = resolve_member_id_from_token(token_core)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>{suffix}"

        def resolve_channel_name(match: re.Match[str]) -> str:
            token = match.group(1)
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            channel_id = channel_map.get(direct_key) or channel_map.get(compact_key)
            if channel_id is None:
                return match.group(0)
            return f"<#{channel_id}>{suffix}"

        def resolve_malformed_user(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.isdigit():
                return f"<@{token}>"
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            if token_core.lower() in {"everyone", "here"}:
                return match.group(0)
            member_id = resolve_member_id_from_token(token_core)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>{suffix}"

        def resolve_malformed_user_loose(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            if not token:
                return match.group(0)
            if token.isdigit():
                return f"<@{token}>"
            member_id = resolve_member_id_from_token(token)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>"

        def resolve_malformed_channel(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.isdigit():
                return f"<#{token}>"
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            channel_id = channel_map.get(direct_key) or channel_map.get(compact_key)
            if channel_id is None:
                return match.group(0)
            return f"<#{channel_id}>{suffix}"

        text = user_name_prefixed.sub(resolve_user_name, text)
        text = channel_name_prefixed.sub(resolve_channel_name, text)
        text = malformed_user_angle.sub(resolve_malformed_user, text)
        text = malformed_user_angle_loose.sub(resolve_malformed_user_loose, text)
        text = malformed_channel_angle.sub(resolve_malformed_channel, text)

        def resolve_bare(match: re.Match[str]) -> str:
            raw = match.group(1)
            try:
                snowflake = int(raw)
            except ValueError:
                return raw

            channel = guild.get_channel(snowflake)
            if channel is None and hasattr(guild, "get_thread"):
                channel = guild.get_thread(snowflake)  # type: ignore[assignment]
            if channel is not None:
                return f"<#{snowflake}>"

            member = guild.get_member(snowflake)
            if member is not None:
                return f"<@{snowflake}>"
            return raw

        text = bare_id.sub(resolve_bare, text)
        text = self._normalize_custom_emoji_mentions(text, guild)
        return self._dedupe_repeated_mentions(text)

    @staticmethod
    def _normalize_custom_emoji_mentions(text: str, guild: discord.Guild) -> str:
        emojis: list[discord.Emoji] = [
            emoji for emoji in guild.emojis if emoji.available and emoji.is_usable()
        ]
        if not emojis:
            return text

        emoji_map: dict[str, discord.Emoji] = {}
        emoji_by_id: dict[int, discord.Emoji] = {}
        for emoji in emojis:
            key = AIChatCog._normalize_lookup_key(emoji.name)
            if key and key not in emoji_map:
                emoji_map[key] = emoji
            emoji_by_id[emoji.id] = emoji

        if not emoji_map and not emoji_by_id:
            return text

        full_emoji = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,22})>")
        malformed_angle = re.compile(r"<(a?):([A-Za-z0-9_]{2,32})>")
        shortcode = re.compile(r"(?<!<):([A-Za-z0-9_]{2,32}):(?!\d)")

        def to_emoji_token(emoji: discord.Emoji) -> str:
            prefix = "a" if emoji.animated else ""
            return f"<{prefix}:{emoji.name}:{emoji.id}>"

        def resolve_full_emoji(match: re.Match[str]) -> str:
            name = match.group(1)
            raw_id = match.group(2)
            try:
                emoji_id = int(raw_id)
            except ValueError:
                return match.group(0)

            emoji = emoji_by_id.get(emoji_id)
            if emoji is None:
                key = AIChatCog._normalize_lookup_key(name)
                emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        def resolve_malformed_angle(match: re.Match[str]) -> str:
            name = match.group(2)
            key = AIChatCog._normalize_lookup_key(name)
            emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        def resolve_shortcode(match: re.Match[str]) -> str:
            name = match.group(1)
            key = AIChatCog._normalize_lookup_key(name)
            emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        text = full_emoji.sub(resolve_full_emoji, text)
        text = malformed_angle.sub(resolve_malformed_angle, text)
        text = shortcode.sub(resolve_shortcode, text)
        return text

    @staticmethod
    async def _query_member_id(guild: discord.Guild, token: str) -> int | None:
        queries = AIChatCog._candidate_user_queries(token)
        if not queries:
            return None

        for query in queries:
            cached = guild.get_member_named(query)
            if cached is not None:
                return cached.id

            try:
                matches = await guild.query_members(query=query, limit=8)
            except (discord.Forbidden, discord.HTTPException):
                continue

            if not matches:
                continue

            direct_key = AIChatCog._normalize_lookup_key(query)
            compact_key = AIChatCog._compact_lookup_key(query)
            for member in matches:
                for candidate in (
                    member.name,
                    member.display_name,
                    getattr(member, "global_name", None),
                ):
                    if not isinstance(candidate, str):
                        continue
                    if AIChatCog._normalize_lookup_key(candidate) == direct_key:
                        return member.id
                    if AIChatCog._compact_lookup_key(candidate) == compact_key:
                        return member.id
            return matches[0].id
        return None

    @staticmethod
    def _normalize_lookup_key(value: str) -> str:
        collapsed = " ".join(value.strip().casefold().split())
        if not collapsed:
            return ""
        normalized = unicodedata.normalize("NFKD", collapsed)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _compact_lookup_key(value: str) -> str:
        base = AIChatCog._normalize_lookup_key(value)
        if not base:
            return ""
        return re.sub(r"[^a-z0-9]+", "", base)

    @staticmethod
    def _split_token(token: str) -> tuple[str, str]:
        match = re.match(r"^([\w.-]{1,64})(.*)$", token, flags=re.UNICODE)
        if match:
            return match.group(1), match.group(2)
        trimmed = token.rstrip(".,!?;:)]}'\"")
        if not trimmed:
            return token, ""
        return trimmed, token[len(trimmed) :]

    @staticmethod
    def _candidate_user_queries(raw: str) -> list[str]:
        cleaned = " ".join(raw.strip().strip("@").split())
        if not cleaned:
            return []

        stopwords = {
            "whoever",
            "someone",
            "user",
            "usuario",
            "member",
            "miembro",
            "named",
            "name",
            "nombre",
            "called",
            "llamado",
            "llamada",
            "the",
            "el",
            "la",
            "al",
            "to",
            "a",
        }

        candidates: list[str] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            item = " ".join(value.strip().split())
            if not item:
                return
            key = item.casefold()
            if key in seen:
                return
            seen.add(key)
            candidates.append(item)

        push(cleaned)
        tokens = re.findall(r"[A-Za-z0-9_.\-']{2,}", cleaned)
        filtered = [tok for tok in tokens if tok.casefold() not in stopwords]
        if filtered:
            push(" ".join(filtered))
            push(filtered[-1])
            push(filtered[0])
            for tok in filtered:
                push(tok)
        return candidates[:8]

    @staticmethod
    def _dedupe_repeated_mentions(text: str) -> str:
        user_dup = re.compile(r"(<@!?(\d{15,22})>)([\s\.,;:!?-]{0,10})<@!?\2>")
        channel_dup = re.compile(r"(<#(\d{15,22})>)([\s\.,;:!?-]{0,10})<#\2>")
        while True:
            updated, user_count = user_dup.subn(r"\1\3", text)
            updated, channel_count = channel_dup.subn(r"\1\3", updated)
            text = updated
            if user_count + channel_count == 0:
                break
        return text

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
    def _normalize_target_language(raw_language: str) -> str | None:
        normalized = raw_language.strip().lower()
        normalized = normalized.replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        return TRANSLATE_LANGUAGE_ALIASES.get(normalized)

    @staticmethod
    def _translate_usage_message(lang: str) -> str:
        choices = ", ".join(SUPPORTED_TRANSLATE_LANGUAGES)
        return tr(
            lang,
            (
                "**Supported translation languages**\n"
                f"`{choices}`\n"
                "Use:\n"
                "- `@Bot english` (replying to a message)\n"
                "- `/translate <language> [text]`"
            ),
            (
                "**Idiomas soportados para traduccion**\n"
                f"`{choices}`\n"
                "Usa:\n"
                "- `@Bot english` (respondiendo a un mensaje)\n"
                "- `/translate <idioma> [texto]`"
            ),
        )

    def _is_same_language(self, text: str, target_language: str) -> bool:
        detected = self._detect_text_language(text)
        return detected == target_language

    @staticmethod
    def _detect_text_language(text: str) -> str | None:
        raw = text.strip()
        if not raw:
            return None

        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff]", raw):
            return "japanese"
        if re.search(r"[\u0400-\u04FF]", raw):
            return "russian"

        lowered = raw.lower()
        tokens = re.findall(r"[a-zA-Z']+", lowered)
        if len(tokens) < 2:
            return None

        stopwords: dict[str, set[str]] = {
            "english": {"the", "and", "you", "are", "is", "this", "that", "with", "for", "what", "how"},
            "spanish": {"el", "la", "los", "las", "que", "como", "con", "para", "por", "estoy", "estas"},
            "german": {"der", "die", "das", "und", "ist", "ich", "nicht", "mit", "du", "wie", "was"},
            "french": {"le", "la", "les", "et", "est", "que", "avec", "pour", "pas", "comment", "quoi"},
            "italian": {"il", "lo", "la", "gli", "le", "e", "che", "con", "per", "come", "sono"},
            "portuguese": {"o", "a", "os", "as", "e", "que", "com", "para", "como", "voce"},
        }

        scores = {lang: 0.0 for lang in stopwords.keys()}
        for token in tokens:
            for lang, words in stopwords.items():
                if token in words:
                    scores[lang] += 1.0

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            return None
        best_lang, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < 2.0:
            return None
        if best_score - second_score < 1.0:
            return None
        return best_lang

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIChatCog(bot))
