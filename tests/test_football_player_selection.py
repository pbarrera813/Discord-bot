from __future__ import annotations

import unittest

from services import football_resolver
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
