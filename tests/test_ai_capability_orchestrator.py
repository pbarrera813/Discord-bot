from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from services.ai_capability_orchestrator import (
    CapabilityResultBundle,
    CapabilityTerminalStatus,
    InputBinding,
    normalize_terminal_result,
    normalize_capability_plan,
    validate_native_capability_contract,
)
from services.football_query_service import compile_football_operation
from services.ai_operation_catalog import operation_effect


def _migrate_native_fixture(payload, *, action="CHAT"):
    if not isinstance(payload, dict) or payload.get("semantic_source") == "compatibility_action":
        return payload
    result = deepcopy(payload)
    result.setdefault("objective", "")
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, list):
        return result
    result.setdefault("semantic_source", "router")
    names = [str(item.get("name") or "").strip().upper() for item in capabilities if isinstance(item, dict)]
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    task_ids = []
    for index, item in enumerate(capabilities):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().upper()
        task_id = str(item.get("task_id") or "").strip() or (name if counts.get(name) == 1 else f"{name}#{index + 1}")
        item["task_id"] = task_id
        item.setdefault("required", True)
        task_ids.append(task_id)
        item.setdefault("depends_on", [])
        item.setdefault("input_bindings", [])
        item.setdefault("source_refs", [])
        item.setdefault("independent", not (name == "CHAT" and len(capabilities) > 1))
        item.setdefault("independent_research", name == "WEB")
        item.setdefault("owner_action", None)
        item.setdefault("scope", {
            "CHAT": "GENERAL_REASONING", "WEB": "BROAD_RESEARCH", "EXTERNAL_SOURCE": "EXPLICIT_SOURCE",
            "IMAGE_ANALYZE": "MEDIA_ANALYSIS", "IMAGE_GENERATE": "IMAGE_GENERATION", "IMAGE_EDIT": "IMAGE_EDIT",
            "FOOTBALL": "STRUCTURED_DATA", "MINECRAFT": "STRUCTURED_DATA", "REACTION": "DISCORD_ACTION",
            "ADMIN": "DISCORD_ACTION", "MEMORY": "DISCORD_ACTION", "CLARIFICATION": "GENERAL_REASONING",
        }.get(name, "UNSPECIFIED"))
        item.setdefault("evidence_span", name.lower())
        item.setdefault("requested_outputs", {
            "WEB": ["answer"], "EXTERNAL_SOURCE": ["results"], "IMAGE_ANALYZE": ["text"],
            "IMAGE_GENERATE": ["artifact"], "IMAGE_EDIT": ["artifact"], "FOOTBALL": ["result"],
            "MINECRAFT": ["result"], "ADMIN": ["result"], "MEMORY": ["result"],
            "REACTION": ["reaction"], "CHAT": ["answer"], "CLARIFICATION": ["question"],
        }.get(name, ["result"]))
        operands = item.get("operands")
        if not isinstance(operands, dict):
            operands = {}
        if name == "ADMIN" and "admin_action" not in operands:
            admin = result.get("admin")
            if isinstance(admin, dict) and admin.get("admin_action"):
                operands["admin_action"] = admin["admin_action"]
        if name == "MEMORY" and "memory_action" not in operands:
            owner_action = str(item.get("owner_action") or "").strip().upper()
            if owner_action.startswith("SERVER_MEMORY_"):
                operands["memory_action"] = owner_action
            memory = result.get("memory")
            if isinstance(memory, dict):
                for key in ("memory_type", "subject", "key", "value", "scope"):
                    if key in memory:
                        operands.setdefault(key, memory[key])
        if name == "FOOTBALL":
            football = result.get("football")
            if isinstance(football, dict):
                for key, value in football.items():
                    if value is not None:
                        operands.setdefault(key, value)
            if "operation_family" not in operands:
                probe = compile_football_operation(
                    action if action.startswith("FOOTBALL_") else "FOOTBALL_LOOKUP",
                    "",
                    {"semantic_source": "router", **operands},
                )
                if probe.operation_type != "unknown":
                    operands["operation_family"] = probe.operation_type
            operands.setdefault("operation_family", "standings")
        elif name == "ADMIN":
            operands.setdefault("admin_action", "join_stats")
            item["owner_action"] = item.get("owner_action") or "ADMIN_ACTION"
        elif name == "MEMORY":
            operands.setdefault("memory_action", item.get("owner_action") or "SERVER_MEMORY_LOOKUP")
            item["owner_action"] = item.get("owner_action") or operands["memory_action"]
        elif name == "WEB":
            operands.setdefault("query", "legacy research")
        elif name == "EXTERNAL_SOURCE":
            operands.setdefault("expected_content", "TEXT")
        elif name == "IMAGE_GENERATE":
            operands.setdefault("prompt", "legacy image")
        elif name == "IMAGE_EDIT":
            operands.setdefault("prompt", "legacy edit")
        elif name == "IMAGE_ANALYZE":
            operands.setdefault("selection_count", 1)
        elif name == "REACTION":
            operands.setdefault("emoji", "👍")
        elif name == "MINECRAFT":
            operands.setdefault("address", "localhost")
        item["operands"] = operands
    requirements = result.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    owner_instances = {}
    for task in capabilities:
        if isinstance(task, dict):
            owner_instances.setdefault(str(task.get("name") or "").upper(), []).append(str(task.get("task_id")))
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        owner = str(requirement.get("capability_owner") or "").upper()
        if not requirement.get("owner_task_id") and owner_instances.get(owner):
            requirement["owner_task_id"] = owner_instances[owner].pop(0)
        requirement.setdefault("depends_on", [])
    if not requirements:
        for task in capabilities:
            if isinstance(task, dict):
                requirements.append({
                    "requirement_id": f"req_{task.get('task_id')}",
                    "objective": str(task.get("evidence_span") or task.get("name") or "task"),
                    "required": bool(task.get("required", True)),
                    "satisfaction_criterion": str(task.get("requested_outputs", ["result"])[0]),
                    "capability_owner": str(task.get("name") or "").upper(),
                    "owner_task_id": str(task.get("task_id")),
                    "depends_on": [],
                })
        result["requirements"] = requirements
    if not result.get("objective") and any(
        isinstance(task, dict) and str(task.get("name") or "").upper() in {"IMAGE_GENERATE", "IMAGE_EDIT"}
        for task in capabilities
    ):
        result["objective"] = "legacy native objective"
    return result


def _legacy_native_plan(payload, *, action, **kwargs):
    return normalize_capability_plan(_migrate_native_fixture(payload, action=action), action=action, **kwargs)


def _legacy_native_envelope(payload, *, action="CHAT"):
    result = deepcopy(payload)
    if isinstance(result.get("capability_plan"), dict):
        result["capability_plan"] = _migrate_native_fixture(result["capability_plan"], action=action)
    return result


class CapabilityPlanTests(unittest.TestCase):
    def test_native_repeated_family_requires_explicit_task_ids_and_preserves_task_contract(self) -> None:
        payload = {
            "objective": "research two independent sources",
            "semantic_source": "router",
            "requirements": [
                {"requirement_id": "r1", "objective": "research alpha", "required": True, "satisfaction_criterion": "summary", "capability_owner": "WEB", "owner_task_id": "research_a", "depends_on": []},
                {"requirement_id": "r2", "objective": "research beta", "required": True, "satisfaction_criterion": "summary", "capability_owner": "WEB", "owner_task_id": "research_b", "depends_on": ["r1"]},
            ],
            "capabilities": [
                {
                    "name": "WEB",
                    "task_id": "research_a",
                    "required": True,
                    "scope": "BROAD_RESEARCH",
                    "independent": True,
                    "owner_action": None,
                    "input_bindings": [],
                    "independent_research": True,
                    "evidence_span": "alpha",
                    "depends_on": [],
                    "operands": {"query": "alpha"},
                    "source_refs": ["message:1"],
"requested_outputs": ["answer"],
                },
                {
                    "name": "WEB",
                    "task_id": "research_b",
                    "required": True,
                    "scope": "BROAD_RESEARCH",
                    "independent": True,
                    "owner_action": None,
                    "independent_research": True,
                    "evidence_span": "beta",
                    "depends_on": ["research_a"],
                    "input_bindings": [{"upstream_task_id": "research_a", "output_field": "answer"}],
                    "operands": {"query": "beta"},
                    "source_refs": ["message:2"],
                    "requested_outputs": ["answer"],
                },
            ],
        }

        self.assertIsNone(validate_native_capability_contract({"capability_plan": payload}))
        plan = _legacy_native_plan(payload, action="CHAT")
        self.assertEqual([item.task_id for item in plan.capabilities], ["research_a", "research_b"])
        self.assertEqual(plan.capabilities[1].input_bindings, (InputBinding("research_a", "answer"),))
        serialized = plan.to_payload()["capabilities"]
        self.assertEqual(serialized[0]["operands"], {"query": "alpha"})
        self.assertEqual(serialized[1]["input_bindings"], [{"upstream_task_id": "research_a", "output_field": "answer"}])

    def test_native_contract_rejects_unknown_missing_task_ids_bindings_cycles_and_ninth_task(self) -> None:
        def reason(capabilities):
            return validate_native_capability_contract({"capability_plan": {"semantic_source": "router", "capabilities": capabilities}})

        self.assertEqual(reason([{"name": "UNKNOWN", "task_id": "x"}]), "inconsistent_capability_plan")
        self.assertEqual(reason([{"name": "WEB"}]), "inconsistent_capability_plan")
        self.assertEqual(
            reason([{"name": "WEB", "task_id": "a", "depends_on": ["missing"]}]),
            "inconsistent_capability_plan",
        )
        self.assertEqual(
            reason([
                {"name": "WEB", "task_id": "a", "depends_on": ["b"], "input_bindings": [{"upstream_task_id": "b", "output_field": "x"}]},
                {"name": "WEB", "task_id": "b", "depends_on": ["a"], "input_bindings": [{"upstream_task_id": "a", "output_field": "x"}]},
            ]),
            "inconsistent_capability_plan",
        )
        self.assertEqual(
            reason([{"name": "WEB", "task_id": str(index)} for index in range(9)]),
            "inconsistent_capability_plan",
        )

    def test_native_contract_rejects_two_producers_bound_to_one_consumer_output(self) -> None:
        capabilities = [
            {"name": "WEB", "task_id": "a", "depends_on": [], "requested_outputs": ["answer"]},
            {"name": "WEB", "task_id": "b", "depends_on": [], "requested_outputs": ["answer"]},
            {
                "name": "IMAGE_EDIT", "task_id": "edit", "depends_on": ["a", "b"],
                "input_bindings": [
                    {"upstream_task_id": "a", "output_field": "answer"},
                    {"upstream_task_id": "b", "output_field": "answer"},
                ],
                "operands": {"prompt": "edit"}, "requested_outputs": ["artifact"],
            },
        ]
        self.assertEqual(validate_native_capability_contract({"capability_plan": {"semantic_source": "router", "capabilities": capabilities}}), "inconsistent_capability_plan")


    def test_operation_effect_uses_canonical_typed_operation_fields(self) -> None:
        plan = _legacy_native_plan(
            {
                "semantic_source": "router",
                "capabilities": [
                    {"name": "ADMIN", "task_id": "stats", "required": True, "owner_action": "ADMIN_ACTION", "operands": {"admin_action": "join_stats"}, "evidence_span": "stats", "depends_on": [], "scope": "DISCORD_ACTION", "independent": True, "independent_research": False, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
                    {"name": "MEMORY", "task_id": "lookup", "required": True, "owner_action": "SERVER_MEMORY_LOOKUP", "operands": {"memory_action": "SERVER_MEMORY_LOOKUP"}, "evidence_span": "lookup", "depends_on": [], "scope": "DISCORD_ACTION", "independent": True, "independent_research": False, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
                    {"name": "FOOTBALL", "task_id": "watch", "required": True, "operands": {"operation_family": "live_watch_start"}, "evidence_span": "watch", "depends_on": [], "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
                ],
            },
            action="CHAT",
        )
        by_id = {item.task_id: item for item in plan.capabilities}
        self.assertEqual(operation_effect(by_id["stats"]), "read")
        self.assertEqual(operation_effect(by_id["lookup"]), "read")
        self.assertEqual(operation_effect(by_id["watch"]), "mutate")
        by_id["stats"] = replace(by_id["stats"], operands={"admin_action": "join_stats", "operation": "ban"})
        self.assertIsNone(operation_effect(by_id["stats"]))


    def test_required_chat_with_specialized_capability_requires_independent_contract(self) -> None:
        payload = {
            "external_source": {"required": True, "expected_content": "TEXT"},
            "capability_plan": {
                "requirements": [
                    {
                        "requirement_id": "reasoning",
                        "objective": "separate conceptual question",
                        "required": True,
                        "satisfaction_criterion": "general reasoning answer",
                        "capability_owner": "CHAT",
                    },
                    {
                        "requirement_id": "source",
                        "objective": "read explicit source",
                        "required": True,
                        "satisfaction_criterion": "grounded source result",
                        "capability_owner": "EXTERNAL_SOURCE",
                    },
                ],
                "capabilities": [
                    {
                        "name": "CHAT",
                        "required": True,
                        "scope": "GENERAL_REASONING",
                        "independent": False,
                        "evidence_span": "",
                    },
                    {"name": "EXTERNAL_SOURCE", "required": True, "scope": "EXPLICIT_SOURCE"},
                ],
            },
        }

        self.assertEqual(validate_native_capability_contract(payload), "inconsistent_capability_plan")

    def test_independent_chat_composition_remains_valid(self) -> None:
        payload = {
            "external_source": {"required": True, "expected_content": "TEXT"},
            "capability_plan": {
                "requirements": [
                    {
                        "requirement_id": "reasoning",
                        "objective": "separate conceptual question",
                        "required": True,
                        "satisfaction_criterion": "general reasoning answer",
                        "capability_owner": "CHAT",
                    },
                    {
                        "requirement_id": "source",
                        "objective": "read explicit source",
                        "required": True,
                        "satisfaction_criterion": "grounded source result",
                        "capability_owner": "EXTERNAL_SOURCE",
                    },
                ],
                "capabilities": [
                    {
                        "name": "CHAT",
                        "required": True,
                        "scope": "GENERAL_REASONING",
                        "independent": True,
                        "evidence_span": "separate conceptual question",
                    },
                    {"name": "EXTERNAL_SOURCE", "required": True, "scope": "EXPLICIT_SOURCE"},
                ],
            },
        }

        self.assertIsNone(validate_native_capability_contract(_legacy_native_envelope(payload)))
        plan = _legacy_native_plan(
            payload["capability_plan"],
            action="CHAT",
            external_source=payload["external_source"],
        )
        self.assertEqual([item.name for item in plan.capabilities], ["CHAT", "EXTERNAL_SOURCE"])

    def test_complete_source_media_contract_is_native_and_structurally_ordered(self) -> None:
        payload = {
            "external_source": {"required": True, "expected_content": "MULTIMODAL"},
            "capability_plan": {
                "requirements": [
                    {
                        "requirement_id": "source",
                        "objective": "read explicit source",
                        "required": True,
                        "satisfaction_criterion": "grounded source result",
                        "capability_owner": "EXTERNAL_SOURCE",
                    },
                    {
                        "requirement_id": "visual",
                        "objective": "inspect source media",
                        "required": True,
                        "satisfaction_criterion": "authoritative visual result",
                        "capability_owner": "IMAGE_ANALYZE",
                        "depends_on": ["source"],
                    },
                ],
                "capabilities": [
                    {"name": "EXTERNAL_SOURCE", "required": True},
                    {
                        "name": "IMAGE_ANALYZE",
                        "required": True,
                        "scope": "MEDIA_ANALYSIS",
                        "depends_on": ["EXTERNAL_SOURCE"],
                    },
                ],
            },
        }

        self.assertIsNone(validate_native_capability_contract(_legacy_native_envelope(payload)))
        plan = _legacy_native_plan(
            payload["capability_plan"], action="CHAT", external_source=payload["external_source"]
        )
        self.assertEqual([item.name for item in plan.capabilities], ["EXTERNAL_SOURCE", "IMAGE_ANALYZE"])
        self.assertEqual(plan.capabilities[-1].depends_on, ("EXTERNAL_SOURCE",))


    def test_native_validation_diagnostic_is_bounded_and_identifies_first_predicate(self) -> None:
        payload = {
            "external_source": {"required": False, "expected_content": "UNKNOWN"},
            "capability_plan": {
                "objective": "Research and inspect acquired media.",
                "requirements": [
                    {
                        "requirement_id": "research",
                        "objective": "current research",
                        "required": True,
                        "satisfaction_criterion": "grounded result",
                        "capability_owner": "WEB",
                        "depends_on": [],
                    },
                    {
                        "requirement_id": "visual",
                        "objective": "inspect acquired media",
                        "required": True,
                        "satisfaction_criterion": "visual observation",
                        "capability_owner": "IMAGE_ANALYZE",
                        "depends_on": ["research"],
                    },
                ],
                "capabilities": [
                    {
                        "name": "WEB",
                        "task_id": "web",
                        "required": True,
                        "scope": "EXPLICIT_SOURCE",
                        "independent": True,
                        "independent_research": True,
                        "depends_on": [],
                        "evidence_span": "current research",
                        "operands": {"query": "current research"},
                        "input_bindings": [],
                        "source_refs": [],
                        "requested_outputs": ["answer"],
                    },
                    {
                        "name": "IMAGE_ANALYZE",
                        "task_id": "image",
                        "required": True,
                        "scope": "MEDIA_ANALYSIS",
                        "independent": False,
                        "independent_research": False,
                        "depends_on": ["web"],
                        "evidence_span": "inspect acquired media",
                        "operands": {"selection_count": 1},
                        "input_bindings": [],
                        "source_refs": [],
                        "requested_outputs": ["text"],
                    },
                ],
            },
        }
        diagnostics: list[dict[str, object]] = []

        result = validate_native_capability_contract(_legacy_native_envelope(payload), diagnostic_sink=diagnostics)

        self.assertEqual(result, "inconsistent_capability_plan")
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual(diagnostic["predicate"], "web_image_dependency_contract_invalid")
        typed_state = diagnostic["typed_state"]
        self.assertEqual(typed_state["capability_count"], 2)
        self.assertEqual(typed_state["requirement_count"], 2)
        self.assertEqual(typed_state["capabilities"][0]["scope"], "EXPLICIT_SOURCE")
        self.assertEqual(typed_state["requirements"][0]["objective"], {"present": True, "length": 16})
        self.assertNotIn("current research", repr(diagnostic))

    def test_cross_owner_requirement_dependency_without_capability_edge_fails_closed(self) -> None:
        payload = {
            "external_source": {"required": True, "expected_content": "TEXT"},
            "capability_plan": {
                "objective": "Retrieve and contrast the current table.",
                "requirements": [
                    {
                        "requirement_id": "req_table",
                        "objective": "retrieve the current table",
                        "required": True,
                        "satisfaction_criterion": "typed football standings",
                        "capability_owner": "FOOTBALL",
                        "owner_task_id": "football",
                        "depends_on": [],
                    },
                    {
                        "requirement_id": "req_contrast",
                        "objective": "contrast the table with the supplied source",
                        "required": True,
                        "satisfaction_criterion": "grounded source comparison",
                        "capability_owner": "EXTERNAL_SOURCE",
                        "owner_task_id": "source",
                        "depends_on": ["req_table"],
                    },
                ],
                "capabilities": [
                    {"name": "FOOTBALL", "task_id": "football", "required": True, "scope": "STRUCTURED_DATA", "independent": True, "independent_research": False, "owner_action": None, "evidence_span": "table", "depends_on": [], "operands": {"operation_family": "standings"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["result"]},
                    {"name": "EXTERNAL_SOURCE", "task_id": "source", "required": True, "scope": "EXPLICIT_SOURCE", "independent": True, "independent_research": False, "owner_action": None, "evidence_span": "source", "depends_on": [], "operands": {"expected_content": "TEXT"}, "input_bindings": [], "source_refs": [], "requested_outputs": ["results"]},
                ],
            },
        }
        diagnostics: list[dict[str, object]] = []

        result = validate_native_capability_contract(payload, diagnostic_sink=diagnostics)

        self.assertEqual(result, "inconsistent_capability_plan")
        self.assertEqual(diagnostics[0]["predicate"], "requirement_dependency_cross_owner_missing_capability_dependency")


    def test_requirements_bind_each_required_outcome_to_a_declared_capability(self) -> None:
        payload = {
            "external_source": {"required": False, "expected_content": "UNKNOWN"},
            "capability_plan": {
                "requirements": [
                    {
                        "requirement_id": "research",
                        "objective": "current research",
                        "required": True,
                        "satisfaction_criterion": "grounded research result",
                        "capability_owner": "WEB",
                        "depends_on": [],
                    },
                    {
                        "requirement_id": "visual",
                        "objective": "inspect discovered media",
                        "required": True,
                        "satisfaction_criterion": "authoritative visual observation",
                        "capability_owner": "IMAGE_ANALYZE",
                        "depends_on": ["research"],
                    },
                ],
                "capabilities": [
                    {"name": "WEB", "required": True, "scope": "BROAD_RESEARCH", "independent_research": True, "evidence_span": "current research"},
                    {"name": "IMAGE_ANALYZE", "required": True, "scope": "MEDIA_ANALYSIS", "depends_on": ["WEB"], "selection_count": 1, "evidence_span": "inspect media"},
                ],
            },
        }

        self.assertIsNone(validate_native_capability_contract(_legacy_native_envelope(payload, action="WEB_LOOKUP")))
        plan = _legacy_native_plan(payload["capability_plan"], action="WEB_LOOKUP")
        self.assertEqual([item.capability_owner for item in plan.requirements], ["WEB", "IMAGE_ANALYZE"])
        self.assertEqual(plan.capabilities[-1].selection_count, 1)

    def test_requirement_owner_mismatch_fails_without_semantic_repair(self) -> None:
        payload = {
            "external_source": {"required": False, "expected_content": "UNKNOWN"},
            "capability_plan": {
                "requirements": [{
                    "requirement_id": "visual",
                    "objective": "inspect discovered media",
                    "required": True,
                    "satisfaction_criterion": "visual observation",
                    "capability_owner": "IMAGE_ANALYZE",
                    "depends_on": [],
                }],
                "capabilities": [{"name": "WEB", "required": True, "scope": "BROAD_RESEARCH", "independent_research": True}],
            },
        }

        self.assertEqual(validate_native_capability_contract(payload), "inconsistent_capability_plan")



    def test_native_football_normalization_preserves_all_typed_fields_and_types(self) -> None:
        football = {
            "operation_family": "odds_mapping",
            "data_focus": "odds_mapping",
            "fixture_focus": "named fixture",
            "date_hint": "2024-03-31",
            "season_hint": "2024",
            "time_scope": "specific_date",
            "stat_focus": "goals",
            "player_candidates": ["Player A"],
            "team_candidates": ["Team A"],
            "coach_candidates": ["Coach A"],
            "venue_candidates": ["Venue A"],
            "league_candidates": ["League A"],
            "country_candidates": ["Country A"],
            "bookmaker_candidates": ["Bookmaker A"],
            "bet_candidates": ["Match Winner"],
            "reference_query": "catalog query",
            "country_filter": "name",
            "reference_page": 2,
        }
        plan = _legacy_native_plan(
            {"capabilities": [{"name": "FOOTBALL", "required": True}], "football": football},
            action="FOOTBALL_LOOKUP",
        )

        self.assertEqual(plan.football, football)
        self.assertIsInstance(plan.football["reference_page"], int)
        self.assertIsNone(validate_native_capability_contract(_legacy_native_envelope(
            {"capability_plan": {"capabilities": [{"name": "FOOTBALL"}], "football": football}},
            action="FOOTBALL_LOOKUP",
        )))


    def test_duplicate_capability_declarations_fail_closed(self) -> None:
        payload = {
            "capability_plan": {
                "capabilities": [
                    {"name": "FOOTBALL", "required": True},
                    {"name": "FOOTBALL", "required": True},
                ]
            }
        }

        self.assertEqual(validate_native_capability_contract(payload), "inconsistent_capability_plan")



    def test_router_required_owner_action_fails_closed_when_missing_or_invalid(self) -> None:
        for label, owner_action, expected_reason in (
            ("missing", None, "inconsistent_capability_plan"),
            ("invalid", "MINECRAFT_SERVER_STATUS", "unknown_enum"),
        ):
            with self.subTest(label=label):
                memory = {"name": "MEMORY", "required": True, "owner_action": "SERVER_MEMORY_LOOKUP"}
                payload = _migrate_native_fixture({
                        "objective": "Perform the requested server actions.",
                        "capabilities": [
                            {"name": "MINECRAFT", "required": True, "owner_action": "MINECRAFT_SERVER_STATUS"},
                            memory,
                        ],
                        "primary_action": "MINECRAFT_SERVER_STATUS",
                        "semantic_source": "router",
                    },
                    action="MINECRAFT_SERVER_STATUS",
                )
                if owner_action is None:
                    payload["capabilities"][1].pop("owner_action")
                else:
                    payload["capabilities"][1]["owner_action"] = owner_action
                plan = normalize_capability_plan(payload, action="MINECRAFT_SERVER_STATUS")

                self.assertFalse(plan.valid)
                self.assertEqual(plan.failure_reason, expected_reason)



    def test_terminal_outcomes_preserve_native_status_and_distinguish_satisfaction(self) -> None:
        no_data = normalize_terminal_result(
            "FOOTBALL",
            "NO_DATA_FOR_SCOPE",
            provider="api-football",
            provenance={"source": "FootballRetrievalResult"},
        )
        provider_failure = normalize_terminal_result(
            "EXTERNAL_SOURCE",
            "TRANSPORT_FAILURE",
            provider="external_sources",
        )

        self.assertEqual(no_data.status, CapabilityTerminalStatus.NO_DATA)
        self.assertEqual(no_data.native_outcome, "NO_DATA_FOR_SCOPE")
        self.assertFalse(no_data.satisfies_request)
        self.assertEqual(provider_failure.status, CapabilityTerminalStatus.PROVIDER_FAILURE)
        self.assertEqual(provider_failure.native_outcome, "TRANSPORT_FAILURE")



    def test_semantic_failure_cannot_become_satisfied_from_compatibility_chat(self) -> None:
        result = normalize_terminal_result(
            "CHAT",
            "SUCCESS",
            provider="compatibility_chat",
            satisfies_request=True,
        )
        bundle = CapabilityResultBundle(
            required_capabilities=("CHAT",),
            results=(result,),
            semantic_status="FAILED",
            semantic_failure_reason="semantic_output_truncated",
        )

        self.assertTrue(bundle.complete)
        self.assertFalse(bundle.satisfied)
        self.assertEqual(bundle.request_status, "SEMANTIC_INTERPRETATION_FAILED")
        self.assertEqual(bundle.semantic_failure_reason, "semantic_output_truncated")



if __name__ == "__main__":
    unittest.main()
