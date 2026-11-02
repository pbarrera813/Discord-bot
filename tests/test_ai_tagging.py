from __future__ import annotations

import asyncio
import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from discord.ext import commands

from cogs.ai_chat import AIChatCog, FootballTurnContext
from cogs.fun import FunCog
from services.admin_actions import AdminActionService
from services.ai_capability_orchestrator import CapabilityPlan, CapabilityRequest, validate_native_capability_contract
from services.external_sources import (
    ExternalMediaArtifact,
    ExternalMediaSource,
    ExternalMediaRole,
    ExternalMediaRelevance,
    ExternalSourceBundle,
    ExternalSourceIntent,
    ExternalSourceOperation,
    ExternalSourceResult,
    ExternalSourceScope,
    ExternalSourceStatus,
    GroundedExternalSource,
)
from services.voice_messages import ProcessedVoiceAudio
from services.web_research import WebResearchRequest, WebResearchResult, WebResearchService
from services.xai_client import XAIClient, XAITTSAuthorizationError


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _explicit_test_plan(name: str, task_id: str, action: str, *, scope: str = "UNSPECIFIED", operands=None) -> CapabilityPlan:
    return CapabilityPlan(
        objective="synthetic test request",
        capabilities=(CapabilityRequest(name=name, task_id=task_id, required=True, scope=scope, operands=operands or {}),),
        primary_action=action,
        response_delivery={},
        external_source={},
        football={},
        semantic_source="router",
    )


def _request_value(item):
    return getattr(item, "value", item)


def _league_request_kwargs(request):
    return {
        "name": _request_value(getattr(request, "name", None)),
        "country": _request_value(getattr(request, "country", None)),
        "search": _request_value(getattr(request, "search", None)),
        "current": getattr(request, "current", None),
    }


def _team_request_kwargs(request):
    return {
        "name": _request_value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _request_value(getattr(request, "season", None)),
    }


def _player_request_kwargs(request):
    return {
        "name": _request_value(getattr(request, "name", None)),
        "league_id": getattr(request, "league_id", None),
        "season": _request_value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _player_stats_kwargs(request):
    return {
        "player_id": getattr(request, "player_id", None),
        "league_id": getattr(request, "league_id", None),
        "season": _request_value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
    }


def _injury_request_kwargs(request):
    return {
        "league_id": getattr(request, "league_id", None),
        "season": _request_value(getattr(request, "season", None)),
        "team_id": getattr(request, "team_id", None),
        "player_id": getattr(request, "player_id", None),
        "fixture_id": getattr(request, "fixture_id", None),
    }


def _transfer_request_kwargs(request):
    return {"team_id": getattr(request, "team_id", None), "player_id": getattr(request, "player_id", None)}


class _DummyMember:
    def __init__(self, *, user_id: int, name: str, display_name: str | None = None) -> None:
        self.id = user_id
        self.name = name
        self.display_name = display_name or name
        self.global_name = None


class _DummyChannel:
    def __init__(self, *, channel_id: int, name: str, position: int = 0) -> None:
        self.id = channel_id
        self.name = name
        self.position = position
        self.mention = f"<#{channel_id}>"


class _DummyAttachment:
    def __init__(self, *, url: str, content_type: str | None, filename: str | None) -> None:
        self.url = url
        self.content_type = content_type
        self.filename = filename


class _CaptureChannel:
    def __init__(self) -> None:
        self.id = 10
        self.name = "general"
        self.parent_id = None
        self.sent: list[str] = []
        self.sent_kwargs: list[dict[str, object]] = []
        self.delivered_attachment_url: str | None = None
        self.history_messages: list[object] = []
        self._next_id = 1000

    async def send(self, content: str, **kwargs):
        self.sent.append(content)
        self.sent_kwargs.append(dict(kwargs))
        self._next_id += 1
        attachments = []
        file = kwargs.get("file")
        if file is not None and self.delivered_attachment_url:
            attachments.append(
                _DummyAttachment(
                    url=self.delivered_attachment_url,
                    content_type="image/png",
                    filename=str(getattr(file, "filename", "image.png") or "image.png"),
                )
            )
        return SimpleNamespace(id=self._next_id, attachments=attachments)

    async def typing(self) -> None:
        return None

    def history(self, *, limit: int):
        async def _iter():
            for item in self.history_messages[:limit]:
                yield item

        return _iter()

    async def fetch_message(self, message_id: int):
        for item in self.history_messages:
            if getattr(item, "id", None) == message_id:
                return item
        raise RuntimeError("message not found")


class _FakeTTSClient:
    def __init__(self, *, reply: str = "ok") -> None:
        self.reply = reply
        self.tts_calls: list[str] = []
        self.chat_calls: list[dict[str, object]] = []

    async def text_to_speech(self, text: str):
        self.tts_calls.append(text)
        return b"mp3", "audio/mpeg"

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        return self.reply

    async def respond_with_evidence(self, *, response_request, **kwargs):
        answer = await self.chat(**kwargs)
        tasks = {task["task_id"]: task for task in response_request["available_tasks"]}
        assessments = []
        for requirement in response_request["requirements"]:
            task = tasks[requirement["owner_task_id"]]
            conversational = requirement["capability_owner"] in {"CHAT", "TRANSLATE"}
            usable = task["status"] in {"SUCCESS", "PARTIAL_SUCCESS", "NO_DATA"} and task["has_payload"] and task["authorized"]
            supported = usable or (conversational and task["status"] in {"SUCCESS", "PENDING"})
            assessments.append({"requirement_id": requirement["requirement_id"],
                "status": "supported" if supported else "unknown", "basis": "context" if conversational else "retrieved",
                "evidence_task_ids": [task["task_id"]] if usable else [],
                "findings": ["The requested result is available."] if supported else [],
                "missing_evidence": [] if supported else ["The requested result was unavailable."],
            })
        blocked = [row["owner_task_id"] for row in response_request["requirements"]
                   if tasks[row["owner_task_id"]]["status"] in {"PROVIDER_FAILURE", "PERMISSION_DENIED", "UNSUPPORTED", "NO_DATA"}]
        supported = all(row["status"] == "supported" for row in assessments)
        return {"decision": "answer" if supported else "limited", "stop": None if supported else {
                    "reason": "source_blocked" if blocked else "clarification_required", "task_ids": blocked},
                "answer": answer, "assessments": assessments, "reads": []}


class _FakeVoiceProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def process(self, audio: bytes, *, source_extension: str = ".mp3"):
        self.calls.append((audio, source_extension))
        return ProcessedVoiceAudio(data=b"ogg", duration_seconds=1.2, waveform="AQID")


class _FakeVoiceSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[object, ProcessedVoiceAudio]] = []

    async def send(self, channel, audio):
        self.calls.append((channel, audio))
        if self.fail:
            raise RuntimeError("discord failed")
        return 777


class _FakeExternalSourceService:
    def __init__(
        self,
        *,
        sources: tuple[GroundedExternalSource, ...] | None = None,
        bundle: ExternalSourceBundle | None = None,
    ) -> None:
        self.max_sources = 5
        self.ground_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[tuple[ExternalSourceIntent, tuple[GroundedExternalSource, ...]]] = []
        self.sources = sources or (
            GroundedExternalSource(1, "https://domain-a.test/a", ExternalSourceScope.CURRENT_MESSAGE, "current_message"),
        )
        self.bundle = bundle or ExternalSourceBundle(
            intent=ExternalSourceIntent(True, operation=ExternalSourceOperation.SUMMARIZE, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            sources=self.sources,
            results=(
                ExternalSourceResult(
                    source=self.sources[0],
                    status=ExternalSourceStatus.SUCCESS,
                    final_url=self.sources[0].original_url,
                    provider_kind="domain-a.test",
                    title="Source A",
                    text="Source A says the announcement changed the schedule.",
                    media=(
                        ExternalMediaSource(
                            url="https://cdn.domain-a.test/notice?format=jpg",
                            provenance="retrieved_page",
                            source_index=1,
                            association="GROUNDED_SOURCE_ITEM",
                            role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                            relevance=ExternalMediaRelevance.AUTHORITATIVE,
                        ),
                    ),
                ),
            ),
            selected_media=(
                ExternalMediaSource(
                    url="https://cdn.domain-a.test/notice?format=jpg",
                    provenance="retrieved_page",
                    source_index=1,
                    association="GROUNDED_SOURCE_ITEM",
                    role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                    relevance=ExternalMediaRelevance.AUTHORITATIVE,
                ),
            ),
        )

    def ground_sources(self, **kwargs):
        self.ground_calls.append(kwargs)
        return self.sources

    async def retrieve_bundle(self, *, intent, sources, **_kwargs):
        self.retrieve_calls.append((intent, sources))
        return self.bundle

    def select_vision_media(self, media_items, *, limit=None):
        target = 4 if limit is None else max(1, min(4, int(limit)))
        selected = []
        seen = set()
        for item in media_items:
            if not item.is_authoritative or item.identity_key in seen:
                continue
            seen.add(item.identity_key)
            selected.append(item)
            if len(selected) >= target:
                break
        return tuple(selected)


def _voice_response_delivery(evidence: str) -> dict[str, object]:
    return {
        "modality": "VOICE",
        "explicit": True,
        "source": "CURRENT_MESSAGE",
        "evidence_span": evidence,
        "semantic_reason": "explicit current-message voice delivery request",
    }


def _text_response_delivery(evidence: str = "") -> dict[str, object]:
    return {
        "modality": "TEXT",
        "explicit": bool(evidence),
        "source": "CURRENT_MESSAGE" if evidence else "UNSPECIFIED",
        "evidence_span": evidence,
        "semantic_reason": "explicit text delivery",
    }


def _external_source_payload(
    *,
    evidence: str = "summarize these links",
    operation: str = "SUMMARIZE",
    source_scope: str = "CURRENT_MESSAGE",
    expected_content: str = "UNKNOWN",
) -> dict[str, object]:
    return {
        "required": True,
        "operation": operation,
        "source_scope": source_scope,
        "explicit": True,
        "evidence_span": evidence,
        "expected_content": expected_content,
        "semantic_reason": "current message asks to use explicit source contents",
    }


class _DummyMessage:
    def __init__(
        self,
        *,
        attachments: list[_DummyAttachment],
        author_id: int = 99,
        author_name: str = "Pablo",
        author_bot: bool = False,
        content: str = "",
        guild_id: int = 1,
        channel_id: int = 10,
        message_id: int = 5000,
        mentions: list[object] | None = None,
        webhook_id: int | None = None,
        reference: object | None = None,
        embeds: list[object] | None = None,
        interaction: object | None = None,
        interaction_metadata: object | None = None,
        application_id: int | None = None,
        delivered_attachment_url: str | None = None,
    ) -> None:
        self.attachments = attachments
        self.author = SimpleNamespace(id=author_id, display_name=author_name, bot=author_bot)
        self.content = content
        self.id = message_id
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=channel_id)
        self.mentions = mentions or []
        self.webhook_id = webhook_id
        self.reference = reference
        self.embeds = embeds or []
        self.interaction = interaction
        self.interaction_metadata = interaction_metadata
        self.application_id = application_id
        self.delivered_attachment_url = delivered_attachment_url
        self.replies: list[tuple[str, bool | None]] = []
        self.reply_kwargs: list[dict[str, object]] = []
        self.reactions: list[object] = []
        self._next_id = 3000

    async def reply(self, content: str, **kwargs):
        self.replies.append((content, kwargs.get("mention_author")))
        self.reply_kwargs.append(dict(kwargs))
        self._next_id += 1
        attachments = []
        file = kwargs.get("file")
        if file is not None and self.delivered_attachment_url:
            attachments.append(
                _DummyAttachment(
                    url=self.delivered_attachment_url,
                    content_type="image/png",
                    filename=str(getattr(file, "filename", "image.png") or "image.png"),
                )
            )
        return SimpleNamespace(id=self._next_id, attachments=attachments)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class _DummyGuild:
    def __init__(self) -> None:
        self.id = 1
        self.members = [
            _DummyMember(user_id=100000000000000001, name="greatooau"),
            _DummyMember(user_id=100000000000000002, name="jonark_xd"),
        ]
        self.text_channels = [
            _DummyChannel(channel_id=200000000000000001, name="pruebas-bots", position=1),
            _DummyChannel(channel_id=200000000000000002, name="minecraft", position=2),
        ]
        self.emojis = []

    def get_channel(self, channel_id: int):
        for channel in self.text_channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_thread(self, channel_id: int):
        return None

    def get_member(self, user_id: int):
        for member in self.members:
            if member.id == user_id:
                return member
        return None

    def get_member_named(self, token: str):
        lowered = token.casefold()
        for member in self.members:
            if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
                return member
        return None

    async def query_members(self, *, query: str, limit: int = 5):
        lowered = query.casefold()
        matches = []
        for member in self.members:
            if lowered in member.name.casefold() or lowered in member.display_name.casefold():
                matches.append(member)
            if len(matches) >= limit:
                break
        return matches


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeInteractionResponse()
        self.followup = _FakeFollowup()


class _SayContext:
    def __init__(self, *, interaction: object | None = None) -> None:
        self.guild = SimpleNamespace(id=1)
        self.channel = _CaptureChannel()
        self.author = SimpleNamespace(id=99)
        self.interaction = interaction
        self.message = None
        self.sent: list[tuple[object, dict[str, object]]] = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=555)

    async def defer(self, *, ephemeral: bool = False) -> None:
        if self.interaction is not None:
            await self.interaction.response.defer(ephemeral=ephemeral)


class _DummyEmoji:
    def __init__(self, *, name: str, emoji_id: int, animated: bool = False) -> None:
        self.name = name
        self.id = emoji_id
        self.animated = animated
        self.available = True

    def is_usable(self) -> bool:
        return True


class _FakeResponse:
    def __init__(self, *, status: int, text: str, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._text = text
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse | list[_FakeResponse]) -> None:
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get(self, url: str, *, headers: dict[str, str] | None = None):
        self.calls.append({"url": url, "headers": headers or {}})
        return self.responses.pop(0)


class _ImageXAIClient(XAIClient):
    def __init__(self, session: _FakeSession) -> None:
        super().__init__("key", "grok-test", image_model="grok-imagine-image-quality")
        self.fake_session = session

    async def _get_session(self):
        return self.fake_session


def _route_payload(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.setdefault(
        "capability_plan",
        {
            "objective": str(payload.get("resolved_request") or ""),
            "capabilities": [],
        },
    )
    plan = result.get("capability_plan")
    if isinstance(plan, dict) and "requirements" not in plan:
        requirements = []
        capabilities = plan.get("capabilities", [])
        if isinstance(capabilities, list):
            typed_requirement_names = {
                "CHAT",
                "WEB",
                "EXTERNAL_SOURCE",
                "IMAGE_GENERATE",
                "IMAGE_EDIT",
                "IMAGE_ANALYZE",
            }
            for item in capabilities:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip().upper()
                if name not in typed_requirement_names:
                    continue
                requirement_id = name.casefold()
                depends_on = [
                    str(dependency).strip().upper().casefold()
                    for dependency in item.get("depends_on", [])
                    if isinstance(dependency, str)
                    and str(dependency).strip().upper() in typed_requirement_names
                ]
                requirements.append(
                    {
                        "requirement_id": requirement_id,
                        "objective": str(item.get("evidence_span") or name),
                        "required": bool(item.get("required", True)),
                        "satisfaction_criterion": f"{name} result",
                        "capability_owner": name,
                        "depends_on": depends_on,
                    }
                )
        result["capability_plan"] = {**plan, "requirements": requirements}
    return result


def _native_positive_route(payload: dict[str, object]) -> dict[str, object]:
    result = json.loads(json.dumps(payload))
    plan = result.get("capability_plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("capabilities"), list):
        raise AssertionError("positive native fixture requires capabilities")
    plan.setdefault("semantic_source", "router")
    plan.setdefault("semantic_status", "SUCCESS")
    capabilities = plan["capabilities"]
    task_ids: dict[str, str] = {}
    for index, item in enumerate(capabilities):
        if not isinstance(item, dict):
            raise AssertionError("positive native fixture capability must be a mapping")
        name = str(item.get("name") or "").strip().upper()
        if not name:
            raise AssertionError("positive native fixture capability requires name")
        if name in task_ids:
            raise AssertionError(f"positive builder does not support repeated {name}; declare repeated tasks explicitly")
        task_id = str(item.get("task_id") or f"{name.casefold()}_{index + 1}")
        item["task_id"] = task_id
        task_ids[name] = task_id
    output_fields = {
        "CHAT": ["answer"], "WEB": ["answer"], "EXTERNAL_SOURCE": ["results"],
        "IMAGE_GENERATE": ["artifact"], "IMAGE_EDIT": ["artifact"],
        "IMAGE_ANALYZE": ["text"], "MINECRAFT": ["result"], "MEMORY": ["result"],
        "ADMIN": ["result"], "REACTION": ["reaction"], "FOOTBALL": ["result"],
        "CLARIFICATION": ["question"],
    }
    for item in capabilities:
        name = str(item["name"]).strip().upper()
        raw_dependencies = item.get("depends_on", [])
        dependencies = [task_ids.get(str(value).strip().upper(), str(value)) for value in raw_dependencies]
        item["depends_on"] = dependencies
        item.setdefault("required", True)
        item.setdefault("owner_action", None)
        item.setdefault("independent", not dependencies)
        item.setdefault("independent_research", name == "WEB" and not dependencies)
        item.setdefault("scope", {
            "CHAT": "GENERAL_REASONING", "WEB": "BROAD_RESEARCH", "EXTERNAL_SOURCE": "EXPLICIT_SOURCE",
            "IMAGE_GENERATE": "IMAGE_GENERATION", "IMAGE_EDIT": "IMAGE_EDIT", "IMAGE_ANALYZE": "MEDIA_ANALYSIS",
            "MINECRAFT": "STRUCTURED_DATA", "MEMORY": "DISCORD_ACTION", "ADMIN": "DISCORD_ACTION",
            "CLARIFICATION": "GENERAL_REASONING",
            "REACTION": "DISCORD_ACTION", "FOOTBALL": "STRUCTURED_DATA",
        }[name])
        item.setdefault("evidence_span", str(plan.get("objective") or name))
        item.setdefault("source_refs", [])
        item.setdefault("input_bindings", [])
        item.setdefault("requested_outputs", output_fields[name])
        operands = item.get("operands")
        if operands is None:
            if name in {"CHAT", "REACTION"}:
                operands = {}
            elif name in {"IMAGE_GENERATE", "IMAGE_EDIT"}:
                operands = {"prompt": str(plan.get("objective") or item["evidence_span"])}
            elif name == "IMAGE_ANALYZE":
                operands = {"selection_count": 1}
            elif name == "WEB":
                operands = {"query": str(plan.get("objective") or item["evidence_span"])}
            else:
                raise AssertionError(f"positive native fixture must provide {name} operands")
            item["operands"] = operands
    requirements = plan.get("requirements")
    if not isinstance(requirements, list):
        raise AssertionError("positive native fixture requires explicit requirements")
    if isinstance(requirements, list):
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise AssertionError("positive native requirement must be a mapping")
            owner = str(requirement.get("capability_owner") or "").strip().upper()
            if owner in task_ids:
                requirement.setdefault("owner_task_id", task_ids[owner])
    result = _route_payload(result)
    diagnostics: list[dict[str, object]] = []
    reason = validate_native_capability_contract(result, diagnostic_sink=diagnostics)
    if reason is not None:
        raise AssertionError(f"positive native fixture rejected: {reason} {diagnostics}")
    return result


def _typed_image_plan(capability: str, objective: str) -> dict[str, object]:
    return {
        "objective": objective,
        "semantic_source": "router",
        "requirements": [
            {
                "requirement_id": capability.casefold(),
                "objective": objective,
                "required": True,
                "satisfaction_criterion": f"{capability} result",
                "capability_owner": capability,
                "depends_on": [],
            }
        ],
        "capabilities": [{"name": capability, "required": True}],
    }


def _typed_minecraft_route(address: str | None) -> dict[str, object]:
    route = {
        "participation": "RESPOND",
        "action": "MINECRAFT_SERVER_STATUS",
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST" if address else "CLARIFICATION_NEEDED",
        "resolved_request": "check the requested Minecraft server status",
        "capability_plan": {
            "objective": "check the requested Minecraft server status",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "MINECRAFT_SERVER_STATUS",
            "requirements": [{
                "requirement_id": "minecraft",
                "objective": "retrieve the Minecraft server status",
                "required": True,
                "satisfaction_criterion": "current server status",
                "capability_owner": "MINECRAFT",
                "depends_on": [],
            }],
            "capabilities": [{
                "name": "MINECRAFT",
                "required": True,
                "evidence_span": "requested Minecraft server status",
                "scope": "STRUCTURED_DATA",
                "depends_on": [],
                "owner_action": "MINECRAFT_SERVER_STATUS",
                "operands": {"address": address},
            }],
            "minecraft": {"address": address},
        },
        "valid": True,
        "failure": False,
    }
    return _native_positive_route(route)


def _typed_memory_route(
    *,
    typed_action: str,
    compatibility_action: str,
    memory: dict[str, object] | None,
) -> dict[str, object]:
    delivery = {
        "modality": "TEXT",
        "explicit": False,
        "source": "UNSPECIFIED",
        "evidence_span": "",
        "semantic_reason": "default text response",
    }
    external_source = {
        "required": False,
        "operation": "UNSPECIFIED",
        "source_scope": "UNSPECIFIED",
        "explicit": False,
        "evidence_span": "",
        "expected_content": "UNKNOWN",
        "semantic_reason": "",
    }
    route = {
        "participation": "RESPOND",
        "action": compatibility_action,
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "SERVER_MEMORY_REQUEST",
        "resolved_request": None,
        "target_message": None,
        "emoji": None,
        "emojis": [],
        "send_text": True,
        "response_delivery": delivery,
        "external_source": external_source,
        "capability_plan": {
            "objective": "Apply one bounded server-memory operation.",
            "requirements": [
                {
                    "requirement_id": "memory_operation",
                    "objective": "Apply one bounded server-memory operation.",
                    "required": True,
                    "satisfaction_criterion": "The requested memory operation completes.",
                    "capability_owner": "MEMORY",
                    "depends_on": [],
                }
            ],
            "capabilities": [
                {
                    "name": "MEMORY",
                    "required": True,
                    "evidence_span": "current typed memory request",
                    "depends_on": [],
                    "scope": "DISCORD_ACTION",
                    "independent": True,
                    "independent_research": False,
                    "owner_action": typed_action,
                    "operands": {
                        "memory_action": typed_action,
                        "memory_type": (memory or {}).get("memory_type"),
                        "subject": (memory or {}).get("subject"),
                        "key": (memory or {}).get("key"),
                        "value": (memory or {}).get("value"),
                        "scope": (memory or {}).get("scope"),
                    },
                }
            ],
            "primary_action": typed_action,
            "response_delivery": delivery,
            "external_source": external_source,
            "football": {
                "operation_family": None,
                "data_focus": None,
                "fixture_focus": None,
                "date_hint": None,
                "season_hint": None,
                "time_scope": None,
                "stat_focus": None,
                "player_candidates": [],
                "team_candidates": [],
                "league_candidates": [],
                "country_candidates": [],
            },
            "evidence_source": "CURRENT_MESSAGE",
            "valid": True,
            "failure_reason": None,
        },
        "pending_operation": None,
        "memory": memory,
        "admin": None,
    }
    return _native_positive_route(route)


def _typed_admin_plan(
    objective: str = "Perform one authorized server action.",
    *,
    admin_action: str,
    operands: dict[str, object],
    task_id: str = "admin_operation_1",
) -> dict[str, object]:
    route = _native_positive_route({
        "participation": "RESPOND",
        "action": "ADMIN_ACTION",
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "ADMIN_ACTION_REQUEST",
        "admin": {"valid": True, "admin_action": admin_action, **operands, "confidence": 1.0},
        "capability_plan": {
            "objective": objective,
            "semantic_source": "router",
            "primary_action": "ADMIN_ACTION",
            "requirements": [{
                "requirement_id": "admin_operation",
                "objective": objective,
                "required": True,
                "satisfaction_criterion": "The authorized server action completes.",
                "capability_owner": "ADMIN",
                "depends_on": [],
            }],
            "capabilities": [{
                "task_id": task_id,
                "name": "ADMIN",
                "required": True,
                "evidence_span": "current authorized request",
                "depends_on": [],
                "scope": "DISCORD_ACTION",
                "independent": True,
                "independent_research": False,
                "owner_action": "ADMIN_ACTION",
                "operands": {"admin_action": admin_action, **operands},
                "requested_outputs": ["result"],
            }],
        },
        "valid": True,
        "failure": False,
    })
    return route["capability_plan"]


def _authorized_admin_preflight(
    message: _DummyMessage,
    *,
    actor_permissions: dict[str, bool],
    bot_permissions: dict[str, bool],
    target: _DummyMember | None = None,
) -> None:
    class _PositionRole:
        def __init__(self, position: int) -> None:
            self.position = position

        def __le__(self, other: object) -> bool:
            return self.position <= getattr(other, "position", -1)

        def __ge__(self, other: object) -> bool:
            return self.position >= getattr(other, "position", -1)

    guild = message.guild
    guild.owner_id = 999
    message.author.top_role = _PositionRole(10)
    message.author.guild_permissions = SimpleNamespace(administrator=False, **actor_permissions)
    guild.me = SimpleNamespace(
        id=42,
        mention="<@42>",
        top_role=_PositionRole(30),
        guild_permissions=SimpleNamespace(administrator=False, **bot_permissions),
    )
    if target is not None:
        target.top_role = _PositionRole(1)
        guild.members = [target]


class _AdminActionHarness(AdminActionService):
    def __init__(self) -> None:
        super().__init__(SimpleNamespace(is_owner_user=lambda _user: False))


class _CaptureChatXAIClient(XAIClient):
    def __init__(self) -> None:
        super().__init__("key", "grok-test")
        self.messages: list[dict[str, object]] = []
        self.kwargs: dict[str, object] = {}

    async def _create_completion_with_retry(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return "ok"


class NativeVisualResearchTests(unittest.IsolatedAsyncioTestCase):


    async def test_typed_visual_web_request_enables_native_visual_tools(self) -> None:
        response = _FakeResponse(
            status=200,
            text=json.dumps(
                {
                    "output_text": "Current research with grounded visual findings.",
                    "citations": ["https://source-a.test/post"],
                }
            ),
        )
        session = _FakeSession(response)
        client = _ImageXAIClient(session)

        await client.web_research(
            query="research the current subject",
            enable_image_understanding=True,
            enable_image_search=True,
            visual_objective="inspect relevant visual evidence and report observations",
            visual_selection_count=1,
        )

        payload = session.calls[0]["json"]
        self.assertTrue(payload["tools"][0]["enable_image_understanding"])
        self.assertTrue(payload["tools"][0]["enable_image_search"])
        request_context = json.loads(payload["input"][1]["content"])
        self.assertEqual(
            request_context["visual_requirement"],
            "inspect relevant visual evidence and report observations",
        )
        self.assertEqual(request_context["visual_selection_count"], 1)
        self.assertIn("actual inspection of relevant visual evidence", payload["input"][0]["content"])

    async def test_current_web_request_carries_date_and_recency_contract(self) -> None:
        session = _FakeSession(_FakeResponse(status=200, text=json.dumps({"output_text": "dated result"})))
        client = _ImageXAIClient(session)

        await client.web_research(
            query="research the latest status of a synthetic subject",
            request_date="2026-08-20",
        )

        sent = session.calls[0]["json"]
        request_context = json.loads(sent["input"][1]["content"])
        self.assertEqual(request_context["request_date"], "2026-08-20")
        self.assertIn("newer authoritative dated evidence", sent["input"][0]["content"])
        self.assertIn("older forecasts", sent["input"][0]["content"])


class SayCommandVoiceTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, bot: object) -> FunCog:
        cog = object.__new__(FunCog)
        cog.bot = bot
        return cog

    async def test_say_default_text_preserves_existing_send_behavior(self) -> None:
        ctx = _SayContext(interaction=_FakeInteraction())
        cog = self._cog(SimpleNamespace())

        await FunCog.say.callback(cog, ctx, message="hello")

        self.assertEqual(ctx.channel.sent, ["hello"])
        self.assertEqual(ctx.interaction.followup.sent, [("Sent.", True)])


    async def test_say_voice_uses_tts_processor_and_native_sender(self) -> None:
        ctx = _SayContext(interaction=_FakeInteraction())
        llm = _FakeTTSClient()
        processor = _FakeVoiceProcessor()
        sender = _FakeVoiceSender()
        cog = self._cog(
            SimpleNamespace(
                llm_client=llm,
                voice_audio_processor=processor,
                discord_voice_sender=sender,
            )
        )

        await FunCog.say.callback(cog, ctx, message="hola [sigh]", mode="voice")

        self.assertEqual(ctx.channel.sent, [])
        self.assertEqual(llm.tts_calls, ["hola [sigh]"])
        self.assertEqual(processor.calls, [(b"mp3", ".mp3")])
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(ctx.interaction.followup.sent, [("Sent voice message.", True)])


    async def test_say_voice_still_requires_manage_messages_permission(self) -> None:
        check = FunCog.say.checks[0]

        with self.assertRaises(commands.MissingPermissions):
            result = check(SimpleNamespace(permissions=SimpleNamespace(manage_messages=False)))
            if hasattr(result, "__await__"):
                await result

        allowed = check(SimpleNamespace(permissions=SimpleNamespace(manage_messages=True)))
        if hasattr(allowed, "__await__"):
            allowed = await allowed
        self.assertTrue(allowed)


class AIChatTaggingTests(unittest.IsolatedAsyncioTestCase):



    def _store_test_football_context(
        self,
        cog: AIChatCog,
        message: _DummyMessage,
        *,
        dormant: bool = False,
    ) -> None:
        key = cog._football_context_key(message)
        assert key is not None
        now = time.monotonic()
        cog._football_turn_contexts[key] = FootballTurnContext(
            guild_id=key[0],
            channel_id=key[1],
            owner_user_id=key[2],
            payload={
                "team_id": 50,
                "team_name": "Club Example",
                "opponent_id": 60,
                "opponent_name": "Example FC",
                "fixture_id": 7001,
                "fixture_status": "FT",
                "operation_type": "fixture_statistics",
            },
            last_operation="FOOTBALL_MATCH_CENTER",
            source_user_message_id=100,
            source_assistant_message_id=101,
            updated_at=now,
            expires_at=now + 300,
            dormant=dormant,
        )



    async def test_passive_reply_to_slash_command_output_is_ignored(self) -> None:
        class _LLM(_FakeTTSClient):
            def __init__(self) -> None:
                self.chat_calls = 0

            async def chat(self, **_kwargs):
                self.chat_calls += 1
                return "should not send"

        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(
                SimpleNamespace(prefix="!", language_code="en", server_context="")
            ),
            is_ai_channel_allowed=_async_return(True),
        )
        llm = _LLM()
        cog = AIChatCog(SimpleNamespace(
            user=bot_user,
            application_id=1234,
            db=db,
            llm_client=llm,
            is_owner_user=lambda _user: False,
        ))
        replied = _DummyMessage(
            attachments=[],
            author_id=42,
            message_id=9001,
            interaction_metadata=object(),
            application_id=None,
        )
        message = _DummyMessage(
            attachments=[],
            content="what do you think?",
            reference=SimpleNamespace(message_id=9001),
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        async def get_replied_message(_message):
            return replied

        cog._get_replied_message = get_replied_message

        self.assertTrue(cog._is_slash_command_response_message(replied))
        await cog.on_message(message)
        self.assertEqual(llm.chat_calls, 0)
        self.assertEqual(message.replies, [])


    async def test_lease_replaced_only_after_successful_other_user_response(self) -> None:
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _LLM(_FakeTTSClient):
            async def route_ai_interaction(self, **_kwargs):
                return {
                    "participation": "RESPOND",
                    "action": "CHAT",
                    "participation_confidence": 0.1,
                    "action_confidence": 0.1,
                    "reason_code": "DIRECT_REQUEST",
                    "resolved_request": None,
                    "response_delivery": _voice_response_delivery("en un audio"),
                    "valid": True,
                    "failure": False,
                }

            async def chat(self, **_kwargs):
                return "new lease"

        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(
                SimpleNamespace(prefix="!", language_code="en", server_context="")
            ),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user,
            application_id=1234,
            db=db,
            llm_client=_LLM(),
            is_owner_user=lambda _user: False,
        ))
        old_owner = _DummyMessage(attachments=[], author_id=99, guild_id=1, channel_id=10)
        cog._create_or_renew_lease(old_owner, last_bot_response_id=600, action="CHAT")
        message = _DummyMessage(
            attachments=[],
            author_id=100,
            guild_id=1,
            channel_id=10,
            content="<@42> hola",
            mentions=[bot_user],
        )
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        await cog.on_message(message)

        lease = cog._valid_lease_for_message(message)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.owner_user_id, 100)


    async def test_ai_football_api_data_prevents_web_fallback(self) -> None:
        bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")

        class _Football:
            async def resolve_league_id(self, _league_key):
                return 262

            async def get_current_season(self, _league_id):
                return 2026

            async def get_fixtures_on_date(self, **_kwargs):
                return [
                    {
                        "fixture": {"id": 10, "date": "2026-07-09T20:00:00+00:00", "status": {"short": "NS"}},
                        "league": {"name": "Liga MX"},
                        "teams": {"home": {"name": "America"}, "away": {"name": "Tigres"}},
                    }
                ]

        class _Web:
            def __init__(self) -> None:
                self.calls = 0

            async def research(self, *_args, **_kwargs):
                self.calls += 1
                return WebResearchResult(query="", answer="", failure_reason="should_not_call")

        class _LLM(_FakeTTSClient):
            def __init__(self) -> None:
                self.chat_prompt = ""
                self.user_prompt = ""

            async def route_ai_interaction(self, **_kwargs):
                return {
                    "participation": "RESPOND",
                    "action": "FOOTBALL_WATCH_TODAY",
                    "participation_confidence": 1.0,
                    "action_confidence": 1.0,
                    "reason_code": "DIRECT_REQUEST",
                    "resolved_request": "partidos de hoy liga mx",
                    "valid": True,
                    "failure": False,
                }

            async def chat(self, **kwargs):
                self.chat_prompt = kwargs["user_message"]
                return "America vs Tigres."

        web = _Web()
        db = SimpleNamespace(
            get_or_create_guild_settings=_async_return(SimpleNamespace(prefix="!", language_code="en", server_context="")),
            is_ai_channel_allowed=_async_return(True),
            get_ai_conversation_history=_async_return([]),
            get_or_create_birthday_guild_settings=_async_return({}),
            get_announcement_settings=_async_return(SimpleNamespace(channel_id=None)),
            add_ai_conversation_turn=_async_return(None),
        )
        cog = AIChatCog(SimpleNamespace(
            user=bot_user,
            application_id=1234,
            db=db,
            llm_client=_LLM(),
            api_football_client=_Football(),
            web_research_service=web,
            is_owner_user=lambda _user: False,
        ))
        message = _DummyMessage(attachments=[], content="<@42> partidos de hoy liga mx", mentions=[bot_user])
        message.guild = _DummyGuild()
        message.channel = _CaptureChannel()

        await cog.on_message(message)

        self.assertEqual(web.calls, 0)



    async def test_ai_admin_delete_role_rejects_hierarchy(self) -> None:
        class _Role:
            def __init__(self, role_id: int, name: str, position: int) -> None:
                self.id = role_id
                self.name = name
                self.position = position
                self.mention = f"<@&{role_id}>"
                self.managed = False

            def is_default(self) -> bool:
                return False

            def __ge__(self, other):
                return self.position >= other.position

        service = AdminActionService(SimpleNamespace(db=SimpleNamespace(), is_owner_user=lambda _user: False))
        target_role = _Role(100000000000000456, "High", 50)
        actor = SimpleNamespace(id=99, mention="<@99>", top_role=SimpleNamespace(position=20), guild_permissions=SimpleNamespace(manage_roles=True, administrator=False))
        guild = SimpleNamespace(
            id=1,
            owner_id=999,
            me=SimpleNamespace(id=42, mention="<@42>", top_role=SimpleNamespace(position=60), guild_permissions=SimpleNamespace(manage_roles=True, administrator=False)),
        )

        result = await service.delete_role(guild, actor, target_role, lang="en")

        self.assertFalse(result.success)
        self.assertEqual(result.code, "actor_role_hierarchy")

    async def test_admin_action_service_creates_role_with_router_operands(self) -> None:
        class _Guild:
            def __init__(self) -> None:
                self.id = 1
                self.owner_id = 999
                self.roles: list[object] = []
                self.me = SimpleNamespace(
                    id=42,
                    mention="<@42>",
                    guild_permissions=SimpleNamespace(manage_roles=True, administrator=False),
                )
                self.create_calls: list[dict[str, object]] = []

            async def create_role(self, *, name, color, reason):
                self.create_calls.append({"name": name, "color": color, "reason": reason})
                role = SimpleNamespace(id=321, name=name, mention="<@&321>")
                self.roles.append(role)
                return role

        actor = SimpleNamespace(
            id=99,
            mention="<@99>",
            guild_permissions=SimpleNamespace(manage_roles=True, administrator=False),
        )
        guild = _Guild()
        service = AdminActionService(SimpleNamespace(db=SimpleNamespace(), is_owner_user=lambda _user: False))

        with patch("services.admin_actions.send_modlog_embed", _async_return(None)):
            result = await service.create_role(
                guild,
                actor,
                "Raid Team",
                color_hex="#22aa55",
                reason="season setup",
                lang="en",
            )

        self.assertTrue(result.success)
        self.assertEqual(result.code, "role_created")
        self.assertEqual(guild.create_calls[0]["name"], "Raid Team")
        self.assertEqual(guild.create_calls[0]["color"].value, 0x22AA55)
        self.assertIn("season setup", str(guild.create_calls[0]["reason"]))

    async def test_admin_action_service_create_role_requires_manage_roles(self) -> None:
        guild = SimpleNamespace(
            id=1,
            owner_id=999,
            roles=[],
            me=SimpleNamespace(
                id=42,
                guild_permissions=SimpleNamespace(manage_roles=True, administrator=False),
            ),
        )
        create_calls = 0

        async def create_role(**_kwargs):
            nonlocal create_calls
            create_calls += 1

        guild.create_role = create_role
        actor = SimpleNamespace(
            id=99,
            guild_permissions=SimpleNamespace(manage_roles=False, administrator=False),
        )
        service = AdminActionService(SimpleNamespace(db=SimpleNamespace(), is_owner_user=lambda _user: False))

        result = await service.create_role(guild, actor, "Raid Team", lang="en")

        self.assertFalse(result.success)
        self.assertEqual(result.code, "author_missing_manage_roles")
        self.assertEqual(create_calls, 0)



class XAIClientMessageTests(unittest.TestCase):


    def test_chat_caps_oversized_trusted_context_at_dedicated_limit(self) -> None:
        client = _CaptureChatXAIClient()
        trusted_context = "[TRUSTED_CAPABILITY_RESULTS]" + ("e" * 15000)

        asyncio.run(client.chat(
            server_context="",
            user_message="ordinary synthetic request",
            trusted_capability_context=trusted_context,
            author_name="synthetic-user",
            channel_name="general",
        ))

        bounded_context = str(client.messages[-2]["content"])
        self.assertEqual(XAIClient._MAX_TRUSTED_CAPABILITY_CONTEXT, 11000)
        self.assertEqual(len(bounded_context), 11000)
        self.assertIn("text omitted; original_chars=", bounded_context)
        self.assertNotIn(bounded_context, str(client.messages[-1]["content"]))


    def test_chat_serializes_rendered_artifact_as_existing_input_image(self) -> None:
        client = _CaptureChatXAIClient()
        artifact = ExternalMediaArtifact(data=b"rendered-png", content_type="image/png")

        asyncio.run(client.chat(
            server_context="",
            user_message="analyze this source visual",
            author_name="Pablo",
            channel_name="general",
            image_inputs=[artifact],
        ))

        content = client.messages[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "input_image")
        self.assertTrue(content[0]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(content[1]["type"], "input_text")


    def test_text_to_speech_uses_configured_voice_and_language(self) -> None:
        response = _FakeResponse(status=200, text="", body=b"mp3-bytes", headers={"Content-Type": "audio/mpeg"})
        session = _FakeSession(response)
        client = _ImageXAIClient(session)

        audio, content_type = asyncio.run(client.text_to_speech("hola [sigh]"))

        self.assertEqual(audio, b"mp3-bytes")
        self.assertEqual(content_type, "audio/mpeg")
        call = session.calls[0]
        self.assertEqual(call["url"], XAIClient.TTS_URL)
        self.assertEqual(call["json"]["voice_id"], "iris")
        self.assertEqual(call["json"]["language"], "es-MX")
        self.assertEqual(call["json"]["text"], "hola [sigh]")


    def test_text_to_speech_403_is_authorization_failure(self) -> None:
        response = _FakeResponse(
            status=403,
            text="",
            body=b'{"error":"voice endpoint permission missing"}',
            headers={"Content-Type": "application/json"},
        )
        session = _FakeSession(response)
        client = _ImageXAIClient(session)

        with self.assertRaises(XAITTSAuthorizationError):
            asyncio.run(client.text_to_speech("hola"))

    def test_generate_image_payload_and_base64_response(self) -> None:
        raw = b"fake-png"
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}
        session = _FakeSession(_FakeResponse(status=200, text=json.dumps(payload)))
        client = _ImageXAIClient(session)

        result = asyncio.run(client.generate_image("draw a forest"))

        self.assertEqual(result, raw)
        call = session.calls[0]
        self.assertEqual(call["url"], XAIClient.IMAGE_GENERATION_URL)
        sent = call["json"]
        self.assertEqual(sent["model"], "grok-imagine-image-quality")
        self.assertEqual(sent["response_format"], "b64_json")
        self.assertEqual(sent["n"], 1)


    def test_edit_image_payload_and_base64_response(self) -> None:
        raw = b"fake-edited-png"
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}
        session = _FakeSession(_FakeResponse(status=200, text=json.dumps(payload)))
        client = _ImageXAIClient(session)

        result = asyncio.run(client.edit_image("make Jeff bigger", "https://cdn.discordapp.com/source.png"))

        self.assertEqual(result, raw)
        call = session.calls[0]
        self.assertEqual(call["url"], XAIClient.IMAGE_EDIT_URL)
        sent = call["json"]
        self.assertEqual(sent["model"], "grok-imagine-image-quality")
        self.assertEqual(sent["response_format"], "b64_json")
        self.assertEqual(sent["image"]["url"], "https://cdn.discordapp.com/source.png")
        self.assertEqual(sent["image"]["type"], "image_url")

    def test_native_url_image_edit_uses_responses_tool_composition(self) -> None:
        raw = b"native-edited-png"
        payload = {
            "id": "resp-native-1",
            "status": "completed",
            "output": [
                {"type": "open_page", "status": "completed"},
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": base64.b64encode(raw).decode("ascii"),
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20, "reasoning_tokens": 5},
        }
        session = _FakeSession(_FakeResponse(status=200, text=json.dumps(payload)))
        client = _ImageXAIClient(session)

        result = asyncio.run(
            client.native_url_image_edit(
                "make the first image bigger",
                ("https://developers.google.com/speed/webp/gallery",),
            )
        )

        self.assertEqual(result["image_bytes"], raw)
        sent = session.calls[0]["json"]
        self.assertEqual(sent["model"], "grok-test")
        self.assertEqual(
            sent["tools"],
            [
                {
                    "type": "web_search",
                    "enable_image_understanding": True,
                    "enable_image_search": True,
                },
                {"type": "image_generation", "action": "edit"},
            ],
        )
        self.assertFalse(sent["store"])
        self.assertNotIn("image_model", sent)
        request_text = sent["input"][0]["content"]
        self.assertIn("Treat the URL as the starting page", request_text)
        self.assertIn("follow its links to the original or direct image", request_text)
        self.assertNotIn("first relevant image", request_text)
        self.assertIn(
            "Continue until image_generation with action=edit has returned the completed edited image artifact",
            request_text,
        )
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["image_generation_call_count"], 1)
        self.assertEqual(diagnostics["image_generation_status"], "COMPLETED")
        self.assertEqual(diagnostics["artifact_length"], len(raw))
        self.assertEqual(
            diagnostics["tool_trace"][0],
            {
                "label": "open_page",
                "type": "open_page",
                "status": "COMPLETED",
                "action_type": None,
                "source_count": 0,
            },
        )
        self.assertEqual(diagnostics["tool_trace"][2]["type"], "image_generation_call")
        self.assertEqual(diagnostics["tool_trace"][2]["status"], "COMPLETED")
        self.assertNotIn("developers.google.com", json.dumps(diagnostics))


    def test_football_player_canonicalizer_rejects_invalid_outputs(self) -> None:
        cases = [
            {"entity_type": "team", "candidate_names": ["Cristiano Ronaldo"], "confidence": 1.0},
            {"entity_type": "player", "candidate_names": [], "confidence": 1.0},
            {"entity_type": "player", "candidate_names": ["Cristiano Ronaldo"], "confidence": 0.1},
            "not json",
        ]

        for payload in cases:
            text = payload if isinstance(payload, str) else json.dumps({"output_text": json.dumps(payload)})
            session = _FakeSession(_FakeResponse(status=200, text=text))
            client = _ImageXAIClient(session)
            result = asyncio.run(
                client.canonicalize_football_player_query(
                    original_query="cr7",
                    clean_query="cr7",
                )
            )
            self.assertIsNone(result)



    def test_web_research_payload_uses_web_tool_and_max_output_tokens(self) -> None:
        response = {
            "output_text": "Current answer",
            "citations": ["https://example.com/news"],
            "output": [
                {
                    "content": [
                        {
                            "sources": [
                                {
                                    "url": "https://example.com/news",
                                    "image_url": "https://cdn.example/image-a",
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        session = _FakeSession(_FakeResponse(status=200, text=json.dumps(response)))
        client = _ImageXAIClient(session)

        result = asyncio.run(
            client.web_research(
                query="latest football news",
                lookup_type="sports",
                max_sources=2,
                allowed_domains=["example.com"],
            )
        )

        self.assertEqual(result["answer"], "Current answer")
        self.assertEqual(result["citations"], ["https://example.com/news"])
        self.assertEqual(result["sources"][0]["media_urls"], ("https://cdn.example/image-a",))
        self.assertEqual(result["sources"][0]["media_candidates"][0]["relevance"], "AUXILIARY")
        call = session.calls[0]
        self.assertEqual(call["url"], XAIClient.BASE_URL)
        sent = call["json"]
        self.assertEqual(sent["max_output_tokens"], 700)
        self.assertNotIn("max_tokens", sent)
        self.assertEqual(sent["tools"][0]["type"], "web_search")
        self.assertEqual(sent["tools"][0]["filters"]["allowed_domains"], ["example.com"])
        self.assertNotIn("enable_image_understanding", sent["tools"][0])
        self.assertNotIn("enable_image_search", sent["tools"][0])

    def test_web_research_service_disabled_cache_and_cooldown(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.calls = 0

            async def web_research(self, **_kwargs):
                self.calls += 1
                return {
                    "answer": "answer",
                    "sources": [{"url": "https://example.com/a", "title": "A"}],
                    "citations": ["https://example.com/a"],
                    "failure_reason": None,
                    "provider_status": "COMPLETED",
                    "tool_used": "web_search",
                }

        disabled = WebResearchService(_Client(), enabled=False)
        disabled_result = asyncio.run(
            disabled.research(WebResearchRequest(query="latest thing"), guild_id=1, user_id=2)
        )
        self.assertEqual(disabled_result.failure_reason, "disabled")

        client = _Client()
        service = WebResearchService(client, enabled=True, cooldown_seconds=0, cache_ttl_seconds=60)
        first = asyncio.run(service.research(WebResearchRequest(query="latest thing"), guild_id=1, user_id=2))
        second = asyncio.run(service.research(WebResearchRequest(query="latest thing"), guild_id=1, user_id=2))
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(client.calls, 1)

        throttled = WebResearchService(_Client(), enabled=True, cooldown_seconds=30, cache_ttl_seconds=0)
        ok = asyncio.run(throttled.research(WebResearchRequest(query="a"), guild_id=1, user_id=2))
        limited = asyncio.run(throttled.research(WebResearchRequest(query="b"), guild_id=1, user_id=2))
        self.assertIsNone(ok.failure_reason)
        self.assertEqual(limited.failure_reason, "rate_limited")
        client = _Client()
        grouped = WebResearchService(client, enabled=True, cooldown_seconds=30, cache_ttl_seconds=60)
        for query, turn in (("a", 10), ("b", 10), ("a", 11), ("c", 11)):
            result = asyncio.run(grouped.research(WebResearchRequest(query=query), guild_id=1, user_id=2, turn_id=turn))
            self.assertEqual(result.failure_reason, "rate_limited" if query == "c" else None)
            if query == "a" and turn == 11:
                self.assertTrue(result.cache_hit)
        self.assertEqual(client.calls, 2)


if __name__ == "__main__":
    unittest.main()
