from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Final


MEMORY_TYPES: Final[set[str]] = {
    "USER_NICKNAME",
    "USER_ALIAS",
    "USER_PREFERENCE",
    "SERVER_FACT",
    "SERVER_RULE",
    "BOT_BEHAVIOR_RULE",
    "INSIDE_JOKE",
    "CHANNEL_CONTEXT",
    "FOOTBALL_PREFERENCE",
}
MEMORY_STATUSES: Final[set[str]] = {"active", "pending", "rejected", "archived", "expired"}
AFFIRMATIVE_RESPONSES: Final[set[str]] = {
    "si",
    "sí",
    "ok",
    "va",
    "va ta bien",
    "ta bien",
    "simon",
    "simón",
    "dale",
    "jalo",
    "no hay pedo",
    "me parece",
    "esta bien",
    "está bien",
}
NEGATIVE_RESPONSES: Final[set[str]] = {"no", "nel", "nah", "no quiero", "ni madres", "mejor no"}


@dataclass(frozen=True)
class ServerMemoryInput:
    guild_id: int
    memory_type: str
    key: str
    value: str
    created_by_user_id: int
    subject_user_id: int | None = None
    subject_channel_id: int | None = None
    source_type: str = "manual"
    source_message_id: int | None = None
    metadata: dict[str, Any] | None = None
    status: str = "active"
    required_approver_type: str | None = None
    approved_by_user_id: int | None = None
    expires_at: str | None = None
    confidence: float = 1.0


class ServerMemoryService:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def create_memory(self, data: ServerMemoryInput) -> dict[str, Any]:
        memory_type = self.normalize_memory_type(data.memory_type)
        status = self.normalize_status(data.status)
        key = self.normalize_key(data.key)
        value = self.clean_value(data.value)
        if not key:
            raise ValueError("Memory key is required.")
        if not value:
            raise ValueError("Memory value is required.")
        memory_id = await self.db.add_server_memory(
            guild_id=data.guild_id,
            memory_type=memory_type,
            subject_user_id=data.subject_user_id,
            subject_channel_id=data.subject_channel_id,
            key=key,
            value=value,
            metadata_json=json.dumps(data.metadata or {}, ensure_ascii=False),
            status=status,
            source_type=self.clean_token(data.source_type, default="manual"),
            confidence=max(0.0, min(float(data.confidence), 1.0)),
            required_approver_type=data.required_approver_type,
            created_by_user_id=data.created_by_user_id,
            approved_by_user_id=data.approved_by_user_id,
            source_message_id=data.source_message_id,
            expires_at=data.expires_at,
        )
        return await self.get_memory(data.guild_id, memory_id) or {"id": memory_id}

    async def create_pending_memory(
        self,
        data: ServerMemoryInput,
        *,
        ttl_minutes: int = 15,
        required_approver_type: str = "target_user",
    ) -> dict[str, Any]:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
        pending = ServerMemoryInput(
            guild_id=data.guild_id,
            memory_type=data.memory_type,
            key=data.key,
            value=data.value,
            created_by_user_id=data.created_by_user_id,
            subject_user_id=data.subject_user_id,
            subject_channel_id=data.subject_channel_id,
            source_type=data.source_type,
            source_message_id=data.source_message_id,
            metadata=data.metadata,
            status="pending",
            required_approver_type=required_approver_type,
            expires_at=expires_at,
            confidence=data.confidence,
        )
        return await self.create_memory(pending)

    async def get_memory(self, guild_id: int, memory_id: int) -> dict[str, Any] | None:
        row = await self.db.get_server_memory(guild_id, memory_id)
        return self.decode_memory(row) if row else None

    async def list_memories(
        self,
        guild_id: int,
        *,
        memory_type: str | None = None,
        status: str | None = "active",
        subject_user_id: int | None = None,
        subject_channel_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.expire_pending_memories(guild_id)
        rows = await self.db.list_server_memories(
            guild_id,
            memory_type=self.normalize_memory_type(memory_type) if memory_type else None,
            status=self.normalize_status(status) if status else None,
            subject_user_id=subject_user_id,
            subject_channel_id=subject_channel_id,
            limit=limit,
        )
        return [self.decode_memory(row) for row in rows]

    async def list_user_memories(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return await self.list_memories(guild_id, subject_user_id=user_id)

    async def list_pending_memories(self, guild_id: int) -> list[dict[str, Any]]:
        return await self.list_memories(guild_id, status="pending")

    async def update_memory(self, guild_id: int, memory_id: int, *, value: str, key: str | None = None) -> bool:
        return await self.db.update_server_memory_value(
            memory_id,
            guild_id=guild_id,
            value=self.clean_value(value),
            key=self.normalize_key(key) if key else None,
        )

    async def archive_memory(self, guild_id: int, memory_id: int) -> bool:
        return await self.db.update_server_memory_status(memory_id, guild_id=guild_id, status="archived")

    async def approve_memory(self, guild_id: int, memory_id: int, approver_user_id: int) -> bool:
        return await self.db.update_server_memory_status(
            memory_id,
            guild_id=guild_id,
            status="active",
            approved_by_user_id=approver_user_id,
        )

    async def reject_memory(self, guild_id: int, memory_id: int, approver_user_id: int | None = None) -> bool:
        return await self.db.update_server_memory_status(
            memory_id,
            guild_id=guild_id,
            status="rejected",
            approved_by_user_id=approver_user_id,
        )

    async def expire_pending_memories(self, guild_id: int) -> int:
        return await self.db.expire_server_memories(guild_id)

    async def clear_memories(self, guild_id: int) -> None:
        await self.db.clear_server_memories(guild_id)

    async def counts(self, guild_id: int) -> list[dict[str, Any]]:
        return await self.db.count_server_memories_by_status(guild_id)

    @classmethod
    def should_require_approval(cls, memory_type: str, value: str, *, created_for_other: bool) -> bool:
        if memory_type not in {"USER_NICKNAME", "USER_ALIAS"}:
            return False
        lowered = value.casefold()
        conflict_words = {
            "pendej",
            "idiot",
            "stupid",
            "dumb",
            "puta",
            "puto",
            "mierda",
            "doxx",
            "dox",
            "address",
            "telefono",
            "teléfono",
        }
        return created_for_other and any(word in lowered for word in conflict_words)

    @staticmethod
    def classify_approval_text(content: str) -> str | None:
        normalized = ServerMemoryService.normalize_phrase(content)
        if normalized in AFFIRMATIVE_RESPONSES:
            return "approve"
        if normalized in NEGATIVE_RESPONSES:
            return "reject"
        return None

    @staticmethod
    def normalize_phrase(value: str) -> str:
        cleaned = re.sub(r"[.,!¡¿?;:]+", " ", value.casefold())
        return " ".join(cleaned.split())

    @staticmethod
    def normalize_memory_type(value: str) -> str:
        normalized = str(value or "").strip().upper()
        aliases = {
            "NICKNAME": "USER_NICKNAME",
            "NICK": "USER_NICKNAME",
            "APODO": "USER_NICKNAME",
            "ALIAS": "USER_ALIAS",
            "PREFERENCE": "USER_PREFERENCE",
            "PREFERENCIA": "USER_PREFERENCE",
            "RULE": "SERVER_RULE",
            "REGLA": "SERVER_RULE",
            "FOOTBALL": "FOOTBALL_PREFERENCE",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {value}")
        return normalized

    @staticmethod
    def normalize_status(value: str | None) -> str:
        normalized = str(value or "active").strip().casefold()
        if normalized not in MEMORY_STATUSES:
            raise ValueError(f"Unsupported memory status: {value}")
        return normalized

    @staticmethod
    def normalize_key(value: str | None) -> str:
        cleaned = re.sub(r"\s+", "_", str(value or "").strip().casefold())
        return re.sub(r"[^a-z0-9_:\-.]+", "", cleaned)[:120]

    @staticmethod
    def clean_value(value: str) -> str:
        cleaned = " ".join(str(value or "").split())
        return cleaned[:900]

    @staticmethod
    def clean_token(value: str, *, default: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "", str(value or "").strip())[:80]
        return cleaned or default

    @staticmethod
    def decode_memory(row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        metadata = decoded.get("metadata_json")
        try:
            decoded["metadata"] = json.loads(metadata) if metadata else {}
        except json.JSONDecodeError:
            decoded["metadata"] = {}
        return decoded
