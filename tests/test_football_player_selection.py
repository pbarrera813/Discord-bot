from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from cogs.ai_chat import AIChatCog
from services import football_resolver
from services.xai_client import XAIClient
from services.football_query_service import compile_football_operation
from tests.test_football_commands import _FakeFootballClient, _player_slot, _value


def _row(player_id: int, *, first: str | None, age: int, team: str) -> dict[str, object]:
    player: dict[str, object] = {"id": player_id, "name": "J. Bellingham", "lastname": "Bellingham", "age": age, "nationality": "England"}
    if first is not None:
        player["firstname"] = first
    return {"player": player, "statistics": [{"team": {"id": player_id * 10, "name": team}}]}


JUDE_FULL = _row(1, first="Jude Victor William", age=22, team="Real Madrid")
JOBE_FULL = _row(2, first="Jobe Alexander", age=20, team="Sunderland")
JUDE_SHORT = _row(1, first=None, age=22, team="Real Madrid")
JOBE_SHORT = _row(2, first=None, age=20, team="Sunderland")


def _client(*rows: dict[str, object]):
    class _Client(_FakeFootballClient):
        async def search_player_profiles(self, request):
            self._record("search_players", search=_value(getattr(request, "lastname", None)))
            return [dict(row) for row in rows]

    return _Client()


class ResolverSelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_identity_choices_stay_within_provider_rows_and_country_scope(self) -> None:
        slot = compile_football_operation("FOOTBALL_LOOKUP", "Northbank Town", {
            "semantic_source": "router", "operation_family": "team_seasons",
            "team_candidates": ["Northbank Town"],
        }).team_slots[0]
        rows = [{"team": {"id": n, "name": f"Other club {n}", "country": "France"}} for n in range(1, 7)]
        rows += [{"team": {"id": 7, "name": "Northbank", "country": "Ireland"}},
                 {"team": {"id": 8, "name": "Northbank", "country": "Canada"}}]
        client = XAIClient("test-key", "test-model")
        captured = []
        verdict = {}

        async def choose(**kwargs):
            captured.append(kwargs["request"])
            return verdict

        client._small_json_call = choose
        cog = AIChatCog.__new__(AIChatCog)
        cog.bot = SimpleNamespace(llm_client=client)
        request = "Which seasons are registered for Northbank Town?"
        history = [{"role": "user", "content": "We were discussing Canadian football."}]
        selector = cog._football_identity_selector(request_text=request, history=history)
        for ref, confidence, expected in (("c8", 0.95, 8), ("unknown", 0.95, None),
                                          (None, 0.95, None), ("c8", float("nan"), None)):
            with self.subTest(ref=ref, confidence=confidence):
                verdict = {"ref": ref, "confidence": confidence}
                result = await football_resolver.select_football_identity(rows, slot, entity_type="team", selector=selector)
                self.assertEqual(result.selected["team"]["id"] if result.selected else None, expected)
                self.assertEqual(captured[-1]["request"], request)
                self.assertEqual(captured[-1]["conversation"], history)
                self.assertEqual(len(captured[-1]["candidates"]), 8)

        verdict = {"ref": "c1", "confidence": 0.95}
        result = await football_resolver.select_football_identity(
            rows, replace(slot, country_hint="Ireland"), entity_type="team", selector=selector,
        )
        self.assertEqual(result.selected["team"]["id"], 7)
        self.assertEqual([row["country"] for row in captured[-1]["candidates"]], ["Ireland"])

    async def _resolve(self, rows, selector, query="J. Bellingham"):
        return await football_resolver.resolve_player(_client(*rows), _player_slot(query), selector=selector)

    async def test_without_a_selector_the_initial_stays_unresolved(self) -> None:
        for rows in ((JUDE_FULL, JOBE_FULL), (JUDE_SHORT, JOBE_SHORT)):
            lookup = await football_resolver.resolve_player(_client(*rows), _player_slot("J. Bellingham"))
            self.assertIsNone(lookup.resolution.selected)


    async def test_the_selector_is_not_asked_when_the_name_already_resolves(self) -> None:
        calls: list[object] = []

        async def selector(candidates):
            calls.append(candidates)
            return 1

        lookup = await football_resolver.resolve_player(
            _client({"player": {"id": 1, "name": "Jude Bellingham"}, "statistics": []}),
            _player_slot("Jude Bellingham"),
            selector=selector,
        )
        self.assertEqual((lookup.resolution.selected["player"]["id"], calls), (1, []))


if __name__ == "__main__":
    unittest.main()
