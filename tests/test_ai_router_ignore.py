import unittest

from services.ai_capability_orchestrator import validate_native_capability_contract


def payload(participation, action, capabilities=None, requirements=None):
    return {
        "participation": participation,
        "action": action,
        "participation_confidence": 1.0,
        "action_confidence": 1.0,
        "reason_code": "UNRELATED_HUMAN_CHAT",
        "capability_plan": {
            "objective": "ignore",
            "requirements": requirements or [],
            "capabilities": capabilities or [],
            "primary_action": action,
            "response_delivery": None,
            "external_source": None,
            "football": None,
            "minecraft": None,
            "evidence_source": "NONE",
            "valid": True,
            "failure_reason": None,
        },
    }


class RouterIgnoreContractTests(unittest.TestCase):
    def test_empty_ignore_is_accepted(self):
        self.assertIsNone(validate_native_capability_contract(payload("IGNORE", "IGNORE")))
        self.assertIsNone(validate_native_capability_contract(payload("IGNORE", "NONE")))


    def test_empty_respond_and_react_only_still_rejected(self):
        for participation in ("RESPOND", "REACT_ONLY"):
            self.assertEqual(
                validate_native_capability_contract(payload(participation, "NONE")),
                "inconsistent_capability_plan",
            )


    def test_ignore_with_too_many_capabilities_rejected(self):
        self.assertEqual(
            validate_native_capability_contract(payload("IGNORE", "IGNORE", capabilities=[{}] * 9)),
            "inconsistent_capability_plan",
        )


if __name__ == "__main__":
    unittest.main()
