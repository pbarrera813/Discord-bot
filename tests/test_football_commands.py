from __future__ import annotations

from types import SimpleNamespace
import unittest

from cogs.football import FootballCog, LEAGUE_CODES, LEAGUE_HELP_TEXT


class FootballCommandTests(unittest.TestCase):
    def test_worldcup_league_aliases_normalize(self) -> None:
        cog = FootballCog(SimpleNamespace())

        self.assertEqual(cog._normalize_league_key("worldcup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("world cup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("fifa world cup"), "worldcup")

    def test_worldcup_is_in_league_choices_and_help(self) -> None:
        self.assertIn("worldcup", LEAGUE_CODES)
        self.assertIn("worldcup", LEAGUE_HELP_TEXT)

    def test_extract_table_rows_flattens_worldcup_groups(self) -> None:
        rows = [
            {
                "league": {
                    "standings": [
                        [
                            {"rank": 1, "team": {"id": 1, "name": "Mexico"}},
                            {"rank": 2, "team": {"id": 2, "name": "Canada"}},
                        ],
                        [
                            {"rank": 1, "team": {"id": 3, "name": "Argentina"}},
                        ],
                    ]
                }
            }
        ]

        flattened = FootballCog._extract_table_rows(rows)

        self.assertEqual(
            [item["team"]["name"] for item in flattened],
            ["Mexico", "Canada", "Argentina"],
        )


if __name__ == "__main__":
    unittest.main()
