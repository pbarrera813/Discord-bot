from __future__ import annotations

import unittest

from services.football_watch import (
    build_watch_updates,
    is_terminal_status,
    should_fetch_statistics,
    snapshot_from_fixture,
)


def _fixture(*, home: int = 0, away: int = 0, status: str = "1H", elapsed: int | None = 10):
    return {
        "fixture": {"id": 77, "status": {"short": status, "elapsed": elapsed}},
        "teams": {
            "home": {"id": 1, "name": "Francia"},
            "away": {"id": 2, "name": "Inglaterra"},
        },
        "goals": {"home": home, "away": away},
    }


class FootballWatchTests(unittest.TestCase):
    def test_goal_with_assist_and_dedupe(self) -> None:
        event = {
            "time": {"elapsed": 37},
            "team": {"id": 2, "name": "Inglaterra"},
            "player": {"id": 9, "name": "Bukayo Saka"},
            "assist": {"id": 8, "name": "Declan Rice"},
            "type": "Goal",
            "detail": "Normal Goal",
        }
        seen: set[str] = set()
        updates, snapshot = build_watch_updates(
            previous=snapshot_from_fixture(_fixture(home=0, away=0, elapsed=30)),
            current=snapshot_from_fixture(_fixture(home=0, away=1, elapsed=37)),
            fixture=_fixture(home=0, away=1, elapsed=37),
            events=[event],
            seen_event_keys=seen,
            emitted_checkpoints=set(),
        )

        self.assertEqual(len(updates), 1)
        self.assertIn("Gol 37'", updates[0].text)
        self.assertIn("Bukayo Saka", updates[0].text)
        self.assertIn("Declan Rice", updates[0].text)
        seen.add(updates[0].event_key or "")

        updates_again, _snapshot = build_watch_updates(
            previous=snapshot,
            current=snapshot,
            fixture=_fixture(home=0, away=1, elapsed=37),
            events=[event],
            seen_event_keys=seen,
            emitted_checkpoints=set(),
        )
        self.assertEqual(updates_again, [])

    def test_event_types_are_formatted(self) -> None:
        fixture = _fixture(home=1, away=1, elapsed=65)
        events = [
            {"time": {"elapsed": 5}, "team": {"id": 1, "name": "Francia"}, "player": {"name": "A"}, "type": "Goal", "detail": "Own Goal"},
            {"time": {"elapsed": 22}, "team": {"id": 2, "name": "Inglaterra"}, "player": {"name": "B"}, "type": "Goal", "detail": "Missed Penalty"},
            {"time": {"elapsed": 44}, "team": {"id": 1, "name": "Francia"}, "player": {"name": "C"}, "type": "Card", "detail": "Yellow Card"},
            {"time": {"elapsed": 55}, "team": {"id": 2, "name": "Inglaterra"}, "player": {"name": "D"}, "assist": {"name": "E"}, "type": "subst", "detail": "Substitution"},
            {"time": {"elapsed": 60}, "team": {"id": 1, "name": "Francia"}, "player": {"name": "F"}, "type": "Var", "detail": "Goal cancelled"},
        ]

        updates, _snapshot = build_watch_updates(
            previous=snapshot_from_fixture(_fixture(home=1, away=0, elapsed=50)),
            current=snapshot_from_fixture(fixture),
            fixture=fixture,
            events=events,
            seen_event_keys=set(),
            emitted_checkpoints=set(),
        )
        text = "\n".join(update.text for update in updates)

        self.assertIn("Autogol", text)
        self.assertIn("Penal fallado", text)
        self.assertIn("Amarilla", text)
        self.assertIn("Cambio", text)
        self.assertIn("VAR", text)

    def test_checkpoint_and_momentum_thresholds(self) -> None:
        fixture = _fixture(home=0, away=1, elapsed=30)
        stats = [
            {
                "team": {"name": "Francia"},
                "statistics": [
                    {"type": "Total Shots", "value": 2},
                    {"type": "Shots on Goal", "value": 1},
                    {"type": "Corner Kicks", "value": 0},
                    {"type": "Ball Possession", "value": "42%"},
                ],
            },
            {
                "team": {"name": "Inglaterra"},
                "statistics": [
                    {"type": "Total Shots", "value": 7},
                    {"type": "Shots on Goal", "value": 4},
                    {"type": "Corner Kicks", "value": 3},
                    {"type": "Ball Possession", "value": "58%"},
                ],
            },
        ]
        current = snapshot_from_fixture(fixture, statistics=stats)

        self.assertTrue(should_fetch_statistics(current, emitted_checkpoints=set()))
        updates, _snapshot = build_watch_updates(
            previous=snapshot_from_fixture(_fixture(home=0, away=1, elapsed=20)),
            current=current,
            fixture=fixture,
            events=[],
            statistics=stats,
            seen_event_keys=set(),
            emitted_checkpoints=set(),
        )

        self.assertTrue(any(update.checkpoint == "30" for update in updates))
        self.assertIn("Inglaterra esta llegando mas", "\n".join(update.text for update in updates))

    def test_insignificant_stats_do_not_send_checkpoint(self) -> None:
        fixture = _fixture(home=0, away=0, elapsed=15)
        stats = [
            {"team": {"name": "A"}, "statistics": [{"type": "Total Shots", "value": 2}]},
            {"team": {"name": "B"}, "statistics": [{"type": "Total Shots", "value": 3}]},
        ]
        updates, _snapshot = build_watch_updates(
            previous=snapshot_from_fixture(_fixture(home=0, away=0, elapsed=10)),
            current=snapshot_from_fixture(fixture, statistics=stats),
            fixture=fixture,
            events=[],
            statistics=stats,
            seen_event_keys=set(),
            emitted_checkpoints=set(),
        )

        self.assertEqual(updates, [])

    def test_terminal_status_detection(self) -> None:
        self.assertTrue(is_terminal_status("FT"))
        self.assertTrue(is_terminal_status("AET"))
        self.assertFalse(is_terminal_status("2H 80'"))


if __name__ == "__main__":
    unittest.main()
