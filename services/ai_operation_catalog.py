"""Never derives an operation from natural language."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from services.database import BIRTHDAY_EVENT_TYPES
from services.football_query_service import OPERATION_TYPES
from services.glot import RUN_FILENAMES
from services.server_memory import MEMORY_STATUSES


ADMIN_READ_OPERATIONS = frozenset({"join_stats", "leave_stats", "list_warnings", "user_info", "ai_channel_list", "server_context_view", "color_list", "help"})
ADMIN_MUTATING_OPERATIONS = frozenset(
    {
        "mute",
        "tempmute",
        "unmute",
        "lock_channel",
        "unlock_channel",
        "delete_channel",
        "create_channel",
        "create_role",
        "remove_role",
        "delete_messages",
        "delete_role",
        "add_role",
        "kick",
        "ban",
        "unban",
        "tempban",
        "warn",
        "remove_warning",
        "clear_warnings",
        "set_slowmode",
        "set_nickname",
        "say",
        "clear_channel",
        "clone_channel",
        "purge_user_messages",
        "set_modlog",
        "set_prefix",
        "set_language",
        "set_antispam",
        "set_antilink",
        "ai_channel_add",
        "ai_channel_remove",
        "ai_channel_clear",
        "server_context_reset",
        "server_context_learn",
        "color_setup",
        "color_publish",
        "color_reload",
        "color_add",
        "color_remove",
    }
)
MEMORY_READ_OPERATIONS = frozenset({"SERVER_MEMORY_LOOKUP", "SERVER_MEMORY_CLARIFY", "SERVER_MEMORY_LIST"})
MEMORY_MUTATING_OPERATIONS = frozenset(
    {"SERVER_MEMORY_WRITE", "SERVER_MEMORY_UPDATE", "SERVER_MEMORY_DELETE", "SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT"}
)
# Lifecycle operations act on stored memory rows by id and require server-administrator authority.
MEMORY_LIFECYCLE_OPERATIONS = frozenset({"SERVER_MEMORY_LIST", "SERVER_MEMORY_APPROVE", "SERVER_MEMORY_REJECT"})


def is_memory_lifecycle(operands: dict) -> bool:
    return operands.get("memory_action") in MEMORY_LIFECYCLE_OPERATIONS or (
        operands.get("memory_action") == "SERVER_MEMORY_DELETE" and operands.get("memory_id") is not None
    )
FOOTBALL_MUTATING_OPERATIONS = frozenset({"live_watch_start", "live_watch_stop"})
# Every NINJAS operation is a read-only lookup against the existing provider client.
NINJAS_OPERATIONS = frozenset({"joke", "dadjoke", "advice", "whois", "unit_conversion"})
# CODE runs a user-supplied program on the remote Glot runner (never locally); the
# language enum is the runner's slug table, and command/stdin are never operands.
CODE_OPERATIONS = frozenset({"run", "languages"})
# REMINDER manages only the author's own reminders; identity, guild and channel are
# taken from the Discord message, never from operands.  list reads; create/remove mutate.
REMINDER_OPERATIONS = frozenset({"create", "list", "remove"})
# BIRTHDAY covers birthday profiles and the administrator-only module settings.  Profile set and
# remove act only on the message author, while show, view and next read.  Each settings operation
# lists the only operands it accepts; every settings operation mutates (preview posts a message)
# except template_list, which reads but stays administrator-only like its command.
BIRTHDAY_SETTINGS_OPERANDS = {
    "setup": frozenset({"target_channel", "target_role_candidates"}),
    "set_channel": frozenset({"target_channel"}),
    "set_role": frozenset({"target_role_candidates"}),
    "set_timezone": frozenset({"birthday_timezone"}),
    "set_mode": frozenset({"birthday_mode"}),
    "set_ages": frozenset({"birthday_ages"}),
    "event": frozenset({"birthday_event", "birthday_event_color", "birthday_event_image", "birthday_event_message"}),
    "preview": frozenset({"birthday_event"}),
    "template_add": frozenset({"birthday_template_type", "birthday_template"}),
    "template_list": frozenset({"birthday_template_type"}),
    "template_remove": frozenset({"birthday_template_type", "birthday_template_id"}),
    "blacklist_user": frozenset({"target_user_id", "target_user_candidates", "birthday_blocked"}),
    "blacklist_role": frozenset({"target_role_candidates", "birthday_blocked"}),
    # An omitted flag is false and an omitted role clears it, exactly as the command's defaults do.
    "trusted": frozenset({"target_role_candidates", "birthday_block_message", "birthday_block_role", "birthday_block_list"}),
}
BIRTHDAY_OPERATIONS = frozenset({"show", "set", "remove", "view", "next"}) | frozenset(BIRTHDAY_SETTINGS_OPERANDS)
BIRTHDAY_MODES = frozenset({"user", "server"})
# The keys of /birthday event; preview accepts every one except disable.
BIRTHDAY_EVENTS = frozenset({"birthday_default", "birthday_year", "member_anniversary", "server_anniversary", "disable"})
BIRTHDAY_TEMPLATE_EVENTS = frozenset(BIRTHDAY_EVENT_TYPES)
# TRANSLATE text is either supplied by the router or taken from the structurally
# authorized replied message; the router never has to copy a long source verbatim.
TRANSLATE_TEXT_SOURCES = frozenset({"REPLIED_MESSAGE"})
MEME_OPERATIONS = frozenset({"template", "random", "custom"})
MEME_TEXT_SLOTS = frozenset({"top", "bottom"})

# Native operands have one canonical operation field per family.  Compatibility
# action/owner fields are deliberately not consulted here.
OPERATION_FIELDS = {
    "ADMIN": "admin_action",
    "MEMORY": "memory_action",
    "FOOTBALL": "operation_family",
    "NINJAS": "ninjas_action",
    "MEME": "meme_action",
    "CODE": "code_action",
    "REMINDER": "reminder_action",
    "BIRTHDAY": "birthday_action",
}
# Every canonical operation field plus the generic ``operation`` spelling: a task
# may carry only its own family's field.
OPERATION_FIELD_NAMES = frozenset(OPERATION_FIELDS.values()) | {"operation"}
OPERATION_VALUES = {
    "ADMIN": frozenset(ADMIN_READ_OPERATIONS | ADMIN_MUTATING_OPERATIONS),
    "MEMORY": frozenset(MEMORY_READ_OPERATIONS | MEMORY_MUTATING_OPERATIONS),
    "FOOTBALL": frozenset(OPERATION_TYPES - {"unknown"}),
    "NINJAS": NINJAS_OPERATIONS,
    "MEME": MEME_OPERATIONS,
    "CODE": CODE_OPERATIONS,
    "REMINDER": REMINDER_OPERATIONS,
    "BIRTHDAY": BIRTHDAY_OPERATIONS,
}

KNOWN_OUTPUTS = {
    "WEB": {"answer", "sources", "citations", "media_candidates", "grounded_visual_observation", "result"},
    # ``media`` is a deliberate structural projection of authorized
    # ``ExternalSourceBundle.selected_media`` for visual consumers.
    "EXTERNAL_SOURCE": {"sources", "results", "selected_media", "media", "result"},
    # Result-only vision handlers return these concrete payload keys.  The
    # whole terminal result remains available for structural consumers.
    "IMAGE_ANALYZE": {"text", "media_urls", "rendered_media_count", "result"},
    "IMAGE_GENERATE": {"artifact"},
    "IMAGE_EDIT": {"artifact"},
    "FOOTBALL": {
        "fixtures", "events", "statistics", "lineups", "fixture_players", "match_center",
        "normalized_events", "shootout", "half_statistics", "player_performances",
        "team_season_statistics", "rounds", "venues", "predictions", "odds", "coach_context_row",
        "trophies", "sidelined", "coverage", "reference_rows", "player_context_row",
        "team_context_row", "league_context_row", "standings_table", "standing_row", "generic_rows",
        "requested_scope", "resolved_entities", "fallback_evidence", "result",
    },
    "MINECRAFT": {"result"},
    "NINJAS": {"result"},
    "MEME": {"artifact", "result"},
    "CODE": {"result"},
    "REMINDER": {"result"},
    "BIRTHDAY": {"result"},
    "TRANSLATE": {"text", "result"},
    "ADMIN": {"result", "resource_handle"},
    "MEMORY": {"result"},
    # "result" is the whole terminal result of a task and the router names it for every family it is not told otherwise;
    # the named outputs stay for typed bindings.
    "REACTION": {"reaction", "result"},
    "CHAT": {"answer", "result"},
    "CLARIFICATION": {"question", "result"},
}
ARTIFACT_OUTPUT_FIELDS = frozenset({"artifact"})
# Only consumers with a typed resource/media input constrain the producer
# output.  Ordered dependencies without a binding remain valid.
CONSUMER_OUTPUT_FIELDS = {
    "ADMIN": frozenset({"resource_handle"}),
    "IMAGE_ANALYZE": frozenset({"media", "artifact"}),
    "IMAGE_EDIT": frozenset({"media", "artifact"}),
    "MEME": frozenset({"answer", "text", "result"}),
}


@dataclass(frozen=True)
class OperandDescriptor:
    kind: str
    nullable: bool = True
    max_length: int | None = None
    max_items: int | None = None
    item_max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    enum: frozenset[str] | None = None

    def json_schema(self) -> dict[str, Any]:
        value_type: str | list[str] = self.kind
        if self.nullable:
            value_type = [self.kind, "null"]
        schema: dict[str, Any] = {"type": value_type}
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.max_items is not None:
            schema["maxItems"] = self.max_items
        if self.item_max_length is not None:
            schema["items"] = {"type": "string", "maxLength": self.item_max_length}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.enum is not None:
            schema["enum"] = [*sorted(self.enum), None] if self.nullable else sorted(self.enum)
        return schema


def _string(max_length: int, *, nullable: bool = True, enum: frozenset[str] | None = None) -> OperandDescriptor:
    return OperandDescriptor("string", nullable=nullable, max_length=max_length, enum=enum)


def _integer(*, minimum: int | None = None, maximum: int | None = None) -> OperandDescriptor:
    return OperandDescriptor("integer", minimum=minimum, maximum=maximum)


def _number(*, minimum: float | None = None, maximum: float | None = None) -> OperandDescriptor:
    return OperandDescriptor("number", minimum=minimum, maximum=maximum)


def _boolean() -> OperandDescriptor:
    return OperandDescriptor("boolean")


def _strings(*, max_items: int = 8, item_max_length: int = 120) -> OperandDescriptor:
    return OperandDescriptor("array", nullable=False, max_items=max_items, item_max_length=item_max_length)


# This is deliberately one boring table: adding a field requires both the
# router schema and the deterministic native boundary to agree on its type.
OPERAND_DESCRIPTORS: dict[str, dict[str, OperandDescriptor]] = {
    "ADMIN": {
        "admin_action": _string(40, nullable=False, enum=frozenset(ADMIN_READ_OPERATIONS | ADMIN_MUTATING_OPERATIONS)),
        "target_channel": _string(120), "channel_name": _string(100),
        "target_user_id": _integer(minimum=1), "warning_number": _integer(minimum=1),
        "role_name": _string(100), "role_color_hex": _string(7), "reason": _string(280),
        "duration_seconds": _integer(minimum=0), "time_window_seconds": _integer(minimum=0),
        "message_count": _integer(minimum=0), "target_user_candidates": _strings(),
        "target_role_candidates": _strings(),
        # Discord's own limits: slowmode 0 (off) to 6 hours, nickname 32 characters, message 2000.  say posts only in the current channel.
        "slowmode_seconds": _integer(minimum=0, maximum=21600), "nickname": _string(32), "say_text": _string(2000),
        # Server configuration (manage_guild): the command's 5-character prefix, en/es, and an explicit on/off flag; the channel reuses target_channel.
        "guild_prefix": _string(5), "guild_language": _string(2, enum=frozenset({"en", "es"})), "filter_enabled": _boolean(),
        # Server context (manage_guild): reset names its scope explicitly (never a default); help takes one canonical section key (validated against the author's own keys).
        "context_scope": _string(12, enum=frozenset({"summaries", "memory", "ai_history", "all"})), "help_section": _string(40),
    },
    "MEMORY": {
        "memory_action": _string(40, nullable=False, enum=frozenset(MEMORY_READ_OPERATIONS | MEMORY_MUTATING_OPERATIONS)),
        "memory_type": _string(120), "subject": _string(120), "key": _string(120),
        "value": _string(900), "scope": _string(120),
        "memory_id": _integer(minimum=1), "memory_status": _string(20, enum=frozenset(MEMORY_STATUSES)),
    },
    "FOOTBALL": {
        "operation_family": _string(80, nullable=False, enum=frozenset(OPERATION_TYPES - {"unknown"})),
        "data_focus": _string(120), "fixture_focus": _string(120), "date_hint": _string(80),
        "season_hint": _string(80), "time_scope": _string(80), "stat_focus": _string(120),
        "player_candidates": _strings(), "team_candidates": _strings(), "coach_candidates": _strings(),
        "venue_candidates": _strings(), "league_candidates": _strings(), "country_candidates": _strings(),
        "bookmaker_candidates": _strings(), "bet_candidates": _strings(),
        "reference_query": _string(120), "country_filter": _string(20, enum=frozenset({"name", "code", "search"})),
        "reference_page": _integer(minimum=1),
    },
    "MINECRAFT": {"address": _string(255)},
    "NINJAS": {
        "ninjas_action": _string(40, nullable=False, enum=NINJAS_OPERATIONS),
        "domain": _string(253), "amount": _number(),
        "from_unit": _string(60), "to_unit": _string(60),
    },
    "CODE": {
        "code_action": _string(40, nullable=False, enum=CODE_OPERATIONS),
        "code_language": _string(20, enum=frozenset(RUN_FILENAMES)),
        "code": _string(4000),  # Discord's longest message
    },
    "REMINDER": {
        "reminder_action": _string(40, nullable=False, enum=REMINDER_OPERATIONS),
        "reminder_duration": _string(8), "reminder_message": _string(500),
        "reminder_id": _integer(minimum=1),
    },
    "BIRTHDAY": {
        "birthday_action": _string(40, nullable=False, enum=BIRTHDAY_OPERATIONS),
        "birth_month": _integer(minimum=1, maximum=12), "birth_day": _integer(minimum=1, maximum=31),
        "birth_year": _integer(minimum=1900),  # the upper bound is the current year, checked at execution
        "birthday_count": _integer(minimum=1, maximum=25),
        # view resolves its member exactly as typed ADMIN does; set/remove never take a target.
        "target_user_id": _integer(minimum=1), "target_user_candidates": _strings(),
        # Settings (administrator-only): channel and role reuse the ADMIN operands and resolve the same way.
        "target_channel": _string(120), "target_role_candidates": _strings(),
        "birthday_timezone": _string(64), "birthday_mode": _string(8, enum=BIRTHDAY_MODES),
        "birthday_ages": _boolean(),  # true shows ages in birthday messages (the command's disable_ages is the inverse)
        "birthday_event": _string(20, enum=BIRTHDAY_EVENTS),
        "birthday_event_color": _string(16), "birthday_event_image": _string(2048), "birthday_event_message": _string(1024),
        "birthday_template_type": _string(20, enum=BIRTHDAY_TEMPLATE_EVENTS),
        "birthday_template": _string(1024), "birthday_template_id": _integer(minimum=1),
        # true blocks the target (the commands' `blocked`); the block_* flags are the trusted command's blockmessage/blockrole/blocklist.
        "birthday_blocked": _boolean(), "birthday_block_message": _boolean(),
        "birthday_block_role": _boolean(), "birthday_block_list": _boolean(),
    },
    "TRANSLATE": {
        "text": _string(1800), "target_language": _string(40),
        "text_source": _string(20, enum=TRANSLATE_TEXT_SOURCES),
    },
    "MEME": {
        "meme_action": _string(12, enum=MEME_OPERATIONS), "meme_template": _string(80),
        "meme_top": _string(200), "meme_bottom": _string(200), "meme_text_slot": _string(6, enum=MEME_TEXT_SLOTS),
    },
    "REACTION": {"emoji": _string(120), "emojis": _strings()},
    "WEB": {"query": _string(900), "research_question": _string(900)},
    "EXTERNAL_SOURCE": {
        "url": _string(2400), "source_url": _string(2400), "source_scope": _string(40),
        "expected_content": _string(30), "operation": _string(40),
    },
    "IMAGE_GENERATE": {"prompt": _string(1200)},
    "IMAGE_EDIT": {"prompt": _string(1200), "source_url": _string(2400)},
    "IMAGE_ANALYZE": {"selection_count": OperandDescriptor("integer", nullable=False, minimum=1, maximum=4)},
}


def operand_descriptors(family: str) -> dict[str, OperandDescriptor]:
    return dict(OPERAND_DESCRIPTORS.get(str(family or "").strip().upper(), {}))


def native_operand_schema() -> dict[str, dict[str, Any]]:
    merged: dict[str, OperandDescriptor] = {}
    for descriptors in OPERAND_DESCRIPTORS.values():
        merged.update(descriptors)
    return {field: descriptor.json_schema() for field, descriptor in merged.items()}


# Strict mode forces a value on every non-nullable operand, so the router fills the other families' selectors and arrays too.
_SCHEMA_FORCED = frozenset(
    field
    for field, descriptor in {f: d for descriptors in OPERAND_DESCRIPTORS.values() for f, d in descriptors.items()}.items()
    if not descriptor.nullable
)


def own_operands(family: str, operands: Mapping[str, Any]) -> dict[str, Any]:
    descriptors = operand_descriptors(family)
    return {
        field: value
        for field, value in operands.items()
        if field in descriptors or not (value is None or field in _SCHEMA_FORCED)
    }


def validate_operands(family: str, operands: object) -> str | None:
    if not isinstance(operands, Mapping):
        return "task_operands_not_mapping"
    descriptors = operand_descriptors(family)
    operands = own_operands(family, operands)
    # Only an unknown operand with a value is refused.
    if any(field not in descriptors for field in operands):
        return "unknown_task_operand"
    for field, value in operands.items():
        descriptor = descriptors.get(field)
        if descriptor is None:
            continue
        if value is None and descriptor.nullable:
            continue
        if descriptor.kind == "string":
            if not isinstance(value, str) or (descriptor.max_length is not None and len(value) > descriptor.max_length):
                return "task_operand_type_or_bounds_invalid"
            if descriptor.enum is not None and value not in descriptor.enum:
                return "task_operand_enum_invalid"
        elif descriptor.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return "task_operand_type_or_bounds_invalid"
            if descriptor.minimum is not None and value < descriptor.minimum:
                return "task_operand_type_or_bounds_invalid"
            if descriptor.maximum is not None and value > descriptor.maximum:
                return "task_operand_type_or_bounds_invalid"
        elif descriptor.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return "task_operand_type_or_bounds_invalid"
            if descriptor.minimum is not None and value < descriptor.minimum:
                return "task_operand_type_or_bounds_invalid"
            if descriptor.maximum is not None and value > descriptor.maximum:
                return "task_operand_type_or_bounds_invalid"
        elif descriptor.kind == "boolean":
            if not isinstance(value, bool):
                return "task_operand_type_or_bounds_invalid"
        elif descriptor.kind == "array":
            if not isinstance(value, list) or (descriptor.max_items is not None and len(value) > descriptor.max_items):
                return "task_operand_type_or_bounds_invalid"
            if any(not isinstance(item, str) or (descriptor.item_max_length is not None and len(item) > descriptor.item_max_length) for item in value):
                return "task_operand_type_or_bounds_invalid"
    return None


def canonical_operation_field(family: str) -> str | None:
    return OPERATION_FIELDS.get(str(family or "").strip().upper())


def canonical_operations(family: str) -> frozenset[str]:
    return OPERATION_VALUES.get(str(family or "").strip().upper(), frozenset())


def known_output_fields(family: str) -> frozenset[str]:
    return frozenset(KNOWN_OUTPUTS.get(str(family or "").strip().upper(), set()))


def compatible_input_outputs(family: str) -> frozenset[str] | None:
    return CONSUMER_OUTPUT_FIELDS.get(str(family or "").strip().upper())


def _operation(request: Any) -> str:
    operands = request.operands if isinstance(getattr(request, "operands", None), Mapping) else {}
    family = str(getattr(request, "name", "")).strip().upper()
    key = canonical_operation_field(family)
    if key and any(candidate in operands for candidate in OPERATION_FIELD_NAMES if candidate != key):
        return ""
    value = operands.get(key) if key else None
    return value.strip() if isinstance(value, str) and value.strip() else ""


def operation_effect(request: Any) -> str | None:
    family = str(getattr(request, "name", "")).strip().upper()
    operation = _operation(request)
    if family == "REACTION":
        return "mutate"
    if family == "FOOTBALL":
        if operation in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP", "live_watch_start", "live_watch_stop"}:
            return "mutate"
        return "read" if operation in canonical_operations(family) else None
    if family == "ADMIN":
        if operation in ADMIN_MUTATING_OPERATIONS:
            return "mutate"
        if operation in ADMIN_READ_OPERATIONS:
            return "read"
        return None
    if family == "MEMORY":
        if operation in MEMORY_MUTATING_OPERATIONS:
            return "mutate"
        if operation in MEMORY_READ_OPERATIONS:
            return "read"
        return None
    if family == "NINJAS":
        return "read" if operation in NINJAS_OPERATIONS else None
    if family == "CODE":
        return "read" if operation in CODE_OPERATIONS else None
    if family == "REMINDER":
        if operation == "list":
            return "read"
        return "mutate" if operation in REMINDER_OPERATIONS else None
    if family == "BIRTHDAY":
        if operation in {"set", "remove"} or (operation in BIRTHDAY_SETTINGS_OPERANDS and operation != "template_list"):
            return "mutate"
        return "read" if operation in BIRTHDAY_OPERATIONS else None
    if family in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"}:
        return "artifact"
    if family in {"CHAT", "MINECRAFT", "TRANSLATE", "WEB", "EXTERNAL_SOURCE", "IMAGE_ANALYZE", "CLARIFICATION"}:
        return "read"
    return None
