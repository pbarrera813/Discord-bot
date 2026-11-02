from __future__ import annotations


import unittest

from services.ai_operation_catalog import validate_operands
from services.xai_client import XAIClient

URL = "https://domain-a.test/a"


def _external_route(operands: dict[str, object], evidence: str = URL) -> dict[str, object]:
    delivery = {
        "modality": "TEXT", "explicit": False, "source": "UNSPECIFIED",
        "evidence_span": "", "semantic_reason": "default text delivery", "artifacts": [],
    }
    route = {
        "participation": "RESPOND", "action": "CHAT",
        "participation_confidence": 1.0, "action_confidence": 1.0,
        "reason_code": "DIRECT_REQUEST", "resolved_request": "read the linked page",
        "send_text": True, "response_delivery": delivery,
        "capability_plan": {
            "objective": "read the linked page", "semantic_source": "router",
            "semantic_status": "SUCCESS", "primary_action": "CHAT",
            "requirements": [{
                "requirement_id": "source", "objective": "read the linked page", "required": True,
                "satisfaction_criterion": "The page text is retrieved.",
                "capability_owner": "EXTERNAL_SOURCE", "owner_task_id": "external_1", "depends_on": [],
            }],
            "capabilities": [{
                "task_id": "external_1", "name": "EXTERNAL_SOURCE", "required": True,
                "evidence_span": evidence, "depends_on": [],
                "scope": "EXPLICIT_SOURCE", "independent": True, "independent_research": False,
                "owner_action": None, "operands": operands, "input_bindings": [],
                "source_refs": [], "requested_outputs": ["results"],
            }],
            "response_delivery": delivery,
        },
        "valid": True, "failure": False,
    }
    return XAIClient("test-key", "test-model")._validate_route_decision(
        "", has_supported_image=False, parsed=route,
    )


def _operands(**overrides: object) -> dict[str, object]:
    base = {
        "source_scope": "CURRENT_MESSAGE", "expected_content": "TEXT",
        "operation": "RETRIEVE",
    }
    base.update(overrides)
    return base


class ExternalSourceOperandContractTests(unittest.TestCase):
    def test_evidence_and_task_owned_flags_are_not_operands(self) -> None:
        self.assertIsNone(validate_operands("EXTERNAL_SOURCE", _operands()))
        for key in ("required", "explicit", "evidence_span", "bogus"):
            with self.subTest(key=key):
                self.assertEqual(
                    validate_operands("EXTERNAL_SOURCE", _operands(**{key: True})), "unknown_task_operand"
                )



if __name__ == "__main__":
    unittest.main()
