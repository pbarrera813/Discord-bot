import unittest


from services.ai_operation_catalog import known_output_fields


PRODUCTION_GREETING_ROUTE = '{"participation":"RESPOND","action":"CHAT","participation_confidence":0.95,"action_confidence":0.9,"reason_code":"DIRECT_REQUEST","resolved_request":null,"target_message":null,"emoji":null,"emojis":[],"send_text":true,"response_delivery":{"modality":"UNSPECIFIED","explicit":false,"source":"UNSPECIFIED","evidence_span":"","semantic_reason":"","failure_reason":null,"ambiguous_reason":null,"artifacts":[]},"external_source":{"required":false,"operation":"UNSPECIFIED","source_scope":"UNSPECIFIED","explicit":false,"evidence_span":"","expected_content":"UNKNOWN","semantic_reason":"","failure_reason":null,"ambiguous_reason":null},"capability_plan":{"objective":"Respond conversationally to the direct greeting and question about the bot\'s day.","requirements":[{"requirement_id":"req_chat_1","objective":"Provide a friendly chat response to the user\'s greeting.","required":true,"satisfaction_criterion":"Natural conversational reply acknowledging the greeting.","capability_owner":"CHAT","depends_on":[],"owner_task_id":"chat_task_1"}],"capabilities":[{"name":"CHAT","task_id":"chat_task_1","required":true,"evidence_span":"hi! How\'s your day going?","depends_on":[],"scope":"GENERAL_REASONING","independent":true,"independent_research":false,"owner_action":null,"operands":{},"input_bindings":[],"source_refs":[],"requested_outputs":["result"]}],"primary_action":"CHAT","response_delivery":{"modality":"UNSPECIFIED","explicit":false,"source":"UNSPECIFIED","evidence_span":"","semantic_reason":"","failure_reason":null,"ambiguous_reason":null,"artifacts":[]},"external_source":{"required":false,"operation":"UNSPECIFIED","source_scope":"UNSPECIFIED","explicit":false,"evidence_span":"","expected_content":"UNKNOWN","semantic_reason":"","failure_reason":null,"ambiguous_reason":null},"football":{"operation_family":null,"data_focus":null,"fixture_focus":null,"date_hint":null,"season_hint":null,"time_scope":null,"stat_focus":null,"player_candidates":[],"team_candidates":[],"coach_candidates":[],"venue_candidates":[],"league_candidates":[],"country_candidates":[],"bookmaker_candidates":[],"bet_candidates":[],"reference_query":null,"country_filter":null,"reference_page":null},"minecraft":{"address":null},"evidence_source":"CURRENT_MESSAGE","valid":true,"failure_reason":null},"pending_operation":null,"memory":null,"admin":null}'


class RouterOutputVocabularyTests(unittest.TestCase):

    def test_every_family_accepts_result_as_its_whole_terminal_result(self) -> None:
        for family in ("CHAT", "CLARIFICATION", "REACTION", "WEB", "EXTERNAL_SOURCE", "FOOTBALL", "MINECRAFT", "NINJAS", "CODE", "REMINDER", "BIRTHDAY", "TRANSLATE", "ADMIN", "MEMORY", "IMAGE_ANALYZE"):
            with self.subTest(family=family):
                self.assertIn("result", known_output_fields(family))
        for family in ("IMAGE_GENERATE", "IMAGE_EDIT"):
            self.assertIn("artifact", known_output_fields(family))


if __name__ == "__main__":
    unittest.main()
