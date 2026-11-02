from __future__ import annotations

from tests.test_ai_tagging import _FakeTTSClient

import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services.ai_capability_orchestrator import validate_native_capability_contract
from services.xai_client import XAIClient


def _artifact_route(*, artifacts: list[dict[str, str]]) -> dict[str, object]:
    delivery = {
        "modality": "TEXT",
        "explicit": True,
        "source": "CURRENT_MESSAGE",
        "evidence_span": "provide the generated image",
        "semantic_reason": "the user requested an image artifact",
        "failure_reason": None,
        "ambiguous_reason": None,
        "artifacts": artifacts,
    }
    return {
        "participation": "RESPOND",
        "action": "GENERATE_IMAGE",
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "IMAGE_GENERATION_REQUEST",
        "resolved_request": "generate a synthetic image",
        "send_text": True,
        "response_delivery": delivery,
        "capability_plan": {
            "objective": "generate and deliver a synthetic image",
            "semantic_source": "router",
            "semantic_status": "SUCCESS",
            "primary_action": "GENERATE_IMAGE",
            "requirements": [{
                "requirement_id": "image_delivery",
                "objective": "generate and deliver a synthetic image",
                "required": True,
                "satisfaction_criterion": "The requested image is generated and delivered.",
                "capability_owner": "IMAGE_GENERATE",
                "owner_task_id": "image_generate_task_1",
                "depends_on": [],
            }],
            "capabilities": [{
                "task_id": "image_generate_task_1",
                "name": "IMAGE_GENERATE",
                "required": True,
                "evidence_span": "generate a synthetic image",
                "depends_on": [],
                "scope": "IMAGE_GENERATION",
                "independent": True,
                "independent_research": False,
                "owner_action": None,
                "operands": {"prompt": "a synthetic image"},
                "input_bindings": [],
                "source_refs": [],
                "requested_outputs": ["artifact"],
            }],
            "response_delivery": delivery,
        },
        "valid": True,
        "failure": False,
    }


def _multi_image_route(*, tasks: list[dict[str, object]], artifacts: list[dict[str, str]]) -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "deliver the requested final images", "semantic_reason": "image delivery",
        "artifacts": artifacts,
    }
    requirements = [
        {
            "requirement_id": f"requirement_{task['task_id']}",
            "objective": str(task["evidence_span"]),
            "required": True,
            "satisfaction_criterion": "The image task completes.",
            "capability_owner": str(task["name"]),
            "owner_task_id": str(task["task_id"]),
            "depends_on": [f"requirement_{dep}" for dep in task.get("depends_on", [])],
        }
        for task in tasks
    ]
    primary_action = "EDIT_IMAGE" if any(task["name"] == "IMAGE_EDIT" for task in tasks) else "GENERATE_IMAGE"
    route = {
        "participation": "RESPOND", "action": primary_action,
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "IMAGE_GENERATION_REQUEST", "resolved_request": "deliver the requested images",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "deliver the requested images", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": primary_action,
            "requirements": requirements, "capabilities": tasks, "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    validated = XAIClient("test-key", "test-model")._validate_route_decision(
        "", has_supported_image=False, parsed=route,
    )
    if not validated.get("valid"):
        raise AssertionError(f"invalid native image fixture: {validated.get('failure_reason')}")
    return validated


class _LifecycleAttachment:
    def __init__(self, *, attachment_id: int, filename: str, size: int, url: str, content_type: str = "image/png") -> None:
        self.id = attachment_id
        self.filename = filename
        self.size = size
        self.url = url
        self.content_type = content_type


class _LifecycleSentMessage:
    def __init__(self, *, message_id: int, attachments: list[_LifecycleAttachment]) -> None:
        self.id = message_id
        self.attachments = attachments


class _LifecycleChannel:
    def __init__(self, *, fail_send_number: int | None = None, metadata_fault: str | None = None) -> None:
        self.id = 10
        self.name = "test-channel"
        self.fail_send_number = fail_send_number
        self.metadata_fault = metadata_fault
        self.sends: list[dict[str, object]] = []

    async def typing(self) -> None:
        return None

    def history(self, **_kwargs):
        async def _history():
            if False:
                yield None
        return _history()

    async def send(self, content: str, **kwargs):
        send_number = len(self.sends) + 1
        files = list(kwargs.get("files") or ())
        if kwargs.get("file") is not None:
            files.append(kwargs["file"])
        sent_files = []
        attachments = []
        for file in files:
            fp = file.fp
            position = fp.tell()
            fp.seek(0)
            data = fp.read()
            fp.seek(position)
            filename = str(file.filename)
            sent_files.append((filename, data))
            attachment_id = 7000 + len(self.sends) * 10 + len(attachments) + 1
            attachments.append(_LifecycleAttachment(
                attachment_id=attachment_id,
                filename=filename,
                size=len(data),
                url=f"https://cdn.discordapp.com/attachments/{attachment_id}/{filename}",
            ))
        if self.metadata_fault and attachments:
            attachment = attachments[0]
            if self.metadata_fault == "missing":
                attachments.clear()
            elif self.metadata_fault == "filename":
                attachment.filename = "unexpected.png"
            elif self.metadata_fault == "size":
                attachment.size += 1
            elif self.metadata_fault == "id":
                attachment.id = 0
            elif self.metadata_fault == "url":
                attachment.url = "not-a-url"
            elif self.metadata_fault == "malformed_url":
                attachment.url = "https://[invalid"
            elif self.metadata_fault == "content_type":
                attachment.content_type = "text/plain"
            elif self.metadata_fault == "second_url" and len(attachments) > 1:
                attachments[1].url = "not-a-url"
        event = {"content": content, "files": sent_files, "attachments": attachments}
        self.sends.append(event)
        if send_number == self.fail_send_number:
            raise RuntimeError("synthetic text chunk failure")
        message_id = 0 if self.metadata_fault == "message_id" else 6000 + send_number
        return _LifecycleSentMessage(message_id=message_id, attachments=attachments)


class _LifecycleMessage:
    def __init__(self, bot_user: object, *, channel: _LifecycleChannel, content: str) -> None:
        self.id = 5000
        self.author = SimpleNamespace(id=99, display_name="Pablo", name="Pablo", bot=False)
        self.content = content
        self.guild = SimpleNamespace(id=1, owner_id=None, emojis=[], text_channels=[], members=[])
        self.channel = channel
        self.mentions = [bot_user]
        self.attachments = []
        self.embeds = []
        self.reference = None
        self.webhook_id = None

    async def reply(self, content: str, **kwargs):
        return await self.channel.send(content, **kwargs)


def _artifact_lifecycle_cog(
    route: dict[str, object], *, fail_send_number: int | None = None, metadata_fault: str | None = None,
):
    bot_user = SimpleNamespace(id=42, name="Nitori", display_name="Nitori")
    channel = _LifecycleChannel(fail_send_number=fail_send_number, metadata_fault=metadata_fault)
    message = _LifecycleMessage(bot_user, channel=channel, content="<@42> generate the requested images")

    class _DB:
        async def get_or_create_guild_settings(self, _guild_id):
            return SimpleNamespace(prefix="!", language_code="en", server_context="")

        async def is_ai_channel_allowed(self, _guild_id, _channel_id):
            return True

        async def get_ai_conversation_history(self, **_kwargs):
            return []

        async def add_ai_conversation_turn(self, **_kwargs):
            return None

    class _LLM(_FakeTTSClient):
        def __init__(self) -> None:
            self.generated: list[str] = []
            self.chat_calls = 0

        async def route_ai_interaction(self, **_kwargs):
            return route

        async def generate_image(self, prompt: str) -> bytes:
            self.generated.append(prompt)
            return f"image-bytes-{len(self.generated)}".encode()

        async def edit_image(self, prompt: str, source_url: str, **_kwargs) -> bytes:
            self.generated.append(prompt)
            self.edit_source_url = source_url
            return b"edited-final-bytes"

        async def chat(self, **_kwargs):
            self.chat_calls += 1
            return "final answer " + ("x" * 2100)

    llm = _LLM()
    bot = SimpleNamespace(
        user=bot_user,
        application_id=1234,
        db=_DB(),
        llm_client=llm,
        api_football_client=None,
        is_owner_user=lambda _user: False,
    )
    return AIChatCog(bot), message, llm


class ArtifactDeliveryNormalizationTests(unittest.TestCase):


    def test_native_validator_rejects_unknown_and_duplicate_artifact_sinks(self) -> None:
        sink = {"upstream_task_id": "image_generate_task_1", "output_field": "artifact"}
        valid = _artifact_route(artifacts=[sink])
        unknown = _artifact_route(artifacts=[{
            "upstream_task_id": "image_generate_task_1", "output_field": "image",
        }])
        duplicate = _artifact_route(artifacts=[sink, sink])

        self.assertIsNone(validate_native_capability_contract({"capability_plan": valid["capability_plan"]}))
        for invalid in (unknown, duplicate):
            diagnostics: list[dict[str, object]] = []
            self.assertEqual(
                validate_native_capability_contract(
                    {"capability_plan": invalid["capability_plan"]},
                    diagnostic_sink=diagnostics,
                ),
                "inconsistent_capability_plan",
            )
            self.assertEqual(diagnostics[0]["predicate"], "delivery_artifact_reference_invalid")



class ArtifactRealImageTypeTests(unittest.TestCase):
    def test_the_artifact_declares_the_type_its_bytes_have(self) -> None:
        for image_bytes, content_type, extension in (
            (b"\x89PNG\r\n\x1a\n" + b"x" * 8, "image/png", ".png"),
            (b"\xff\xd8\xff\xe0" + b"x" * 8, "image/jpeg", ".jpg"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", ".webp"),
            (b"not-a-known-header", "image/png", ".png"),
        ):
            with self.subTest(content_type=content_type):
                result = AIChatCog._image_artifact_terminal_result(
                    "IMAGE_GENERATE", image_bytes, source="generate_image", filename="nitori_generated.png",
                )
                self.assertEqual(result.payload["content_type"], content_type)
                self.assertTrue(result.payload["filename"].endswith(extension))
