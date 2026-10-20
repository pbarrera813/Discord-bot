from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import struct
import tempfile
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any

import discord
from discord.http import Route


class ResponseModality(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class VoiceResponseDecision:
    modality: ResponseModality
    intent_detected: bool
    source: str = "current_message"
    reason: str = "no_explicit_voice_output_request"


class VoiceMessageError(RuntimeError):
    pass


class VoicePermissionError(VoiceMessageError):
    pass


@dataclass(frozen=True)
class ProcessedVoiceAudio:
    data: bytes
    duration_seconds: float
    waveform: str


ALLOWED_TTS_TAGS: frozenset[str] = frozenset(
    {
        "pause",
        "long-pause",
        "hum-tune",
        "laugh",
        "chuckle",
        "giggle",
        "cry",
        "tsk",
        "tongue-click",
        "lip-smack",
        "breath",
        "inhale",
        "exhale",
        "sigh",
    }
)

_TAG_RE = re.compile(r"\[([a-z][a-z-]{1,40})\]", flags=re.IGNORECASE)


def sanitize_tts_text(text: str, *, limit: int = 1800) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) > limit:
        raise VoiceMessageError("voice_response_too_long")

    def replace_tag(match: re.Match[str]) -> str:
        tag = match.group(1).casefold()
        if tag in ALLOWED_TTS_TAGS:
            return f"[{tag}]"
        return tag.replace("-", " ")

    return _TAG_RE.sub(replace_tag, cleaned).strip()


class VoiceAudioProcessor:
    async def process(self, source_audio: bytes, *, source_extension: str = ".mp3") -> ProcessedVoiceAudio:
        if not source_audio:
            raise VoiceMessageError("empty_tts_audio")
        suffix = source_extension if source_extension.startswith(".") else f".{source_extension}"
        with tempfile.TemporaryDirectory(prefix="nitori-voice-") as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / f"source{suffix}"
            output_path = tmp_path / "voice-message.ogg"
            source_path.write_bytes(source_audio)
            await self._run(
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                str(output_path),
            )
            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise VoiceMessageError("ffmpeg_empty_output")
            duration = await self._duration(output_path)
            waveform = await self._waveform(output_path)
            return ProcessedVoiceAudio(
                data=output_path.read_bytes(),
                duration_seconds=duration,
                waveform=waveform,
            )

    async def _duration(self, path: Path) -> float:
        out = await self._run(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        )
        try:
            duration = float(out.decode("utf-8", errors="ignore").strip())
        except ValueError as exc:
            raise VoiceMessageError("invalid_audio_duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise VoiceMessageError("invalid_audio_duration")
        return duration

    async def _waveform(self, path: Path) -> str:
        raw = await self._run(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        )
        if len(raw) < 2:
            raise VoiceMessageError("empty_waveform")
        samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) - (len(raw) % 2)])
        if not samples:
            raise VoiceMessageError("empty_waveform")
        bucket_count = min(256, max(1, len(samples) // 800))
        bucket_size = max(1, math.ceil(len(samples) / bucket_count))
        peaks: list[int] = []
        for index in range(0, len(samples), bucket_size):
            bucket = samples[index : index + bucket_size]
            peaks.append(max(abs(value) for value in bucket))
            if len(peaks) >= 256:
                break
        max_peak = max(peaks) or 1
        preview = bytes(max(0, min(255, round((peak / max_peak) * 255))) for peak in peaks)
        return base64.b64encode(preview).decode("ascii")

    async def _run(self, *args: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise VoiceMessageError(f"{args[0]}_not_found") from exc
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore").strip()[:240]
            raise VoiceMessageError(f"{args[0]}_failed:{detail}")
        return stdout


class DiscordVoiceMessageSender:
    MESSAGE_FLAG_VOICE = 1 << 13

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def send(
        self,
        channel: Any,
        audio: ProcessedVoiceAudio,
        *,
        filename: str = "voice-message.ogg",
    ) -> int:
        self._check_permissions(channel)
        http = getattr(self.bot, "http", None)
        if http is None or not hasattr(http, "request"):
            raise VoiceMessageError("discord_http_unavailable")
        if not audio.data or audio.duration_seconds <= 0 or not audio.waveform:
            raise VoiceMessageError("invalid_voice_audio")

        payload = {
            "flags": self.MESSAGE_FLAG_VOICE,
            "attachments": [
                {
                    "id": "0",
                    "filename": filename,
                    "duration_secs": float(round(audio.duration_seconds, 3)),
                    "waveform": audio.waveform,
                }
            ],
        }
        fp = BytesIO(audio.data)
        form = [
            {"name": "payload_json", "value": json.dumps(payload, separators=(",", ":"))},
            {
                "name": "files[0]",
                "value": fp,
                "filename": filename,
                "content_type": "audio/ogg",
            },
        ]
        file = discord.File(fp, filename=filename)
        try:
            response = await http.request(
                Route("POST", "/channels/{channel_id}/messages", channel_id=int(channel.id)),
                files=[file],
                form=form,
            )
        finally:
            file.close()
        message_id = int(response.get("id", 0)) if isinstance(response, dict) else 0
        if message_id <= 0:
            raise VoiceMessageError("discord_voice_message_missing_id")
        return message_id

    def _check_permissions(self, channel: Any) -> None:
        guild = getattr(channel, "guild", None)
        me = getattr(guild, "me", None) if guild is not None else None
        if me is None:
            return
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is None:
            return
        perms = permissions_for(me)
        if not bool(getattr(perms, "send_messages", False)):
            raise VoicePermissionError("missing_send_messages")
        if not bool(getattr(perms, "attach_files", False)):
            raise VoicePermissionError("missing_attach_files")
        if hasattr(perms, "send_voice_messages") and not bool(getattr(perms, "send_voice_messages", False)):
            raise VoicePermissionError("missing_send_voice_messages")
