from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
import hashlib
from typing import Any, Awaitable, Callable, Mapping
import inspect

from services.ai_operation_catalog import operation_effect


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
    requirement_assessments: dict[str, dict[str, Any]] | None = None
    evidence_requirements: dict[str, tuple[str, ...]] = field(default_factory=dict)
    investigation_stop_reason: str | None = None

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
        protected = tuple(
            item.task_id for item in self.execution_plan.ordered_capabilities if item.required
            and (operation_effect(item) in {"mutate", "artifact"}
                 or (item.name == "CODE" and item.operands.get("code_action") == "run"))
        )
        return CapabilityResultBundle(
            required_capabilities=self.execution_plan.required_capabilities,
            results=tuple(self.results.values()),
            semantic_status=self.execution_plan.semantic_plan.semantic_status,
            semantic_failure_reason=self.execution_plan.semantic_plan.semantic_failure_reason,
            required_task_ids=self.execution_plan.required_task_ids,
            requirement_assessments=self.requirement_assessments,
            reviewed_requirement_ids=tuple(
                item.requirement_id for item in self.execution_plan.semantic_plan.requirements
                if item.required and item.owner_task_id not in protected
            ) if self.execution_plan.semantic_plan.requirements else None,
            protected_task_ids=protected,
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
            "requirement_statuses": {key: value["status"] for key, value in (self.requirement_assessments or {}).items()},
            "investigation_stop_reason": self.investigation_stop_reason,
        }


@dataclass(frozen=True)
class CapabilityResultBundle:
    required_capabilities: tuple[str, ...]
    results: tuple[CapabilityTerminalResult, ...] = ()
    semantic_status: str = "SUCCESS"
    semantic_failure_reason: str | None = None
    required_task_ids: tuple[str, ...] = ()
    requirement_assessments: Mapping[str, Mapping[str, Any]] | None = None
    reviewed_requirement_ids: tuple[str, ...] | None = None
    protected_task_ids: tuple[str, ...] = ()

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
        if self.reviewed_requirement_ids is not None:
            assessments = self.requirement_assessments or {}
            missing = [key for key in self.reviewed_requirement_ids if assessments.get(key, {}).get("status") != "supported"]
            missing.extend(key for key in self.protected_task_ids
                           if key not in self.by_task or not self.by_task[key].satisfies_request)
            return tuple(missing)
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
            "requirement_assessments": self.requirement_assessments,
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
    can_continue: Callable[[], bool] | None = None,
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
                # A preflight may refuse with a reason string, which reaches the caller so the arguments can be fixed.
                preflight_allowed = bool(decision) and not isinstance(decision, str)
                preflight_reason = decision if isinstance(decision, str) and decision else "mutation_batch_preflight_failed"
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
        if can_continue is not None and not can_continue():
            break
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


@dataclass(frozen=True)
class CapabilityPlan:
    objective: str
    capabilities: tuple[CapabilityRequest, ...]
    primary_action: str
    response_delivery: dict[str, Any]
    football: dict[str, Any]
    minecraft: dict[str, Any] = field(default_factory=dict)
    evidence_source: str = "CURRENT_MESSAGE"
    semantic_source: str = "router"
    semantic_status: str = "SUCCESS"
    semantic_failure_reason: str | None = None
    valid: bool = True
    failure_reason: str | None = None
    requirements: tuple[CapabilityRequirement, ...] = ()

    def has(self, name: str) -> bool:
        return any(item.name == name for item in self.capabilities)


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


def evidence_read_key(request: CapabilityRequest) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            return {key: normalize(item) for key, item in value.items() if item is not None and item != []}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return json.dumps([request.name, normalize(request.operands)], sort_keys=True, default=str, separators=(",", ":"))


def hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
