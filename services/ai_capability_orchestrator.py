from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
import logging
import re
from typing import Any, Awaitable, Callable, Mapping, Sequence
import inspect

from services.external_sources import hash_text
from services.ai_operation_catalog import (
    ARTIFACT_OUTPUT_FIELDS,
    OPERATION_FIELD_NAMES,
    canonical_operation_field,
    canonical_operations,
    compatible_input_outputs,
    known_output_fields,
    operation_effect,
    own_operands,
    validate_operands,
)
from services.football_query_service import (
    CANONICAL_FOOTBALL_TIME_SCOPES,
    OPERATION_TYPES,
    compile_football_operation,
)


SUPPORTED_CAPABILITIES = frozenset(
    {
        "CHAT",
        "FOOTBALL",
        "MINECRAFT",
        "NINJAS",
        "CODE",
        "REMINDER",
        "BIRTHDAY",
        "TRANSLATE",
        "WEB",
        "EXTERNAL_SOURCE",
        "IMAGE_GENERATE",
        "IMAGE_EDIT",
        "IMAGE_ANALYZE",
        "MEME",
        "REACTION",
        "ADMIN",
        "MEMORY",
        "CLARIFICATION",
    }
)


@dataclass(frozen=True)
class CapabilityRequirement:
    requirement_id: str
    objective: str
    required: bool = True
    satisfaction_criterion: str = ""
    capability_owner: str = ""
    depends_on: tuple[str, ...] = ()
    owner_task_id: str | None = None


@dataclass(frozen=True)
class CapabilityRequest:
    name: str
    required: bool = True
    evidence_span: str = ""
    depends_on: tuple[str, ...] = ()
    scope: str = "UNSPECIFIED"
    independent: bool = True
    independent_research: bool = False
    selection_count: int = 1
    owner_action: str | None = None
    # Native plans use task_id as the identity.  Empty IDs remain supported
    # only for legacy single-family command constructors; normalization gives
    # those requests a stable family ID.
    task_id: str = ""
    operands: Mapping[str, Any] = field(default_factory=dict)
    input_bindings: tuple["InputBinding", ...] = ()
    source_refs: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputBinding:
    upstream_task_id: str
    output_field: str


class CapabilityTerminalStatus(StrEnum):
    SUCCESS = "SUCCESS"
    NO_DATA = "NO_DATA"
    PENDING = "PENDING"
    UNSUPPORTED = "UNSUPPORTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    SEMANTIC_INTERPRETATION_FAILED = "SEMANTIC_INTERPRETATION_FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"


@dataclass(frozen=True)
class CapabilityTerminalResult:
    capability: str
    status: CapabilityTerminalStatus
    provider: str
    native_outcome: str
    payload: object | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    scope: Mapping[str, Any] = field(default_factory=dict)
    delivery_metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    satisfies_request: bool = False
    task_id: str = ""
    delivery_status: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status != CapabilityTerminalStatus.PENDING

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "task_id": self.task_id,
            "status": self.status.value,
            "provider": self.provider,
            "native_outcome": self.native_outcome,
            "has_payload": self.payload is not None,
            "provenance": dict(self.provenance),
            "scope": dict(self.scope),
            "reason": self.reason,
            "satisfies_request": self.satisfies_request,
            "delivery_status": self.delivery_status,
        }


def _outcome_text(outcome: object) -> str:
    value = getattr(outcome, "value", outcome)
    return str(value or "UNKNOWN").strip().upper()


def _terminal_status_for_outcome(outcome: object) -> CapabilityTerminalStatus:
    if isinstance(outcome, CapabilityTerminalStatus):
        return outcome
    value = _outcome_text(outcome)
    if value in {"SELECTED", "SUCCESS", "OK"}:
        return CapabilityTerminalStatus.SUCCESS
    if value in {"NO_DATA", "NO_DATA_FOR_SCOPE", "EMPTY_RESPONSE"}:
        return CapabilityTerminalStatus.NO_DATA
    if value in {"PENDING", "IN_PROGRESS"}:
        return CapabilityTerminalStatus.PENDING
    if value in {
        "UNSUPPORTED",
        "UNSUPPORTED_BY_COVERAGE",
        "UNSUPPORTED_SOURCE",
        "DISABLED",
        "NOT_CONFIGURED",
    }:
        return CapabilityTerminalStatus.UNSUPPORTED
    if value in {"PERMISSION_DENIED", "ACCESS_DENIED", "AUTHORIZATION_FAILED", "FORBIDDEN"}:
        return CapabilityTerminalStatus.PERMISSION_DENIED
    if (
        value.startswith("AUTHOR_MISSING_")
        or value.startswith("BOT_MISSING_")
        or value.endswith("_FORBIDDEN")
        or value in {"TARGET_IS_ADMIN", "CHANNEL_FORBIDDEN", "ROLE_MUTE_FORBIDDEN", "BOT_TIMEOUT_FORBIDDEN"}
    ):
        return CapabilityTerminalStatus.PERMISSION_DENIED
    if value in {
        "AMBIGUOUS",
        "NOT_FOUND",
        "RESOLUTION_FAILED",
        "MALFORMED_SOURCE",
        "INVALID_REQUEST",
        "VALIDATION_FAILED",
    }:
        return CapabilityTerminalStatus.VALIDATION_FAILED
    if value in {"PARTIAL_SUCCESS", "PARTIAL"}:
        return CapabilityTerminalStatus.PARTIAL_SUCCESS
    if value == "PROVIDER_FAILURE":
        return CapabilityTerminalStatus.PROVIDER_FAILURE
    return CapabilityTerminalStatus.PROVIDER_FAILURE


def normalize_terminal_result(
    capability: str,
    outcome: object,
    *,
    provider: str,
    payload: object | None = None,
    provenance: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    delivery_metadata: Mapping[str, Any] | None = None,
    reason: str | None = None,
    satisfies_request: bool | None = None,
    task_id: str = "",
    delivery_status: str | None = None,
) -> CapabilityTerminalResult:
    status = _terminal_status_for_outcome(outcome)
    bounded_delivery_metadata: dict[str, Any] = {}
    delete_after_seconds = (delivery_metadata or {}).get("delete_after_seconds")
    if (
        isinstance(delete_after_seconds, (int, float))
        and not isinstance(delete_after_seconds, bool)
        and 0 < float(delete_after_seconds) <= 60
    ):
        bounded_delivery_metadata["delete_after_seconds"] = float(delete_after_seconds)
    if (delivery_metadata or {}).get("delete_trigger_on_success") is True:
        bounded_delivery_metadata["delete_trigger_on_success"] = True
    direct_failure_message = (delivery_metadata or {}).get("direct_failure_message")
    if isinstance(direct_failure_message, str) and direct_failure_message.strip():
        bounded_delivery_metadata["direct_failure_message"] = direct_failure_message.strip()[:500]
    return CapabilityTerminalResult(
        capability=str(capability or "UNKNOWN").strip().upper(),
        status=status,
        provider=str(provider or "unknown"),
        native_outcome=_outcome_text(outcome),
        payload=payload,
        provenance=dict(provenance or {}),
        scope=dict(scope or {}),
        delivery_metadata=bounded_delivery_metadata,
        reason=str(reason).strip() if reason else None,
        satisfies_request=(status == CapabilityTerminalStatus.SUCCESS if satisfies_request is None else satisfies_request),
        task_id=str(task_id or ""),
        delivery_status=str(delivery_status).strip() if delivery_status else None,
    )


@dataclass
class CapabilityExecutionContext:
    execution_plan: "CapabilityExecutionPlan"
    # Results are keyed by task identity.  Family views are compatibility
    # projections and never overwrite repeated task instances.
    results: dict[str, CapabilityTerminalResult] = field(default_factory=dict)

    def record(self, result: CapabilityTerminalResult) -> None:
        declared = {item.task_id: item for item in self.execution_plan.ordered_capabilities}
        task_id = result.task_id
        if not task_id:
            if self.execution_plan.semantic_plan.semantic_source == "router":
                raise ValueError(f"native result missing task id: {result.capability}")
            matches = [item.task_id for item in self.execution_plan.ordered_capabilities if item.name == result.capability]
            if len(matches) != 1:
                raise ValueError(f"ambiguous capability result: {result.capability}")
            task_id = matches[0]
            result = replace(result, task_id=task_id)
        item = declared.get(task_id)
        if item is None:
            raise ValueError(f"undeclared task result: {task_id}")
        if result.capability != item.name:
            raise ValueError(f"task result family mismatch: {task_id}")
        self.results[task_id] = result

    def defer_remaining(self, *, reason: str) -> None:
        for item in self.execution_plan.ordered_capabilities:
            if item.task_id in self.results:
                continue
            self.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.NOT_EXECUTED,
                    provider="orchestrator",
                    provenance={"source": "bounded_dispatch"},
                    reason=reason,
                    task_id=item.task_id,
                )
            )

    @property
    def bundle(self) -> "CapabilityResultBundle":
        return CapabilityResultBundle(
            required_capabilities=self.execution_plan.required_capabilities,
            results=tuple(self.results.values()),
            semantic_status=self.execution_plan.semantic_plan.semantic_status,
            semantic_failure_reason=self.execution_plan.semantic_plan.semantic_failure_reason,
            required_task_ids=self.execution_plan.required_task_ids,
        )

    def diagnostics(self) -> dict[str, Any]:
        bundle = self.bundle
        return {
            "required_capabilities": list(bundle.required_capabilities),
            "required_task_ids": list(bundle.required_task_ids),
            "completed_task_ids": sorted(bundle.by_task),
            "completed_capabilities": sorted(bundle.by_capability),
            "complete": bundle.complete,
            "satisfied": bundle.satisfied,
            "request_status": bundle.request_status,
            "semantic_status": bundle.semantic_status,
            "semantic_failure_reason": bundle.semantic_failure_reason,
            "missing": list(bundle.missing),
            "results": [result.to_payload() for result in bundle.results],
        }


@dataclass(frozen=True)
class CapabilityResultBundle:
    required_capabilities: tuple[str, ...]
    results: tuple[CapabilityTerminalResult, ...] = ()
    semantic_status: str = "SUCCESS"
    semantic_failure_reason: str | None = None
    required_task_ids: tuple[str, ...] = ()

    @property
    def by_task(self) -> dict[str, CapabilityTerminalResult]:
        return {result.task_id: result for result in self.results if result.task_id}

    @property
    def by_capability(self) -> dict[str, CapabilityTerminalResult]:
        grouped: dict[str, list[CapabilityTerminalResult]] = {}
        for result in self.results:
            grouped.setdefault(result.capability, []).append(result)
        # Compatibility convenience is intentionally unique-family-only.
        return {name: values[0] for name, values in grouped.items() if len(values) == 1}

    @property
    def complete(self) -> bool:
        if self.required_task_ids:
            results = self.by_task
            return all(task_id in results and results[task_id].terminal for task_id in self.required_task_ids)
        if not self.required_capabilities:
            return True
        # Legacy bundles have family-only results.  They still require one
        # terminal result per declared family; an empty result set is not a
        # vacuously complete request.
        return all(
            any(result.capability == capability and result.terminal for result in self.results)
            for capability in self.required_capabilities
        )

    @property
    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.required_task_ids:
            results = self.by_task
            for task_id in self.required_task_ids:
                result = results.get(task_id)
                if result is None or not result.terminal or not result.satisfies_request:
                    missing.append(task_id)
            return tuple(missing)
        for capability in self.required_capabilities:
            family_results = [result for result in self.results if result.capability == capability]
            if not family_results or not any(result.terminal and result.satisfies_request for result in family_results):
                missing.append(capability)
        return tuple(missing)

    @property
    def satisfied(self) -> bool:
        return self.semantic_status == "SUCCESS" and self.complete and not self.missing

    @property
    def request_status(self) -> str:
        if self.semantic_status != "SUCCESS":
            return "SEMANTIC_INTERPRETATION_FAILED"
        if self.satisfied:
            return "SATISFIED"
        if self.complete and self.missing:
            return "UNSATISFIED"
        return "PARTIALLY_SATISFIED"

    def to_payload(self) -> dict[str, Any]:
        return {
            "required_capabilities": list(self.required_capabilities),
            "required_task_ids": list(self.required_task_ids),
            "complete": self.complete,
            "satisfied": self.satisfied,
            "request_status": self.request_status,
            "semantic_status": self.semantic_status,
            "semantic_failure_reason": self.semantic_failure_reason,
            "missing": list(self.missing),
            "results": [result.to_payload() for result in self.results],
        }


CapabilityHandler = Callable[[CapabilityRequest, CapabilityExecutionContext], Awaitable[CapabilityTerminalResult]]
MutationPreflight = Callable[
    [tuple[CapabilityRequest, ...], CapabilityExecutionContext],
    Awaitable[bool] | bool,
]

async def execute_ordered_capabilities(
    execution_plan: "CapabilityExecutionPlan",
    handlers: Mapping[str, CapabilityHandler],
    *,
    context: CapabilityExecutionContext | None = None,
    preflight: MutationPreflight | None = None,
) -> CapabilityExecutionContext:
    """A failed dependency prevents downstream provider work, so incomplete and satisfied work stay distinct."""

    execution_context = context or CapabilityExecutionContext(execution_plan)
    if not execution_plan.valid:
        execution_context.defer_remaining(reason="invalid_execution_plan")
        return execution_context
    mutation_tasks = tuple(
        item for item in execution_plan.ordered_capabilities if operation_effect(item) == "mutate"
    )
    invalid_catalog_tasks = tuple(
        item for item in execution_plan.ordered_capabilities if operation_effect(item) is None
    )
    catalog_invalid = bool(invalid_catalog_tasks)
    if catalog_invalid:
        for item in invalid_catalog_tasks:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.VALIDATION_FAILED,
                    provider="orchestrator",
                    provenance={"source": "operation_catalog"},
                    reason="unknown_or_missing_operation",
                    task_id=item.task_id,
                )
            )
    preflight_allowed = True
    if mutation_tasks and catalog_invalid:
        preflight_allowed = False
        preflight_reason = "operation_catalog_invalid"
    elif mutation_tasks:
        if preflight is None:
            preflight_allowed = False
            preflight_reason = "mutation_batch_preflight_missing"
        else:
            try:
                decision = preflight(mutation_tasks, execution_context)
                if inspect.isawaitable(decision):
                    decision = await decision
                preflight_allowed = bool(decision)
                preflight_reason = "mutation_batch_preflight_failed"
            except Exception as exc:
                preflight_allowed = False
                preflight_reason = f"mutation_batch_preflight_exception:{type(exc).__name__}"
        if not preflight_allowed:
            for item in mutation_tasks:
                execution_context.record(
                    normalize_terminal_result(
                        item.name,
                        CapabilityTerminalStatus.NOT_EXECUTED,
                        provider="orchestrator",
                        provenance={"source": "mutation_batch_preflight"},
                        reason=preflight_reason,
                        task_id=item.task_id,
                    )
                )
    mutation_failed = not preflight_allowed
    for item in execution_plan.ordered_capabilities:
        if item.task_id in execution_context.results:
            continue
        dependency_failures = [
            dependency
            for dependency in item.depends_on
            if dependency not in execution_context.results
            or not execution_context.results[dependency].satisfies_request
        ]
        if dependency_failures:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.NOT_EXECUTED,
                    provider="orchestrator",
                    provenance={"source": "dependency_gate"},
                    reason=f"unsatisfied_dependencies:{','.join(dependency_failures)}",
                    task_id=item.task_id,
                )
            )
            continue
        item_is_mutation = operation_effect(item) == "mutate"
        if item_is_mutation and mutation_failed:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.NOT_EXECUTED,
                    provider="orchestrator",
                    provenance={"source": "mutation_fail_stop"},
                    reason="mutation_batch_fail_stop",
                    task_id=item.task_id,
                )
            )
            continue
        handler = handlers.get(item.name)
        if handler is None:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.UNSUPPORTED,
                    provider="orchestrator",
                    provenance={"source": "result_only_adapter_registry"},
                    reason="result_only_handler_missing",
                    task_id=item.task_id,
                )
            )
            if item_is_mutation:
                mutation_failed = True
            continue
        if operation_effect(item) is None:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.VALIDATION_FAILED,
                    provider="orchestrator",
                    provenance={"source": "operation_catalog"},
                    reason="unknown_or_missing_operation",
                    task_id=item.task_id,
                )
            )
            if item_is_mutation:
                mutation_failed = True
            continue
        try:
            result = await handler(item, execution_context)
        except Exception as exc:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.PROVIDER_FAILURE,
                    provider="orchestrator",
                    provenance={"source": "result_only_handler"},
                    reason=type(exc).__name__,
                    task_id=item.task_id,
                )
            )
            if item_is_mutation:
                mutation_failed = True
            continue
        if result.capability != item.name:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.VALIDATION_FAILED,
                    provider="orchestrator",
                    provenance={"source": "result_only_handler"},
                    reason=f"handler_returned:{result.capability}",
                    task_id=item.task_id,
                )
            )
            if item_is_mutation:
                mutation_failed = True
            continue
        if result.task_id and result.task_id != item.task_id:
            execution_context.record(
                normalize_terminal_result(
                    item.name,
                    CapabilityTerminalStatus.VALIDATION_FAILED,
                    provider="orchestrator",
                    provenance={"source": "result_only_handler"},
                    reason=f"handler_returned_task:{result.task_id}",
                    task_id=item.task_id,
                )
            )
            if item_is_mutation:
                mutation_failed = True
            continue
        execution_context.record(replace(result, task_id=item.task_id))
        if item_is_mutation and not result.satisfies_request:
            mutation_failed = True
    return execution_context


def reusable_results_for_continuation(
    execution_plan: "CapabilityExecutionPlan",
    prior_execution_plan: "CapabilityExecutionPlan",
    prior_bundle: "CapabilityResultBundle",
    *,
    current_scope: Mapping[str, Any] | None = None,
    fresh: bool = True,
    authorized: bool = True,
) -> dict[str, CapabilityTerminalResult]:
    current_names = tuple(item.name for item in execution_plan.ordered_capabilities)
    prior_names = tuple(item.name for item in prior_execution_plan.ordered_capabilities)

    def reject(reason: str) -> dict[str, CapabilityTerminalResult]:
        logging.info(
            "AI continuation reuse reusable=False rejection=%s current_capabilities=%s prior_capabilities=%s "
            "current_scope_present=%s",
            reason,
            ",".join(current_names),
            ",".join(prior_names),
            bool(current_scope),
        )
        return {}

    logging.info(
        "AI continuation reuse prior_context=True current_capabilities=%s prior_capabilities=%s "
        "current_scope_present=%s fresh=%s authorized=%s",
        ",".join(current_names),
        ",".join(prior_names),
        bool(current_scope),
        fresh,
        authorized,
    )

    if not fresh or not authorized:
        return reject("freshness_or_authorization")
    if not execution_plan.valid or not prior_execution_plan.valid:
        return reject("invalid_plan")
    if execution_plan.semantic_plan.semantic_status != "SUCCESS":
        return reject("current_semantic_status")
    if prior_bundle.semantic_status != "SUCCESS" or not prior_bundle.satisfied:
        return reject("prior_bundle_status_or_satisfaction")
    current_scopes = {
        str(task_id): dict(scope)
        for task_id, scope in dict(current_scope or {}).items()
        if isinstance(scope, Mapping)
    }
    prior_results = prior_bundle.by_task
    reusable: dict[str, CapabilityTerminalResult] = {}
    used_prior_tasks: set[str] = set()
    for request in execution_plan.ordered_capabilities:
        if not request.required:
            continue
        if operation_effect(request) == "mutate" or request.name in {"IMAGE_GENERATE", "IMAGE_EDIT", "MEME"}:
            return reject(f"non_replayable_request:{request.task_id}")
        candidate = next(
            (
                prior_item
                for prior_item in prior_execution_plan.ordered_capabilities
                if prior_item.required
                and prior_item.name == request.name
                and operation_effect(prior_item) != "mutate"
                and prior_item.name not in {"IMAGE_GENERATE", "IMAGE_EDIT"}
                and prior_item.scope == request.scope
                and prior_item.selection_count == request.selection_count
                and dict(prior_item.operands) == dict(request.operands)
                and prior_item.evidence_span == request.evidence_span
                and prior_item.independent == request.independent
                and prior_item.independent_research == request.independent_research
                and tuple(prior_item.source_refs) == tuple(request.source_refs)
                and tuple(prior_item.requested_outputs) == tuple(request.requested_outputs)
                and tuple(prior_item.depends_on) == tuple(request.depends_on)
                and tuple(prior_item.input_bindings) == tuple(request.input_bindings)
                and prior_item.task_id not in used_prior_tasks
            ),
            None,
        )
        result = prior_results.get(candidate.task_id) if candidate is not None else None
        if candidate is None or result is None:
            return reject(f"missing_or_mismatched_request:{request.task_id}")
        if not result.terminal or not result.satisfies_request:
            return reject(f"prior_result_not_satisfied:{request.task_id}")
        if result.status != CapabilityTerminalStatus.SUCCESS:
            return reject(f"prior_result_status:{request.task_id}")
        if result.provenance.get("authorized") is False:
            return reject(f"prior_result_unauthorized:{request.task_id}")
        if result.provider == "web-fallback":
            return reject("web_fallback_result_not_reusable_as_structured_context")
        scope = current_scopes.get(request.task_id, {})
        if request.name == "FOOTBALL":
            if not _football_result_matches_continuation(result, scope):
                return reject("football_identity_or_scope_mismatch")
        else:
            if not scope or any(
                result.scope.get(key, result.provenance.get(key)) != value
                and result.provenance.get(key, result.scope.get(key)) != value
                for key, value in scope.items()
            ):
                return reject(f"scope_mismatch:{request.task_id}")
        used_prior_tasks.add(candidate.task_id)
        reusable[request.task_id] = replace(result, task_id=request.task_id)
    logging.info(
        "AI continuation reuse reusable=True current_capabilities=%s reused_capabilities=%s",
        ",".join(current_names),
        ",".join(sorted(reusable)),
    )
    return reusable


def _football_result_matches_continuation(
    result: CapabilityTerminalResult,
    current_scope: Mapping[str, Any],
) -> bool:
    def typed_evidence(value: object) -> tuple[bool, str]:
        if isinstance(value, (list, tuple)):
            text = "|".join(
                _clean_text(item, 120)
                for item in value
                if _clean_text(item, 120)
            )
        else:
            text = _clean_text(value, 180)
        return bool(text), hash_text(text) if text else ""

    current_operation = current_scope.get("operation_family") or current_scope.get("data_focus")
    current_operation_present, current_operation_hash = typed_evidence(current_operation)
    current_data_focus_present, current_data_focus_hash = typed_evidence(current_scope.get("data_focus"))
    current_entity_evidence = {
        entity_type: typed_evidence(current_scope.get(candidate_key))
        for entity_type, candidate_key in (
            ("league", "league_candidates"),
            ("team", "team_candidates"),
            ("player", "player_candidates"),
        )
    }
    prior_operation_present = prior_operation_hash = ""
    prior_data_focus_present = prior_data_focus_hash = ""
    prior_entity_evidence = {
        entity_type: (False, "")
        for entity_type in ("league", "team", "player")
    }

    def reject(
        reason: str,
        *,
        operation_match: bool = False,
        data_focus_match: bool = False,
        identity_match: bool = False,
        identity_type: str = "none",
        requested_scope_present: bool = False,
        resolved_entities_present: bool = False,
    ) -> bool:
        logging.info(
            "AI continuation football matcher result=False reason=%s operation_match=%s "
            "data_focus_match=%s identity_match=%s identity_type=%s requested_scope_present=%s "
            "resolved_entities_present=%s current_operation_present=%s current_operation_hash=%s "
            "prior_operation_present=%s prior_operation_hash=%s current_data_focus_present=%s "
            "current_data_focus_hash=%s prior_data_focus_present=%s prior_data_focus_hash=%s "
            "current_league_candidates_present=%s current_league_candidates_hash=%s "
            "prior_league_identity_present=%s prior_league_identity_hash=%s "
            "current_team_candidates_present=%s current_team_candidates_hash=%s "
            "prior_team_identity_present=%s prior_team_identity_hash=%s "
            "current_player_candidates_present=%s current_player_candidates_hash=%s "
            "prior_player_identity_present=%s prior_player_identity_hash=%s",
            reason,
            operation_match,
            data_focus_match,
            identity_match,
            identity_type,
            requested_scope_present,
            resolved_entities_present,
            current_operation_present,
            current_operation_hash,
            bool(prior_operation_present),
            prior_operation_hash,
            current_data_focus_present,
            current_data_focus_hash,
            bool(prior_data_focus_present),
            prior_data_focus_hash,
            *current_entity_evidence["league"],
            *prior_entity_evidence["league"],
            *current_entity_evidence["team"],
            *prior_entity_evidence["team"],
            *current_entity_evidence["player"],
            *prior_entity_evidence["player"],
        )
        return False

    payload = result.payload
    resolved_entities = getattr(payload, "resolved_entities", None)
    requested_scope = getattr(payload, "requested_scope", None)
    if not isinstance(resolved_entities, Mapping) or not isinstance(requested_scope, Mapping):
        return reject(
            "missing_typed_scope",
            requested_scope_present=isinstance(requested_scope, Mapping),
            resolved_entities_present=isinstance(resolved_entities, Mapping),
        )
    prior_operation = requested_scope.get("operation_type") or result.provenance.get("operation")
    prior_operation_present, prior_operation_hash = typed_evidence(prior_operation)
    prior_data_focus_present, prior_data_focus_hash = typed_evidence(requested_scope.get("data_focus"))
    prior_entity_evidence = {
        entity_type: typed_evidence(
            resolved_entities.get(entity_type, {}).get("display_name")
            if isinstance(resolved_entities.get(entity_type), Mapping)
            else None
        )
        for entity_type in ("league", "team", "player")
    }
    operation = _clean_text(
        current_scope.get("operation_family") or current_scope.get("data_focus"),
        80,
    ).casefold()
    prior_operation = _clean_text(prior_operation, 80).casefold()
    operation_match = bool(operation) and operation == prior_operation
    data_focus = _clean_text(current_scope.get("data_focus"), 80).casefold()
    prior_focus = _clean_text(requested_scope.get("data_focus"), 80).casefold()
    if data_focus and prior_focus:
        data_focus_match = data_focus == prior_focus
        if not data_focus_match:
            return reject(
                "data_focus_mismatch",
                operation_match=operation_match,
                requested_scope_present=True,
                resolved_entities_present=True,
            )
    elif not operation_match:
        return reject(
            "operation_family_mismatch",
            requested_scope_present=True,
            resolved_entities_present=True,
        )

    identity_found = False
    for entity_type, candidate_key in (
        ("league", "league_candidates"),
        ("team", "team_candidates"),
        ("player", "player_candidates"),
    ):
        raw_candidates = current_scope.get(candidate_key)
        if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
            continue
        candidates = {
            _clean_text(value, 120).casefold()
            for value in raw_candidates
            if _clean_text(value, 120)
        }
        resolved = resolved_entities.get(entity_type)
        resolved_name = (
            _clean_text(resolved.get("display_name"), 120).casefold()
            if isinstance(resolved, Mapping)
            else ""
        )
        if len(candidates) != 1 or not resolved_name or resolved_name not in candidates:
            return reject(
                "resolved_entity_mismatch",
                operation_match=True,
                data_focus_match=True,
                identity_type=entity_type,
                requested_scope_present=True,
                resolved_entities_present=True,
            )
        identity_found = True
    if not identity_found:
        return reject(
            "missing_typed_entity_candidate",
            operation_match=True,
            data_focus_match=True,
            requested_scope_present=True,
            resolved_entities_present=True,
        )
    logging.info(
        "AI continuation football matcher result=True operation_match=True data_focus_match=True identity_match=True"
    )
    return True


@dataclass(frozen=True)
class CapabilityPlan:
    objective: str
    capabilities: tuple[CapabilityRequest, ...]
    primary_action: str
    response_delivery: dict[str, Any]
    external_source: dict[str, Any]
    football: dict[str, Any]
    minecraft: dict[str, Any] = field(default_factory=dict)
    evidence_source: str = "CURRENT_MESSAGE"
    semantic_source: str = "router"
    semantic_status: str = "SUCCESS"
    semantic_failure_reason: str | None = None
    valid: bool = True
    failure_reason: str | None = None
    requirements: tuple[CapabilityRequirement, ...] = ()

    @property
    def primary_capability(self) -> str:
        return self.capabilities[0].name if self.capabilities else "CHAT"

    def has(self, name: str) -> bool:
        return any(item.name == name for item in self.capabilities)

    def to_payload(self) -> dict[str, Any]:
        capabilities_payload: list[dict[str, Any]] = []
        for item in self.capabilities:
            capability_payload = {
                "name": item.name,
                "task_id": item.task_id,
                "required": item.required,
                "evidence_span": item.evidence_span,
                "depends_on": list(item.depends_on),
                "scope": item.scope,
                "independent": item.independent,
                "independent_research": item.independent_research,
                "operands": dict(item.operands),
                "input_bindings": [
                    {"upstream_task_id": binding.upstream_task_id, "output_field": binding.output_field}
                    for binding in item.input_bindings
                ],
                "source_refs": list(item.source_refs),
                "requested_outputs": list(item.requested_outputs),
            }
            capability_payload["owner_action"] = item.owner_action
            if item.name == "IMAGE_ANALYZE":
                capability_payload["selection_count"] = item.selection_count
            capabilities_payload.append(capability_payload)
        return {
            "objective": self.objective,
            "requirements": [
                {
                    "requirement_id": item.requirement_id,
                    "objective": item.objective,
                    "required": item.required,
                    "satisfaction_criterion": item.satisfaction_criterion,
                    "capability_owner": item.capability_owner,
                    "depends_on": list(item.depends_on),
                    "owner_task_id": item.owner_task_id,
                }
                for item in self.requirements
            ],
            "capabilities": capabilities_payload,
            "primary_action": self.primary_action,
            "response_delivery": dict(self.response_delivery),
            "external_source": dict(self.external_source),
            "football": dict(self.football),
            "minecraft": dict(self.minecraft),
            "evidence_source": self.evidence_source,
            "semantic_source": self.semantic_source,
            "semantic_status": self.semantic_status,
            "semantic_failure_reason": self.semantic_failure_reason,
            "valid": self.valid,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    semantic_plan: CapabilityPlan
    ordered_capabilities: tuple[CapabilityRequest, ...]
    dispatch_action: str
    missing_dependencies: tuple[str, ...] = ()
    cyclic_capabilities: tuple[str, ...] = ()
    compatibility_mismatch: str | None = None
    overflow: bool = False

    @property
    def valid(self) -> bool:
        return (
            self.semantic_plan.valid
            and self.semantic_plan.semantic_status == "SUCCESS"
            and not self.missing_dependencies
            and not self.cyclic_capabilities
            and not self.overflow
        )

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.ordered_capabilities if item.required)

    @property
    def required_task_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.ordered_capabilities if item.required)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ordered_capabilities": [item.name for item in self.ordered_capabilities],
            "ordered_task_ids": [item.task_id for item in self.ordered_capabilities],
            "required_capabilities": list(self.required_capabilities),
            "required_task_ids": list(self.required_task_ids),
            "dispatch_action": self.dispatch_action,
            "missing_dependencies": list(self.missing_dependencies),
            "cyclic_capabilities": list(self.cyclic_capabilities),
            "compatibility_mismatch": self.compatibility_mismatch,
            "overflow": self.overflow,
            "valid": self.valid,
        }


_ACTION_TO_CAPABILITY = {
    "MINECRAFT_SERVER_STATUS": "MINECRAFT",
    "WEB_LOOKUP": "WEB",
    "GENERATE_IMAGE": "IMAGE_GENERATE",
    "EDIT_IMAGE": "IMAGE_EDIT",
    "ANALYZE_IMAGE": "IMAGE_ANALYZE",
    "ADD_REACTION": "REACTION",
    "REACT_ONLY": "REACTION",
    "ADMIN_ACTION": "ADMIN",
    "SERVER_MEMORY_LOOKUP": "MEMORY",
    "SERVER_MEMORY_WRITE": "MEMORY",
    "SERVER_MEMORY_UPDATE": "MEMORY",
    "SERVER_MEMORY_DELETE": "MEMORY",
    "SERVER_MEMORY_CLARIFY": "MEMORY",
    "SERVER_MEMORY_LIST": "MEMORY",
    "SERVER_MEMORY_APPROVE": "MEMORY",
    "SERVER_MEMORY_REJECT": "MEMORY",
    "CLARIFY": "CLARIFICATION",
}

_CAPABILITY_TO_ACTION = {
    "FOOTBALL": "FOOTBALL_LOOKUP",
    "MINECRAFT": "MINECRAFT_SERVER_STATUS",
    "WEB": "WEB_LOOKUP",
    "IMAGE_GENERATE": "GENERATE_IMAGE",
    "IMAGE_EDIT": "EDIT_IMAGE",
    "IMAGE_ANALYZE": "ANALYZE_IMAGE",
    "REACTION": "ADD_REACTION",
    "ADMIN": "ADMIN_ACTION",
    "MEMORY": "SERVER_MEMORY_LOOKUP",
    "CLARIFICATION": "CLARIFY",
}

_MEMORY_ACTIONS = frozenset(
    {
        "SERVER_MEMORY_LOOKUP",
        "SERVER_MEMORY_WRITE",
        "SERVER_MEMORY_UPDATE",
        "SERVER_MEMORY_DELETE",
        "SERVER_MEMORY_CLARIFY",
        "SERVER_MEMORY_LIST",
        "SERVER_MEMORY_APPROVE",
        "SERVER_MEMORY_REJECT",
    }
)

_CAPABILITY_OWNER_ACTIONS = {
    "MINECRAFT": frozenset({"MINECRAFT_SERVER_STATUS"}),
    "ADMIN": frozenset({"ADMIN_ACTION"}),
    "MEMORY": _MEMORY_ACTIONS,
}
# Every typed handler action a required task may name as owner_action; the router schema offers exactly these.
OWNER_ACTION_VALUES = frozenset().union(*_CAPABILITY_OWNER_ACTIONS.values())


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _capability_for_action(action: str) -> str | None:
    if action.startswith("FOOTBALL_"):
        return "FOOTBALL"
    if action.startswith("SERVER_MEMORY_"):
        return "MEMORY"
    return _ACTION_TO_CAPABILITY.get(action)


def _normalize_delivery(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"modality": "UNSPECIFIED", "explicit": False, "source": "UNSPECIFIED", "evidence_span": "", "artifacts": []}
    modality = _clean_text(value.get("modality", "UNSPECIFIED"), 20).upper()
    if modality not in {"TEXT", "VOICE", "UNSPECIFIED"}:
        modality = "UNSPECIFIED"
    source = _clean_text(value.get("source", "UNSPECIFIED"), 30).upper()
    if source not in {"CURRENT_MESSAGE", "UNSPECIFIED"}:
        source = "UNSPECIFIED"
    return {
        "modality": modality,
        "explicit": bool(value.get("explicit", False)),
        "source": source,
        "evidence_span": _clean_text(value.get("evidence_span"), 180),
        "semantic_reason": _clean_text(value.get("semantic_reason"), 240),
        "artifacts": [
            {
                "upstream_task_id": _clean_text(item.get("upstream_task_id"), 80),
                "output_field": _clean_text(item.get("output_field"), 120),
            }
            for item in value.get("artifacts", [])[:8]
            if isinstance(item, Mapping)
        ] if isinstance(value.get("artifacts", []), list) else [],
    }


def _normalize_external(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"required": False, "operation": "UNSPECIFIED", "source_scope": "UNSPECIFIED", "explicit": False}
    operation = _clean_text(value.get("operation", "UNSPECIFIED"), 30).upper()
    if operation not in {"SUMMARIZE", "EXPLAIN", "COMPARE", "EXTRACT", "ANALYZE", "VERIFY", "OTHER", "UNSPECIFIED"}:
        operation = "UNSPECIFIED"
    scope = _clean_text(value.get("source_scope", "UNSPECIFIED"), 30).upper()
    if scope not in {"CURRENT_MESSAGE", "REPLY_TARGET", "BOTH", "UNSPECIFIED"}:
        scope = "UNSPECIFIED"
    expected_content = _clean_text(value.get("expected_content", "UNKNOWN"), 30).upper()
    if expected_content not in {"TEXT", "MEDIA", "MULTIMODAL", "UNKNOWN"}:
        expected_content = "UNKNOWN"
    return {
        "required": bool(value.get("required", False)),
        "operation": operation,
        "source_scope": scope,
        "explicit": bool(value.get("explicit", False)),
        "evidence_span": _clean_text(value.get("evidence_span"), 180),
        "expected_content": expected_content,
        "semantic_reason": _clean_text(value.get("semantic_reason"), 240),
    }


def _consumed_task_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        _clean_text(binding.get("upstream_task_id"), 80)
        for other in items
        for binding in (other.get("input_bindings") or ())
        if isinstance(binding, Mapping)
    }


def _is_pipeline_step(item: Mapping[str, Any], consumed_task_ids: set[str]) -> bool:
    """A CHAT task flagged not independent next to a specialized task is otherwise a non-executable projection."""

    return bool(item.get("input_bindings")) or _clean_text(item.get("task_id"), 80) in consumed_task_ids


def validate_native_capability_contract(
    payload: Mapping[str, Any],
    *,
    diagnostic_sink: list[dict[str, Any]] | None = None,
) -> str | None:
    """Deliberately does not infer missing capabilities; normalization is a structural projection only."""

    capability_plan = payload.get("capability_plan")
    if not isinstance(capability_plan, Mapping):
        return "inconsistent_capability_plan"
    allowed_plan_fields = {
        "objective", "requirements", "capabilities", "primary_action", "response_delivery",
        "external_source", "football", "minecraft", "evidence_source", "valid", "failure_reason",
        "semantic_source", "semantic_status", "semantic_failure_reason",
    }
    if set(capability_plan) - allowed_plan_fields:
        return "inconsistent_capability_plan"
    objective = capability_plan.get("objective")
    if not isinstance(objective, str) or len(objective) > 300:
        return "inconsistent_capability_plan"

    raw_capabilities = capability_plan.get("capabilities")
    if not isinstance(raw_capabilities, list):
        return "inconsistent_capability_plan"

    raw_requirements = capability_plan.get("requirements")
    external_value = payload.get("external_source", capability_plan.get("external_source"))
    external = external_value if isinstance(external_value, Mapping) else {}

    def text_shape(value: Any, limit: int) -> dict[str, int | bool]:
        text = _clean_text(value, limit)
        return {"present": bool(text), "length": len(text)}

    def dependency_values(value: Any, *, uppercase: bool = False) -> list[str]:
        if not isinstance(value, list):
            return []
        values = [_clean_text(item, 80) for item in value if isinstance(item, str)]
        return [item.upper() for item in values] if uppercase else values

    def typed_snapshot() -> dict[str, Any]:
        capabilities = [
            {
                "name": _clean_text(item.get("name"), 40).upper(),
                "required": bool(item.get("required", True)),
                "scope": _clean_text(item.get("scope"), 40).upper(),
                "independent": bool(item.get("independent")),
                "independent_research": bool(item.get("independent_research")),
                "depends_on": dependency_values(item.get("depends_on"), uppercase=True),
            }
            for item in raw_capabilities
            if isinstance(item, Mapping)
        ]
        requirements = [
            {
                "id": _clean_text(item.get("requirement_id"), 40),
                "required": bool(item.get("required", True)),
                "owner": _clean_text(item.get("capability_owner"), 40).upper(),
                "depends_on": dependency_values(item.get("depends_on")),
                "objective": text_shape(item.get("objective"), 240),
                "satisfaction_criterion": text_shape(item.get("satisfaction_criterion"), 240),
            }
            for item in raw_requirements
            if isinstance(item, Mapping)
        ] if isinstance(raw_requirements, list) else []
        return {
            "capability_count": len(raw_capabilities),
            "requirement_count": len(raw_requirements) if isinstance(raw_requirements, list) else None,
            "capabilities": capabilities,
            "requirements": requirements,
            "external_source": {
                "required": bool(external.get("required")),
                "expected_content": _clean_text(external.get("expected_content"), 30).upper(),
            },
        }

    def reject(predicate: str) -> str:
        if diagnostic_sink is not None and not diagnostic_sink:
            diagnostic_sink.append({"predicate": predicate, "typed_state": typed_snapshot()})
        return "inconsistent_capability_plan"

    if (
        not raw_capabilities
        and payload.get("participation") == "IGNORE"
        and payload.get("action") in {"IGNORE", "NONE"}
    ):
        return None  # structural IGNORE: nothing to plan
    if len(raw_capabilities) == 0 or len(raw_capabilities) > 8:
        return reject("capabilities_not_bounded_list")
    task_ids: list[str] = []
    task_by_id: dict[str, Mapping[str, Any]] = {}
    allowed_task_fields = {
        "name", "task_id", "required", "evidence_span", "depends_on", "scope",
        "independent", "independent_research", "selection_count", "owner_action",
        "operands", "input_bindings", "source_refs", "requested_outputs",
    }
    for item in raw_capabilities:
        if not isinstance(item, Mapping):
            return reject("capability_not_mapping")
        if set(item) - allowed_task_fields:
            return reject("unknown_task_field")
        name = _clean_text(item.get("name"), 40).upper()
        if name not in SUPPORTED_CAPABILITIES:
            return reject("unknown_capability")
        raw_task_id = item.get("task_id")
        if not isinstance(raw_task_id, str) or not 1 <= len(raw_task_id.strip()) <= 80:
            return reject("missing_or_duplicate_task_id")
        task_id = raw_task_id.strip()
        required_task_fields = {"name", "task_id", "required", "evidence_span", "depends_on", "scope", "independent", "independent_research", "owner_action", "operands", "input_bindings", "source_refs", "requested_outputs"}
        if not required_task_fields.issubset(item):
            return reject("task_fields_missing")
        if task_id in task_by_id:
            return reject("missing_or_duplicate_task_id")
        if not isinstance(item.get("required"), bool) or not isinstance(item.get("evidence_span"), str) or len(item["evidence_span"]) > 180:
            return reject("task_field_type_invalid")
        if not isinstance(item.get("scope"), str) or len(item["scope"]) > 40 or item["scope"] not in {
            "GENERAL_REASONING", "STRUCTURED_DATA", "BROAD_RESEARCH", "EXPLICIT_SOURCE",
            "MEDIA_ANALYSIS", "IMAGE_GENERATION", "IMAGE_EDIT", "DISCORD_ACTION", "UNSPECIFIED",
        }:
            return reject("task_field_type_invalid")
        owner_action = item.get("owner_action")
        if owner_action is not None and (not isinstance(owner_action, str) or len(owner_action.strip()) > 80):
            return reject("task_field_type_invalid")
        selection_count = item.get("selection_count", 1)
        if isinstance(selection_count, bool) or not isinstance(selection_count, int) or not 1 <= selection_count <= 4:
            return reject("task_selection_count_invalid")
        task_ids.append(task_id)
        task_by_id[task_id] = item
    for item in raw_capabilities:
        task_id = _clean_text(item.get("task_id"), 80)
        name = _clean_text(item.get("name"), 40).upper()
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or len(dependencies) > 8:
            return reject("task_dependencies_invalid")
        if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 for value in dependencies):
            return reject("task_dependency_reference_invalid")
        dependency_ids = [value.strip() for value in dependencies]
        if len(set(dependency_ids)) != len(dependency_ids) or any(not value or value not in task_by_id or value == task_id for value in dependency_ids):
            return reject("task_dependency_reference_invalid")
        bindings = item.get("input_bindings", [])
        if not isinstance(bindings, list) or len(bindings) > 8:
            return reject("task_bindings_invalid")
        bound_ids: set[str] = set()
        bound_pairs: set[tuple[str, str]] = set()
        bound_outputs: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != {"upstream_task_id", "output_field"}:
                return reject("task_binding_not_mapping")
            raw_upstream = binding.get("upstream_task_id")
            raw_output = binding.get("output_field")
            if not isinstance(raw_upstream, str) or not isinstance(raw_output, str) or not 1 <= len(raw_upstream.strip()) <= 80 or not 1 <= len(raw_output.strip()) <= 120:
                return reject("task_binding_reference_invalid")
            upstream = raw_upstream.strip()
            output = raw_output.strip()
            pair = (upstream, output)
            producer = task_by_id.get(upstream)
            producer_name = _clean_text(producer.get("name"), 40).upper() if producer else ""
            producer_outputs = producer.get("requested_outputs", []) if producer else []
            compatible_outputs = compatible_input_outputs(name)
            if (
                not upstream or producer is None or not output or pair in bound_pairs
                or output in bound_outputs
                or output not in known_output_fields(producer_name)
                or not isinstance(producer_outputs, list) or output not in producer_outputs
                or (compatible_outputs is not None and output not in compatible_outputs)
            ):
                return reject("task_binding_reference_invalid")
            bound_ids.add(upstream)
            bound_pairs.add(pair)
            bound_outputs.add(output)
        if not bound_ids.issubset(set(dependency_ids)):
            return reject("task_binding_not_declared_dependency")
        operands = item.get("operands")
        if not isinstance(operands, Mapping):
            return reject("task_operands_not_mapping")
        operand_failure = validate_operands(name, operands)
        if operand_failure is not None:
            return reject(operand_failure)
        operands = own_operands(name, operands)
        if not isinstance(item.get("required"), bool) or not isinstance(item.get("independent"), bool) or not isinstance(item.get("independent_research"), bool):
            return reject("task_field_type_invalid")
        if name == "MINECRAFT" and operands.get("address") is not None:
            address = operands.get("address")
            if not isinstance(address, str) or not re.fullmatch(r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?", address.strip()):
                return reject("invalid_minecraft_address")
        operation_field = canonical_operation_field(name)
        if operation_field:
            operation = operands.get(operation_field)
            if not isinstance(operation, str) or operation not in canonical_operations(name):
                return reject("unknown_or_missing_operation")
            if any(key in operands for key in OPERATION_FIELD_NAMES - {operation_field}):
                return reject("conflicting_operation_fields")
        for field in ("source_refs", "requested_outputs"):
            values = item.get(field)
            if not isinstance(values, list) or len(values) > 8 or any(
                not isinstance(value, str) or not 1 <= len(value.strip()) <= 120
                for value in values
            ):
                return reject(f"task_{field}_invalid")
        if len(set(item.get("source_refs", []))) != len(item.get("source_refs", [])) or len(set(item.get("requested_outputs", []))) != len(item.get("requested_outputs", [])):
            return reject("task_outputs_or_sources_duplicate")
        if any(output not in known_output_fields(name) for output in item.get("requested_outputs", [])):
            return reject("unknown_requested_output")
    if any(_clean_text(item.get("name"), 40).upper() == "CLARIFICATION" for item in raw_capabilities):
        if len(raw_capabilities) != 1:
            return reject("clarification_must_be_exclusive")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_task(task_id: str) -> bool:
        if task_id in visiting:
            return False
        if task_id in visited:
            return True
        visiting.add(task_id)
        dependencies = task_by_id[task_id].get("depends_on", [])
        if any(not visit_task(_clean_text(value, 80)) for value in dependencies):
            return False
        visiting.remove(task_id)
        visited.add(task_id)
        return True

    if any(not visit_task(task_id) for task_id in task_ids):
        return reject("task_dependency_cycle")

    delivery = payload.get("response_delivery", capability_plan.get("response_delivery"))
    if isinstance(delivery, Mapping):
        artifacts = delivery.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 8:
            return reject("delivery_artifacts_invalid")
        artifact_pairs: set[tuple[str, str]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping) or set(artifact) != {"upstream_task_id", "output_field"}:
                return reject("delivery_artifact_not_mapping")
            upstream = _clean_text(artifact.get("upstream_task_id"), 80)
            output = _clean_text(artifact.get("output_field"), 120)
            producer = task_by_id.get(upstream)
            if (
                not producer
                or (upstream, output) in artifact_pairs
                or output not in ARTIFACT_OUTPUT_FIELDS
                or output not in producer.get("requested_outputs", [])
            ):
                return reject("delivery_artifact_reference_invalid")
            artifact_pairs.add((upstream, output))

    capability_names = [
        _clean_text(item.get("name"), 40).upper()
        for item in raw_capabilities
        if isinstance(item, Mapping)
    ]

    football = capability_plan.get("football")
    if isinstance(football, Mapping):
        date_hint = football.get("date_hint")
        if date_hint is not None:
            if not isinstance(date_hint, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_hint) is None:
                return reject("invalid_football_date_hint")
            try:
                date.fromisoformat(date_hint)
            except ValueError:
                return reject("invalid_football_date_hint")
        season_hint = football.get("season_hint")
        if season_hint is not None and (
            not isinstance(season_hint, str)
            or re.fullmatch(r"(?:19|20)\d{2}", season_hint) is None
        ):
            return reject("invalid_football_season_hint")
        time_scope = football.get("time_scope")
        if time_scope is not None and (
            not isinstance(time_scope, str)
            or time_scope not in CANONICAL_FOOTBALL_TIME_SCOPES
        ):
            return reject("invalid_football_time_scope")
        operation_family = _clean_text(football.get("operation_family"), 80).casefold()
        if operation_family and (
            operation_family == "unknown"
            or operation_family not in {item.casefold() for item in OPERATION_TYPES}
        ):
            return reject("unsupported_football_operation_family")
        data_focus = _clean_text(football.get("data_focus"), 120)
        if data_focus:
            try:
                probe = compile_football_operation(
                    "FOOTBALL_LOOKUP",
                    "",
                    {"semantic_source": "router", "data_focus": data_focus},
                )
            except Exception:
                return reject("unsupported_football_data_focus")
            if probe.operation_type == "unknown":
                return reject("unsupported_football_data_focus")

    if isinstance(raw_requirements, list):
        required_requirement_owners: dict[str, int] = {}
        for item in raw_requirements:
            if not isinstance(item, Mapping) or not bool(item.get("required", True)):
                continue
            owner = _clean_text(item.get("capability_owner"), 40).upper()
            required_requirement_owners[owner] = required_requirement_owners.get(owner, 0) + 1
        owner_task_ids = {
            (_clean_text(item.get("capability_owner"), 40).upper(), _clean_text(item.get("owner_task_id"), 80))
            for item in raw_requirements
            if isinstance(item, Mapping) and bool(item.get("required", True))
        }
        # Repeated families are legal; each required outcome must point at its
        # own declared task instance instead of collapsing to a family owner.

    required_image_capabilities = {
        _clean_text(item.get("name"), 40).upper()
        for item in raw_capabilities
        if isinstance(item, Mapping)
        and bool(item.get("required", True))
        and _clean_text(item.get("name"), 40).upper() in {"IMAGE_GENERATE", "IMAGE_EDIT"}
    }
    if required_image_capabilities:
        if not _clean_text(capability_plan.get("objective"), 500):
            return reject("required_image_capability_requires_objective")
        if not isinstance(raw_requirements, list):
            return reject("required_image_capability_requires_requirements")
        image_requirement_owners = {
            _clean_text(item.get("capability_owner"), 40).upper()
            for item in raw_requirements
            if isinstance(item, Mapping)
            and bool(item.get("required", True))
            and _clean_text(item.get("objective"), 240)
            and _clean_text(item.get("satisfaction_criterion"), 240)
        }
        if not required_image_capabilities.issubset(image_requirement_owners):
            return reject("required_image_capability_requires_requirement_objective")

    required_stage4_capability = any(
        isinstance(item, Mapping)
        and bool(item.get("required", True))
        and _clean_text(item.get("name"), 40).upper() in {"WEB", "EXTERNAL_SOURCE", "IMAGE_ANALYZE"}
        for item in raw_capabilities
    )
    if raw_requirements is None and any(
        isinstance(item, Mapping) and bool(item.get("required", True))
        for item in raw_capabilities
    ):
        return reject("required_capability_requires_requirements")
    if raw_requirements is None and required_stage4_capability:
        return reject("required_stage4_capability_requires_requirements")
    if raw_requirements is not None:
        if not isinstance(raw_requirements, list) or len(raw_requirements) > 8:
            return reject("requirements_not_bounded_list")
        if not raw_requirements and any(
            isinstance(item, Mapping) and bool(item.get("required", True))
            for item in raw_capabilities
        ):
            return reject("required_capability_requires_requirements")
        if not raw_requirements and any(
            isinstance(item, Mapping)
            and bool(item.get("required", True))
            and _clean_text(item.get("name"), 40).upper() in {"WEB", "EXTERNAL_SOURCE", "IMAGE_ANALYZE"}
            for item in raw_capabilities
        ):
            return reject("required_stage4_capability_requires_nonempty_requirements")
        requirement_ids: set[str] = set()
        requirement_owners: dict[str, set[str]] = {}
        for item in raw_requirements:
            if not isinstance(item, Mapping):
                return reject("requirement_not_mapping")
            if set(item) - {"requirement_id", "objective", "required", "satisfaction_criterion", "capability_owner", "depends_on", "owner_task_id"}:
                return reject("unknown_requirement_field")
            raw_requirement_id = item.get("requirement_id")
            raw_owner = item.get("capability_owner")
            raw_owner_task_id = item.get("owner_task_id")
            if not isinstance(item.get("required"), bool):
                return reject("requirement_fields_invalid")
            if (
                not isinstance(item.get("objective"), str)
                or not isinstance(item.get("satisfaction_criterion"), str)
                or len(item["objective"]) > 240
                or len(item["satisfaction_criterion"]) > 240
            ):
                return reject("requirement_fields_invalid")
            if not isinstance(raw_requirement_id, str) or not 1 <= len(raw_requirement_id.strip()) <= 40:
                return reject("requirement_fields_invalid")
            if not isinstance(raw_owner, str) or not 1 <= len(raw_owner.strip()) <= 40:
                return reject("requirement_fields_invalid")
            if raw_owner_task_id is not None and (not isinstance(raw_owner_task_id, str) or len(raw_owner_task_id.strip()) > 80):
                return reject("requirement_fields_invalid")
            requirement_id = raw_requirement_id.strip()
            owner = raw_owner.strip().upper()
            owner_task_id = raw_owner_task_id.strip() if isinstance(raw_owner_task_id, str) else ""
            if (
                not requirement_id
                or requirement_id in requirement_ids
                or not _clean_text(item.get("objective"), 240)
                or not _clean_text(item.get("satisfaction_criterion"), 240)
                or owner not in SUPPORTED_CAPABILITIES
                or (bool(item.get("required", True)) and (not owner_task_id or owner_task_id not in task_by_id))
                or (owner_task_id and _clean_text(task_by_id.get(owner_task_id, {}).get("name"), 40).upper() != owner)
            ):
                return reject("requirement_fields_invalid")
            requirement_ids.add(requirement_id)
            requirement_owners.setdefault(owner, set()).add(requirement_id)
        for item in raw_requirements:
            dependencies = item.get("depends_on", [])
            if (
                not isinstance(dependencies, list)
                or len(dependencies) > 8
                or any(
                    not isinstance(dependency, str) or not 1 <= len(dependency.strip()) <= 80
                    or _clean_text(dependency, 40) not in requirement_ids
                    for dependency in dependencies
                )
                or len({dependency.strip() for dependency in dependencies if isinstance(dependency, str)}) != len(dependencies)
            ):
                return reject("requirement_dependency_reference_invalid")

    capabilities = [
        item
        for item in raw_capabilities
        if isinstance(item, Mapping)
        and _clean_text(item.get("name"), 40).upper() in SUPPORTED_CAPABILITIES
    ]
    capability_names = {
        _clean_text(item.get("name"), 40).upper()
        for item in capabilities
    }
    if raw_requirements is not None:
        required_owners = {
            _clean_text(item.get("capability_owner"), 40).upper()
            for item in raw_requirements
            if isinstance(item, Mapping) and bool(item.get("required", True))
        }
        if any(
            isinstance(item, Mapping)
            and bool(item.get("required", True))
            and _clean_text(item.get("capability_owner"), 40).upper() not in capability_names
            for item in raw_requirements
        ):
            return reject("required_requirement_owner_missing_capability")
        required_task_owners = {
            _clean_text(item.get("owner_task_id"), 80)
            for item in raw_requirements
            if isinstance(item, Mapping) and bool(item.get("required", True))
        }
        if any(
            _clean_text(item.get("task_id"), 80) not in required_task_owners
            for item in capabilities
            if isinstance(item, Mapping) and bool(item.get("required", True))
        ):
            return reject("required_capability_missing_requirement_owner")
        owner_by_requirement = {
            _clean_text(item.get("requirement_id"), 40): _clean_text(item.get("capability_owner"), 40).upper()
            for item in raw_requirements
            if isinstance(item, Mapping)
        }
        capability_dependencies = {
            _clean_text(item.get("task_id"), 80): {
                _clean_text(dependency, 80)
                for dependency in item.get("depends_on", [])
                if isinstance(dependency, str)
            }
            for item in capabilities
            if isinstance(item, Mapping)
        }
        for item in raw_requirements:
            if not isinstance(item, Mapping) or not bool(item.get("required", True)):
                continue
            owner = _clean_text(item.get("capability_owner"), 40).upper()
            owner_task_id = _clean_text(item.get("owner_task_id"), 80)
            owner_dependencies = capability_dependencies.get(owner_task_id, set())
            for dependency in item.get("depends_on", []):
                dependency_owner = owner_by_requirement.get(_clean_text(dependency, 40))
                dependency_task_id = next(
                    (
                        _clean_text(req.get("owner_task_id"), 80)
                        for req in raw_requirements
                        if isinstance(req, Mapping)
                        and _clean_text(req.get("requirement_id"), 40) == _clean_text(dependency, 40)
                    ),
                    "",
                )
                if dependency_task_id and dependency_task_id != owner_task_id and dependency_task_id not in owner_dependencies:
                    return reject("requirement_dependency_cross_owner_missing_capability_dependency")
    has_specialized_capability = any(
        _clean_text(item.get("name"), 40).upper() != "CHAT"
        for item in capabilities
    )
    if has_specialized_capability:
        consumed_task_ids = _consumed_task_ids(capabilities)
        for item in capabilities:
            if _clean_text(item.get("name"), 40).upper() != "CHAT" or not bool(item.get("required", True)):
                continue
            if (
                (not _is_pipeline_step(item, consumed_task_ids) and not bool(item.get("independent")))
                or _clean_text(item.get("scope"), 40).upper() != "GENERAL_REASONING"
                or not _clean_text(item.get("evidence_span"), 180)
            ):
                return reject("required_chat_specialized_contract_invalid")

    def dependency_names(item: Mapping[str, Any]) -> set[str]:
        return {
            _clean_text(task_by_id.get(_clean_text(dependency, 80), {}).get("name"), 40).upper()
            for dependency in item.get("depends_on", [])
            if isinstance(dependency, str)
        }

    web_tasks = [
        item for item in capabilities
        if _clean_text(item.get("name"), 40).upper() == "WEB"
    ]
    for item in capabilities:
        if _clean_text(item.get("name"), 40).upper() != "IMAGE_ANALYZE" or not bool(item.get("required", True)):
            continue
        dependencies = dependency_names(item)
        if "WEB" not in dependencies:
            continue
        web = next(
            (task for task in web_tasks if _clean_text(task.get("task_id"), 80) in {
                _clean_text(dependency, 80) for dependency in item.get("depends_on", [])
            }),
            None,
        )
        if (
            web is None
            or not bool(web.get("required", True))
            or _clean_text(web.get("scope"), 40).upper() != "BROAD_RESEARCH"
            or not bool(web.get("independent_research", False))
            or _clean_text(item.get("scope"), 40).upper() != "MEDIA_ANALYSIS"
        ):
            return reject("web_image_dependency_contract_invalid")

    if not bool(external.get("required")):
        return None
    expected_content = _clean_text(external.get("expected_content"), 30).upper()
    source = next(
        (item for item in capabilities if _clean_text(item.get("name"), 40).upper() == "EXTERNAL_SOURCE"),
        None,
    )
    image = next(
        (item for item in capabilities if _clean_text(item.get("name"), 40).upper() == "IMAGE_ANALYZE"),
        None,
    )

    if expected_content == "MULTIMODAL":
        if source is None or not bool(source.get("required", True)):
            return reject("external_multimodal_source_required")
        if image is None or not bool(image.get("required", True)):
            return reject("external_multimodal_visual_capability_required")
        scope = _clean_text(image.get("scope"), 40).upper()
        dependencies = dependency_names(image)
        if scope != "MEDIA_ANALYSIS" or "EXTERNAL_SOURCE" not in dependencies:
            return reject("external_multimodal_visual_dependency_invalid")
    elif expected_content == "TEXT":
        if image is not None:
            dependencies = dependency_names(image)
            if "EXTERNAL_SOURCE" in dependencies:
                return reject("external_text_cannot_depend_on_external_source_visual")
    elif expected_content == "MEDIA" and image is not None:
        dependencies = dependency_names(image)
        if "EXTERNAL_SOURCE" not in dependencies:
            return reject("external_media_visual_dependency_invalid")
        if _clean_text(image.get("scope"), 40).upper() != "MEDIA_ANALYSIS":
            return reject("external_media_visual_scope_invalid")

    return None


_CAPABILITY_SCOPES = {
    "CHAT": "GENERAL_REASONING",
    "FOOTBALL": "STRUCTURED_DATA",
    "MINECRAFT": "STRUCTURED_DATA",
    "NINJAS": "STRUCTURED_DATA",
    "CODE": "STRUCTURED_DATA",
    "REMINDER": "STRUCTURED_DATA",
    "BIRTHDAY": "STRUCTURED_DATA",
    "TRANSLATE": "GENERAL_REASONING",
    "WEB": "BROAD_RESEARCH",
    "EXTERNAL_SOURCE": "EXPLICIT_SOURCE",
    "IMAGE_GENERATE": "IMAGE_GENERATION",
    "IMAGE_EDIT": "IMAGE_EDIT",
    "IMAGE_ANALYZE": "MEDIA_ANALYSIS",
    "MEME": "IMAGE_GENERATION",
    "REACTION": "DISCORD_ACTION",
    "ADMIN": "DISCORD_ACTION",
    "MEMORY": "DISCORD_ACTION",
    "CLARIFICATION": "GENERAL_REASONING",
}


def _capability_scope(name: str, value: object, *, external_required: bool) -> str:
    scope = _clean_text(value, 40).upper()
    if not scope:
        scope = "UNSPECIFIED"
    if scope in {
        "GENERAL_REASONING",
        "STRUCTURED_DATA",
        "BROAD_RESEARCH",
        "EXPLICIT_SOURCE",
        "MEDIA_ANALYSIS",
        "IMAGE_GENERATION",
        "IMAGE_EDIT",
        "DISCORD_ACTION",
        "UNSPECIFIED",
    }:
        if scope == "UNSPECIFIED":
            if name == "WEB" and external_required:
                return "EXPLICIT_SOURCE"
            return _CAPABILITY_SCOPES.get(name, "UNSPECIFIED")
        return scope
    return _CAPABILITY_SCOPES.get(name, "UNSPECIFIED")


def _normalize_requirements(value: object, *, native_task_contract: bool = False) -> tuple[CapabilityRequirement, ...]:
    if not isinstance(value, list):
        return ()
    requirements: list[CapabilityRequirement] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        requirement_id = _clean_text(raw.get("requirement_id"), 40)
        if not requirement_id or requirement_id in seen:
            continue
        dependencies = raw.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)):
            dependencies = ()
        seen.add(requirement_id)
        requirements.append(
            CapabilityRequirement(
                requirement_id=requirement_id,
                objective=_clean_text(raw.get("objective"), 240),
                required=bool(raw.get("required", True)),
                satisfaction_criterion=_clean_text(raw.get("satisfaction_criterion"), 240),
                capability_owner=_clean_text(raw.get("capability_owner"), 40).upper(),
                depends_on=tuple(
                    _clean_text(item, 40)
                    for item in dependencies
                    if _clean_text(item, 40)
                ),
                owner_task_id=_clean_text(raw.get("owner_task_id"), 80) or None,
            )
        )
    return tuple(requirements)


def _normalize_capabilities(
    value: object,
    *,
    action: str,
    external: Mapping[str, Any],
    native_task_contract: bool = False,
) -> tuple[CapabilityRequest, ...]:
    requests: list[CapabilityRequest] = []
    if isinstance(value, list):
        has_specialized_capability = any(
            isinstance(raw, Mapping)
            and _clean_text(raw.get("name"), 40).upper() not in {"", "CHAT"}
            for raw in value
        )
        raw_items = [raw for raw in value if isinstance(raw, Mapping)]
        consumed_task_ids = _consumed_task_ids(raw_items)
        names = [_clean_text(raw.get("name"), 40).upper() for raw in raw_items]
        explicit_ids = [_clean_text(raw.get("task_id"), 80) for raw in raw_items]
        name_counts: dict[str, int] = {}
        for name in names:
            name_counts[name] = name_counts.get(name, 0) + 1
        used_ids: set[str] = set()
        for index, raw in enumerate(raw_items):
            name = _clean_text(raw.get("name"), 40).upper()
            if name not in SUPPORTED_CAPABILITIES:
                continue
            dependencies = raw.get("depends_on", ())
            if not isinstance(dependencies, (list, tuple)):
                dependencies = ()
            scope = _capability_scope(name, raw.get("scope"), external_required=bool(external.get("required")))
            independent = bool(raw.get("independent", not (name == "CHAT" and has_specialized_capability))) or (
                name == "CHAT" and _is_pipeline_step(raw, consumed_task_ids)
            )
            independent_research = bool(
                raw.get("independent_research", name == "WEB" and not external.get("required"))
            )
            default_selection_count = 1
            try:
                selection_count = max(1, min(4, int(raw.get("selection_count", default_selection_count) or default_selection_count)))
            except (TypeError, ValueError):
                selection_count = default_selection_count
            raw_task_id = explicit_ids[index]
            if raw_task_id:
                task_id = raw_task_id
            elif native_task_contract:
                continue
            elif name_counts.get(name, 0) == 1:
                task_id = name
            else:
                task_id = f"{name}#{index + 1}"
            used_ids.add(task_id)
            depends_on = tuple(_clean_text(item, 80) for item in dependencies if _clean_text(item, 80))
            operands = raw.get("operands") if isinstance(raw.get("operands"), Mapping) else {}
            bindings: list[InputBinding] = []
            raw_bindings = raw.get("input_bindings", ())
            if isinstance(raw_bindings, (list, tuple)):
                for binding in raw_bindings:
                    if not isinstance(binding, Mapping):
                        continue
                    upstream = _clean_text(binding.get("upstream_task_id"), 80)
                    output = _clean_text(binding.get("output_field"), 120)
                    if upstream and output:
                        bindings.append(InputBinding(upstream, output))
            source_refs = raw.get("source_refs", ())
            requested_outputs = raw.get("requested_outputs", ())
            if name == "CHAT" and has_specialized_capability and not independent:
                continue
            if name == "WEB" and (
                scope != "BROAD_RESEARCH"
                or not independent_research
                or not _clean_text(raw.get("evidence_span"), 180)
            ):
                continue
            requests.append(
                CapabilityRequest(
                    name=name,
                    required=bool(raw.get("required", True)),
                    evidence_span=_clean_text(raw.get("evidence_span"), 180),
                    depends_on=depends_on,
                    scope=scope,
                    independent=independent,
                    independent_research=independent_research,
                    selection_count=selection_count,
                    owner_action=_clean_text(raw.get("owner_action"), 80).upper() or None,
                    task_id=task_id,
                    operands=own_operands(name, operands),
                    input_bindings=tuple(bindings),
                    source_refs=tuple(_clean_text(item, 120) for item in source_refs if _clean_text(item, 120)) if isinstance(source_refs, (list, tuple)) else (),
                    requested_outputs=tuple(_clean_text(item, 120) for item in requested_outputs if _clean_text(item, 120)) if isinstance(requested_outputs, (list, tuple)) else (),
                )
            )
    if external.get("required") and not any(item.name == "EXTERNAL_SOURCE" for item in requests) and not native_task_contract:
        requests.insert(
            0,
            CapabilityRequest(
                "EXTERNAL_SOURCE",
                evidence_span=_clean_text(external.get("evidence_span"), 180),
                scope="EXPLICIT_SOURCE",
            ),
        )
    if requests:
        by_id = {item.task_id: item for item in requests}
        by_name = {item.name: item for item in requests if name_counts.get(item.name, 0) == 1}
        if not native_task_contract:
            requests = [
                replace(
                    item,
                    depends_on=tuple(
                        by_name.get(dep, by_id.get(dep, None)).task_id
                        if by_name.get(dep, by_id.get(dep, None)) is not None
                        else dep
                        for dep in item.depends_on
                    ),
                )
                for item in requests
            ]
        by_id = {item.task_id: item for item in requests}
        ordered: list[CapabilityRequest] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                return
            item = by_id.get(name)
            if item is None:
                return
            visiting.add(name)
            for dependency in item.depends_on:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(item)

        for item in requests:
            visit(item.task_id)
        return tuple(ordered)
    if native_task_contract:
        return ()
    mapped = "FOOTBALL" if action.startswith("FOOTBALL_") else _ACTION_TO_CAPABILITY.get(action, "CHAT")
    return (CapabilityRequest(mapped, scope=_CAPABILITY_SCOPES.get(mapped, "UNSPECIFIED"), task_id=mapped),)


def normalize_capability_plan(
    payload: object,
    *,
    action: str,
    response_delivery: object = None,
    external_source: object = None,
    semantic_status: str | None = None,
    semantic_failure_reason: str | None = None,
) -> CapabilityPlan:
    has_semantic_payload = isinstance(payload, Mapping)
    raw = payload if has_semantic_payload else {}
    compatibility_action = _clean_text(action, 80).upper() or "CHAT"
    native_task_contract = has_semantic_payload and _clean_text(raw.get("semantic_source"), 30).lower() != "compatibility_action"
    native_validation_failure = (
        validate_native_capability_contract(
            {"capability_plan": raw, "action": compatibility_action}
            | ({"participation": "IGNORE"} if compatibility_action in {"IGNORE", "NONE"} else {})
        )  # ponytail: router already gated participation; NONE/IGNORE here is post-gate
        if native_task_contract
        else None
    )
    external = _normalize_external(raw.get("external_source", external_source))
    requirements = _normalize_requirements(raw.get("requirements"), native_task_contract=native_task_contract)
    capabilities = _normalize_capabilities(
        raw.get("capabilities"),
        action=compatibility_action,
        external=external,
        native_task_contract=native_task_contract,
    )
    objective = _clean_text(raw.get("objective"), 500)
    football = raw.get("football") if isinstance(raw.get("football"), Mapping) else {}
    if native_task_contract:
        typed_football = next(
            (
                item.get("operands")
                for item in raw.get("capabilities", [])
                if isinstance(item, Mapping)
                and _clean_text(item.get("name"), 40).upper() == "FOOTBALL"
                and isinstance(item.get("operands"), Mapping)
            ),
            None,
        )
        if typed_football is not None:
            football = typed_football
    football_payload: dict[str, Any] = {}
    for key, value in football.items():
        if key in {
            "player_candidates",
            "team_candidates",
            "coach_candidates",
            "venue_candidates",
            "league_candidates",
            "country_candidates",
            "bookmaker_candidates",
            "bet_candidates",
        }:
            if isinstance(value, (list, tuple)):
                football_payload[key] = [_clean_text(item, 120) for item in value[:8] if _clean_text(item, 120)]
            elif value is None:
                football_payload[key] = None
            continue
        if key in {
            "operation_family",
            "data_focus",
            "fixture_focus",
            "date_hint",
            "season_hint",
            "time_scope",
            "stat_focus",
            "player_candidates",
            "team_candidates",
            "coach_candidates",
            "venue_candidates",
            "league_candidates",
            "country_candidates",
            "reference_query",
            "country_filter",
        }:
            football_payload[key] = None if value is None else _clean_text(value, 180)
        elif key == "reference_page" and (value is None or (isinstance(value, int) and not isinstance(value, bool))):
            football_payload[key] = value
    minecraft = raw.get("minecraft") if isinstance(raw.get("minecraft"), Mapping) else {}
    minecraft_payload = {
        "address": _clean_text(minecraft.get("address"), 255) or None,
    }
    delivery = _normalize_delivery(raw.get("response_delivery", response_delivery))
    evidence_source = _clean_text(raw.get("evidence_source", "CURRENT_MESSAGE"), 40).upper() or "CURRENT_MESSAGE"
    if evidence_source not in {"CURRENT_MESSAGE", "EXPLICIT_REPLY_TARGET", "VALIDATED_ACTIVE_TYPED_CONTEXT", "CURRENT_MESSAGE_AND_REPLY"}:
        evidence_source = "CURRENT_MESSAGE"
    semantic_source = _clean_text(raw.get("semantic_source"), 30).lower()
    if semantic_source not in {"router", "compatibility_action"}:
        semantic_source = "router" if has_semantic_payload else "compatibility_action"
    normalized_semantic_status = _clean_text(
        raw.get("semantic_status", semantic_status or "SUCCESS"),
        40,
    ).upper()
    if normalized_semantic_status not in {"SUCCESS", "FAILED"}:
        normalized_semantic_status = "FAILED"
    normalized_failure_reason = _clean_text(
        raw.get("semantic_failure_reason", semantic_failure_reason or ""),
        160,
    ) or None
    plan_failure_reason: str | None = None
    if normalized_semantic_status == "FAILED":
        capabilities = ()
        semantic_source = "compatibility_action"
    elif native_task_contract and native_validation_failure:
        plan_failure_reason = native_validation_failure
        capabilities = ()
    normalized_action = compatibility_action
    if semantic_source == "router":
        action_owners = [
            item
            for item in capabilities
            if item.required and item.name in _CAPABILITY_OWNER_ACTIONS
        ]
        if action_owners:
            normalized_action = _clean_text(raw.get("primary_action"), 80).upper()
            primary_owner = action_owners[0]
            allowed_primary_actions = _CAPABILITY_OWNER_ACTIONS[primary_owner.name]
            mixed_plan = len(capabilities) > 1
            if not normalized_action:
                plan_failure_reason = "missing_field"
            elif not mixed_plan and normalized_action not in allowed_primary_actions:
                plan_failure_reason = "unknown_enum"
            projected: list[CapabilityRequest] = []
            for item in capabilities:
                owner_action = item.owner_action
                if item.required and item.name in _CAPABILITY_OWNER_ACTIONS:
                    allowed_actions = _CAPABILITY_OWNER_ACTIONS[item.name]
                    if owner_action is None:
                        plan_failure_reason = plan_failure_reason or "missing_field"
                    elif owner_action not in allowed_actions:
                        plan_failure_reason = plan_failure_reason or "unknown_enum"
                projected.append(replace(item, owner_action=owner_action))
            capabilities = tuple(projected)
    return CapabilityPlan(
        objective=objective,
        capabilities=capabilities,
        primary_action=normalized_action,
        response_delivery=delivery,
        external_source=external,
        football=football_payload,
        minecraft=minecraft_payload,
        evidence_source=evidence_source,
        semantic_source=semantic_source,
        semantic_status=normalized_semantic_status,
        semantic_failure_reason=normalized_failure_reason,
        valid=normalized_semantic_status == "SUCCESS" and plan_failure_reason is None,
        failure_reason=plan_failure_reason,
        requirements=requirements,
    )


def build_capability_execution_plan(plan: CapabilityPlan, *, fallback_action: str) -> CapabilityExecutionPlan:
    by_id: dict[str, CapabilityRequest] = {}
    by_name: dict[str, list[CapabilityRequest]] = {}
    for item in plan.capabilities:
        if item.task_id:
            by_id[item.task_id] = item
        by_name.setdefault(item.name, []).append(item)

    missing: set[str] = set()
    visiting: set[str] = set()
    visited: set[str] = set()
    cyclic: set[str] = set()
    ordered: list[CapabilityRequest] = []

    def resolve_dependency(item: CapabilityRequest, dependency: str) -> str | None:
        if dependency in by_id:
            return dependency
        family_matches = by_name.get(dependency, [])
        if len(family_matches) == 1 and plan.semantic_source != "router":
            return family_matches[0].task_id
        return None

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            cyclic.add(task_id)
            return
        item = by_id.get(task_id)
        if item is None:
            missing.add(task_id)
            return
        visiting.add(task_id)
        for dependency in item.depends_on:
            resolved_dependency = resolve_dependency(item, dependency)
            if resolved_dependency is None:
                missing.add(dependency)
            else:
                visit(resolved_dependency)
        visiting.remove(task_id)
        visited.add(task_id)
        ordered.append(item)

    for item in plan.capabilities:
        visit(item.task_id)

    fallback = _clean_text(fallback_action, 80).upper() or "CHAT"
    candidate_action = plan.primary_action if plan.semantic_source == "router" else fallback
    candidate_action = candidate_action or "CHAT"
    declared_capabilities = set(by_name)
    compatibility_mismatch: str | None = None
    if plan.semantic_source == "router" and any(item.name == "FOOTBALL" for item in plan.capabilities):
        compatibility_mismatch = (
            None
            if candidate_action == "FOOTBALL_LOOKUP"
            else f"{candidate_action}->FOOTBALL"
        )
        dispatch_action = (
            candidate_action
            if candidate_action in {"FOOTBALL_LIVE_WATCH_START", "FOOTBALL_LIVE_WATCH_STOP"}
            else "FOOTBALL_LOOKUP"
        )
    elif plan.semantic_source == "router" and plan.primary_capability in {"IMAGE_GENERATE", "IMAGE_EDIT"}:
        dispatch_action = _CAPABILITY_TO_ACTION[plan.primary_capability]
        compatibility_mismatch = (
            None
            if candidate_action == dispatch_action
            else f"{candidate_action}->{plan.primary_capability}"
        )
    elif plan.semantic_source == "router" and plan.primary_capability == "MEMORY":
        dispatch_action = plan.primary_action if plan.primary_action in _MEMORY_ACTIONS else "CHAT"
        compatibility_mismatch = (
            None
            if fallback == dispatch_action
            else f"{fallback}->MEMORY"
        )
    elif plan.semantic_source == "router" and plan.primary_capability == "ADMIN":
        dispatch_action = "ADMIN_ACTION" if plan.primary_action == "ADMIN_ACTION" else "CHAT"
        compatibility_mismatch = (
            None
            if fallback == dispatch_action
            else f"{fallback}->ADMIN"
        )
    else:
        mapped_capability = _capability_for_action(candidate_action)
        if mapped_capability is not None and mapped_capability not in declared_capabilities:
            compatibility_mismatch = f"{candidate_action}->{mapped_capability}"
            dispatch_action = "CHAT"
        else:
            dispatch_action = candidate_action
            if dispatch_action == "CHAT":
                mapped_action = _CAPABILITY_TO_ACTION.get(plan.primary_capability)
                if mapped_action is not None and plan.primary_capability in declared_capabilities:
                    dispatch_action = mapped_action
    return CapabilityExecutionPlan(
        semantic_plan=plan,
        ordered_capabilities=tuple(ordered),
        dispatch_action=dispatch_action,
        missing_dependencies=tuple(sorted(missing)),
        cyclic_capabilities=tuple(sorted(cyclic)),
        compatibility_mismatch=compatibility_mismatch,
        overflow=len(plan.capabilities) > 8,
    )


def plan_from_route_payload(payload: Mapping[str, Any]) -> CapabilityPlan:
    return normalize_capability_plan(
        payload.get("capability_plan"),
        action=_clean_text(payload.get("action", "CHAT"), 80).upper(),
        response_delivery=payload.get("response_delivery"),
        external_source=payload.get("external_source"),
    )


def football_plan_from_route_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The compatibility action and the legacy football planner are excluded; a compatibility fallback is allowed only when this typed plan is absent, never when it is present but incomplete."""

    if "capability_plan" in payload:
        plan = plan_from_route_payload(payload)
    else:
        # RouteDecision stores the already-normalized capability plan.  Accept
        # that representation without reconstructing authority from action.
        if "semantic_source" not in payload:
            return None
        if payload.get("semantic_source") == "compatibility_action":
            return None
        plan = normalize_capability_plan(
            payload,
            action=_clean_text(payload.get("primary_action", "CHAT"), 80).upper(),
            response_delivery=payload.get("response_delivery"),
            external_source=payload.get("external_source"),
        )
    if plan.semantic_source != "router":
        return None
    result = dict(plan.football)
    result["semantic_source"] = plan.semantic_source
    result["evidence_source"] = plan.evidence_source
    result["primary_action"] = plan.primary_action
    for capability in plan.capabilities:
        if capability.name == "FOOTBALL" and capability.evidence_span:
            result.setdefault("evidence_span", capability.evidence_span)
            break
    return result
