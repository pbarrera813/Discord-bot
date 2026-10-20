from __future__ import annotations

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

    class _LLM:
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


class ArtifactDeliveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_message_delivers_only_declared_repeated_image_sinks(self) -> None:
        tasks = [
            {
                "task_id": task_id,
                "name": "IMAGE_GENERATE",
                "required": True,
                "evidence_span": f"generate image {index}",
                "depends_on": [],
                "scope": "IMAGE_GENERATION",
                "independent": True,
                "independent_research": False,
                "owner_action": None,
                "operands": {"prompt": f"image prompt {index}"},
                "input_bindings": [],
                "source_refs": [],
                "requested_outputs": ["artifact"],
            }
            for index, task_id in enumerate(("image_a", "image_b"), start=1)
        ]
        sinks = [
            {"upstream_task_id": task_id, "output_field": "artifact"}
            for task_id in ("image_a", "image_b")
        ]
        route = _multi_image_route(tasks=tasks, artifacts=sinks)
        cog, message, llm = _artifact_lifecycle_cog(route)
        contexts = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: contexts.append(context)
        published = []
        original_publish = cog._publish_typed_continuation_context

        def capture_publication(*args, **kwargs):
            published.append(kwargs["execution_context"])
            return original_publish(*args, **kwargs)

        cog._publish_typed_continuation_context = capture_publication

        await cog.on_message(message)

        self.assertEqual(llm.generated, ["image prompt 1", "image prompt 2"])
        self.assertEqual(len(message.channel.sends[0]["files"]), 2)
        self.assertEqual(
            [data for _filename, data in message.channel.sends[0]["files"]],
            [b"image-bytes-1", b"image-bytes-2"],
        )
        filenames = [filename for filename, _data in message.channel.sends[0]["files"]]
        self.assertEqual(len(set(filenames)), 2)
        self.assertEqual(len(message.channel.sends), 2)
        self.assertTrue(contexts)
        self.assertEqual(contexts[0].bundle.by_task["image_a"].payload["artifact"], b"image-bytes-1")
        self.assertEqual(contexts[0].bundle.by_task["image_b"].payload["artifact"], b"image-bytes-2")
        self.assertNotIn("media_urls", contexts[0].bundle.by_task["image_a"].payload)
        self.assertEqual(len(published), 1)
        self.assertIsNot(published[0], contexts[0])
        self.assertEqual(
            published[0].results["image_a"].payload["media_urls"],
            (message.channel.sends[0]["attachments"][0].url,),
        )


    async def test_on_message_fails_closed_for_untrusted_artifact_provenance(self) -> None:
        task = {
            "task_id": "image_a", "name": "IMAGE_GENERATE", "required": True,
            "evidence_span": "generate image", "depends_on": [], "scope": "IMAGE_GENERATION",
            "independent": True, "independent_research": False, "owner_action": None,
            "operands": {"prompt": "image prompt"}, "input_bindings": [], "source_refs": [],
            "requested_outputs": ["artifact"],
        }
        route = _multi_image_route(
            tasks=[task], artifacts=[{"upstream_task_id": "image_a", "output_field": "artifact"}],
        )
        for provenance in (
            {"source": "unexpected_source", "execution_path": "image_generate"},
            {"source": "generate_image", "execution_path": "image_generate", "authorized": False},
        ):
            with self.subTest(provenance=provenance):
                cog, message, _llm = _artifact_lifecycle_cog(route)

                async def bad_artifact(*_args, **_kwargs):
                    return cog._image_artifact_terminal_result(
                        "IMAGE_GENERATE", b"provider-bytes", source="generate_image",
                        filename="nitori_generated.png", provenance=provenance,
                    )

                cog._execute_image_generation_result_only = bad_artifact
                published = []
                cog._publish_typed_continuation_context = lambda *args, **kwargs: published.append(kwargs)

                await cog.on_message(message)

                self.assertEqual(message.channel.sends[0]["files"], [])
                self.assertEqual(published, [])


    async def test_on_message_rejects_invalid_attachment_metadata(self) -> None:
        tasks = [
            {
                "task_id": task_id, "name": "IMAGE_GENERATE", "required": True,
                "evidence_span": f"generate image {index}", "depends_on": [], "scope": "IMAGE_GENERATION",
                "independent": True, "independent_research": False, "owner_action": None,
                "operands": {"prompt": f"image prompt {index}"}, "input_bindings": [], "source_refs": [],
                "requested_outputs": ["artifact"],
            }
            for index, task_id in enumerate(("image_a", "image_b"), start=1)
        ]
        sinks = [{"upstream_task_id": task_id, "output_field": "artifact"} for task_id in ("image_a", "image_b")]
        for fault in ("missing", "filename", "size", "id", "url", "malformed_url", "message_id", "content_type", "second_url"):
            with self.subTest(fault=fault):
                route = _multi_image_route(tasks=tasks, artifacts=sinks)
                cog, message, _llm = _artifact_lifecycle_cog(route, metadata_fault=fault)
                published = []
                cog._publish_typed_continuation_context = lambda *args, **kwargs: published.append(kwargs)
                captured_receipts = []
                original_send = cog._send_long_reply

                async def observe_send(*args, **kwargs):
                    callback = kwargs.get("on_first_delivery")
                    if callback is not None:
                        def observe_receipt(sent_message):
                            result = callback(sent_message)
                            captured_receipts.append(result)
                            return result
                        kwargs["on_first_delivery"] = observe_receipt
                    return await original_send(*args, **kwargs)

                cog._send_long_reply = observe_send

                await cog.on_message(message)

                self.assertEqual(published, [])
                if fault == "second_url":
                    self.assertEqual(set(captured_receipts[0]), {"image_a"})
                if fault == "message_id":
                    self.assertEqual(captured_receipts[0], {})

    async def test_first_artifact_receipt_precedes_later_text_failure(self) -> None:
        tasks = [
            {
                "task_id": task_id, "name": "IMAGE_GENERATE", "required": True,
                "evidence_span": f"generate image {index}", "depends_on": [], "scope": "IMAGE_GENERATION",
                "independent": True, "independent_research": False, "owner_action": None,
                "operands": {"prompt": f"image prompt {index}"}, "input_bindings": [], "source_refs": [],
                "requested_outputs": ["artifact"],
            }
            for index, task_id in enumerate(("image_a", "image_b"), start=1)
        ]
        sinks = [{"upstream_task_id": task_id, "output_field": "artifact"} for task_id in ("image_a", "image_b")]
        route = _multi_image_route(tasks=tasks, artifacts=sinks)
        cog, message, _llm = _artifact_lifecycle_cog(route, fail_send_number=2)
        events: list[str] = []
        observed_receipts = []
        terminal_contexts = []
        cog._log_capability_terminal_checkpoint = lambda _message, context: terminal_contexts.append(context)
        original_send = cog._send_long_reply

        async def observe_send(*args, **kwargs):
            callback = kwargs.get("on_first_delivery")
            if callback is not None:
                def observe_receipt(sent_message):
                    result = callback(sent_message)
                    events.append("receipt")
                    self.assertTrue(result)
                    observed_receipts.append(result)
                    return result
                kwargs["on_first_delivery"] = observe_receipt
            return await original_send(*args, **kwargs)

        cog._send_long_reply = observe_send
        published = []
        cog._publish_typed_continuation_context = lambda *args, **kwargs: published.append(kwargs)
        original_channel_send = message.channel.send

        async def observe_channel_send(*args, **kwargs):
            events.append("send")
            return await original_channel_send(*args, **kwargs)

        message.channel.send = observe_channel_send

        with self.assertLogs(level="WARNING") as logs:
            await cog.on_message(message)

        self.assertEqual(events[:3], ["send", "receipt", "send"])
        self.assertEqual(len(message.channel.sends[0]["files"]), 2)
        self.assertEqual(set(observed_receipts[0]), {"image_a", "image_b"})
        self.assertNotIn("media_urls", terminal_contexts[0].results["image_a"].payload)
        self.assertEqual(published, [])
        self.assertTrue(any("text_complete=false" in line and "image_a:" in line and "image_b:" in line for line in logs.output))


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
