from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest

from cogs.football import FootballCog, LEAGUE_CODES, LEAGUE_HELP_TEXT
from services.api_football import FootballApiError
from services import football_formatter, football_resolver


def _async_return(value):  # noqa: ANN001
    async def _inner(*_args, **_kwargs):  # noqa: ANN202
        return value

    return _inner


class _FakeCtx:
    def __init__(self, client: object) -> None:
        self.guild = SimpleNamespace(id=1)
        self.interaction = None
        self.sent: list[dict[str, object]] = []
        self.bot = SimpleNamespace(
            api_football_client=client,
            db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en"))),
        )

    async def send(self, content: str | None = None, **kwargs):  # noqa: ANN001, ANN202
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.sent))


class _FakeFootballClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, call_name: str, **kwargs):  # noqa: ANN202
        self.calls.append((call_name, dict(kwargs)))

    async def resolve_league_id(self, league_key):  # noqa: ANN001, ANN202
        self._record("resolve_league_id", league_key=league_key)
        return 262

    async def get_current_season(self, league_id):  # noqa: ANN001, ANN202
        self._record("get_current_season", league_id=league_id)
        return 2026

    async def get_live_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_live_fixtures", **kwargs)
        return [_fixture()]

    async def get_fixtures_on_date(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixtures_on_date", **kwargs)
        return [_fixture()]

    async def get_next_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_next_fixtures", **kwargs)
        return [_fixture()]

    async def get_last_fixtures(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_last_fixtures", **kwargs)
        return [_fixture()]

    async def get_standings(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_standings", **kwargs)
        return [{"league": {"standings": [[{"rank": 1, "points": 3, "goalsDiff": 1, "team": {"id": 1, "name": "Mexico"}, "all": {"played": 1}}]]}}]

    async def search_teams(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("search_teams", **kwargs)
        name = str(kwargs.get("name", "Team"))
        team_id = 1 if "switzerland" not in name.casefold() else 2
        return [{"team": {"id": team_id, "name": name.title()}}]

    async def get_top_scorers(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_scorers", **kwargs)
        return [_player()]

    async def get_top_assists(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_assists", **kwargs)
        return [_player()]

    async def get_top_yellow_cards(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_yellow_cards", **kwargs)
        return [_player()]

    async def get_top_red_cards(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_top_red_cards", **kwargs)
        return [_player()]

    async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("search_players", **kwargs)
        return [_player(name=str(kwargs.get("name", "Erling Haaland")))]

    async def get_fixture_by_id(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_by_id", **kwargs)
        return [_fixture()]

    async def get_fixture_events(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_events", **kwargs)
        return [{"time": {"elapsed": 10}, "team": {"name": "Mexico"}, "player": {"name": "A"}, "type": "Goal", "detail": "Normal Goal"}]

    async def get_fixture_statistics(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_statistics", **kwargs)
        return [{"team": {"name": "Mexico"}, "statistics": [{"type": "Shots", "value": 4}]}]

    async def get_fixture_lineups(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_fixture_lineups", **kwargs)
        return [{"team": {"name": "Mexico"}, "coach": {"name": "Coach"}, "formation": "4-3-3"}]

    async def get_injuries(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_injuries", **kwargs)
        return []

    async def get_transfers(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_transfers", **kwargs)
        return []

    async def get_head_to_head(self, **kwargs):  # noqa: ANN001, ANN202
        self._record("get_head_to_head", **kwargs)
        return [_fixture()]


def _fixture() -> dict[str, object]:
    return {
        "fixture": {"id": 10, "date": f"{date.today().isoformat()}T20:00:00+00:00", "status": {"short": "NS"}},
        "league": {"name": "Liga MX", "round": "Round 1"},
        "teams": {"home": {"name": "Mexico"}, "away": {"name": "France"}},
        "goals": {"home": None, "away": None},
    }


def _player(*, name: str = "Erling Haaland", player_id: int = 9) -> dict[str, object]:
    return {
        "player": {"id": player_id, "name": name},
        "statistics": [
            {
                "team": {"id": 5, "name": "Manchester City"},
                "games": {"position": "Attacker", "appearences": 12},
                "goals": {"total": 10, "assists": 2},
            }
        ],
    }


class FootballCommandTests(unittest.TestCase):
    def test_worldcup_league_aliases_normalize(self) -> None:
        cog = FootballCog(SimpleNamespace())

        self.assertEqual(cog._normalize_league_key("worldcup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("world cup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("fifa world cup"), "worldcup")
        self.assertEqual(cog._normalize_league_key("copa del mundo"), "worldcup")
        self.assertEqual(cog._normalize_league_key("copa mundial fifa"), "worldcup")

    def test_worldcup_is_in_league_choices_and_help(self) -> None:
        self.assertIn("worldcup", LEAGUE_CODES)
        self.assertIn("worldcup", LEAGUE_HELP_TEXT)

    def test_shared_resolver_aliases(self) -> None:
        self.assertEqual(football_resolver.normalize_league_key("Champions League"), "champions")
        self.assertEqual(football_resolver.canonical_team_query("barca"), "Barcelona")
        self.assertEqual(football_resolver.canonical_team_query("francia"), "France")
        self.assertEqual(football_resolver.canonical_player_query("mbappe"), "mbappe")
        self.assertEqual(football_resolver.canonical_player_query("dibu mtz"), "Emiliano Martinez")

    def test_player_query_parser_splits_stat_focus_from_name(self) -> None:
        parsed = football_resolver.parse_player_query("dibu mtz penales")

        self.assertEqual(parsed.stat_focus, "penalties")
        self.assertEqual(parsed.candidates[0], "Emiliano Martinez")
        self.assertNotIn("penales", " ".join(parsed.candidates).casefold())

    def test_player_query_parser_strips_age_goal_and_year_noise(self) -> None:
        parsed = football_resolver.parse_player_query("cuantos años tendra Diego Lainez en 2030")

        joined = " ".join(parsed.candidates).casefold()
        self.assertIn("diego lainez", joined)
        self.assertNotIn("2030", joined)
        self.assertNotIn("años", joined)

    def test_player_query_parser_removes_stat_words(self) -> None:
        parsed = football_resolver.parse_player_query("Dibu Martinez penalty stats performance")

        joined = " ".join(parsed.candidates).casefold()
        self.assertIn("emiliano martinez", joined)
        self.assertNotIn("penalty", joined)
        self.assertNotIn("stats", joined)
        self.assertNotIn("performance", joined)

    def test_pick_team_marks_ambiguity(self) -> None:
        rows = [
            {"team": {"id": 1, "name": "America"}},
            {"team": {"id": 2, "name": "America de Cali"}},
        ]
        result = football_resolver.pick_team(rows, "america")
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.selected)

    def test_shared_formatter_fixture_line(self) -> None:
        item = {
            "teams": {"home": {"name": "Mexico"}, "away": {"name": "France"}},
            "goals": {"home": 1, "away": 2},
            "fixture": {"status": {"short": "FT"}},
            "league": {"round": "Final"},
        }
        title, details = football_formatter.format_fixture_line(item)
        self.assertEqual(title, "Mexico vs France")
        self.assertIn("1 - 2", details)
        self.assertIn("Final", details)

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


class FootballCommandContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_football_commands_call_expected_api_wrappers(self) -> None:
        client = _FakeFootballClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog._run_football_live(ctx, "ligamx")
        await cog.football_today.callback(cog, ctx, "ligamx")
        await cog.football_next.callback(cog, ctx, "ligamx", target="2")
        await cog.football_last.callback(cog, ctx, "ligamx", team="Mexico")
        await cog.football_table.callback(cog, ctx, "ligamx")
        await cog.football_team.callback(cog, ctx, "ligamx", team="Mexico")
        await cog.football_scorers.callback(cog, ctx, "ligamx")
        await cog.football_match.callback(cog, ctx, "10", "ligamx")
        await cog.football_schedule.callback(cog, ctx, target="league", mode="next", league="ligamx")
        await cog.football_player.callback(cog, ctx, "haaland")
        await cog.football_lineup.callback(cog, ctx, 10)
        await cog.football_stats.callback(cog, ctx, 10)
        await cog.football_injuries.callback(cog, ctx, "real madrid")
        await cog.football_transfers.callback(cog, ctx, "america")
        await cog.football_h2h.callback(cog, ctx, "argentina", "switzerland")
        await cog.football_top.callback(cog, ctx, "assists", "ligamx")
        await cog.football_preview.callback(cog, ctx, 10)
        await cog.football_summary.callback(cog, ctx, 10)

        called = {name for name, _kwargs in client.calls}
        expected = {
            "get_live_fixtures",
            "get_fixtures_on_date",
            "get_next_fixtures",
            "get_last_fixtures",
            "get_standings",
            "search_teams",
            "get_top_scorers",
            "get_fixture_by_id",
            "get_fixture_events",
            "get_fixture_statistics",
            "search_players",
            "get_fixture_lineups",
            "get_injuries",
            "get_transfers",
            "get_head_to_head",
            "get_top_assists",
        }
        self.assertTrue(expected.issubset(called))

    async def test_player_command_uses_scoped_search_without_explicit_league(self) -> None:
        client = _FakeFootballClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_player.callback(cog, ctx, "haaland")

        player_calls = [kwargs for name, kwargs in client.calls if name == "search_players"]
        self.assertTrue(player_calls)
        self.assertEqual(player_calls[0]["name"], "haaland")
        self.assertIsNotNone(player_calls[0]["league_id"])
        self.assertIsNotNone(player_calls[0]["season"])

    async def test_player_resolver_does_not_send_invalid_unscoped_search(self) -> None:
        class _StrictClient(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs.get("league_id") is None and kwargs.get("team_id") is None:
                    raise FootballApiError("league or team required")
                return [_player(name=str(kwargs.get("name", "Erling Haaland")))]

        client = _StrictClient()
        lookup = await football_resolver.resolve_player(client, "haaland")

        self.assertIsNotNone(lookup.resolution.selected)
        player_calls = [kwargs for name, kwargs in client.calls if name == "search_players"]
        self.assertTrue(player_calls)
        self.assertTrue(all(call.get("league_id") is not None or call.get("team_id") is not None for call in player_calls))

    async def test_ordinary_player_search_succeeds_without_canonicalizer(self) -> None:
        client = _FakeFootballClient()
        calls = 0

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            return {"candidate_names": ["Should Not Be Used"], "confidence": 1.0}

        lookup = await football_resolver.resolve_player(client, "haaland", canonicalizer=canonicalizer)

        self.assertIsNotNone(lookup.resolution.selected)
        self.assertEqual(calls, 0)
        self.assertFalse(lookup.canonicalizer_used)

    async def test_canonicalizer_candidate_must_validate_through_api(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs["name"] == "Cristiano Ronaldo":
                    return [_player(name="Cristiano Ronaldo", player_id=7)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Cristiano Ronaldo"], "confidence": 0.91}

        lookup = await football_resolver.resolve_player(_Client(), "cr7", canonicalizer=canonicalizer)

        self.assertTrue(lookup.canonicalizer_used)
        self.assertEqual(lookup.resolution.selected["player"]["name"], "Cristiano Ronaldo")

    async def test_unvalidated_canonicalizer_candidate_is_rejected(self) -> None:
        class _EmptyClient(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Imaginary Player"], "confidence": 0.91}

        lookup = await football_resolver.resolve_player(_EmptyClient(), "unknown nickname", canonicalizer=canonicalizer)

        self.assertTrue(lookup.canonicalizer_used)
        self.assertIsNone(lookup.resolution.selected)

    async def test_la_tortuga_can_resolve_through_canonicalizer(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs["name"] == "Kylian Mbappe":
                    return [_player(name="Kylian Mbappe", player_id=10)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Kylian Mbappe"], "confidence": 0.86}

        lookup = await football_resolver.resolve_player(_Client(), "la tortuga", canonicalizer=canonicalizer)

        self.assertEqual(lookup.resolution.selected["player"]["name"], "Kylian Mbappe")

    async def test_canonicalizer_validation_can_remain_ambiguous(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs["name"] == "Ronaldo":
                    return [
                        _player(name="Cristiano Ronaldo", player_id=7),
                        _player(name="Ronaldo Nazario", player_id=9),
                    ]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Ronaldo"], "confidence": 0.8}

        lookup = await football_resolver.resolve_player(_Client(), "ronaldo", canonicalizer=canonicalizer)

        self.assertTrue(lookup.resolution.ambiguous)

    async def test_validated_alias_cache_is_reused_without_canonicalizer(self) -> None:
        class _Client(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                if kwargs["name"] == "Cristiano Ronaldo":
                    return [_player(name="Cristiano Ronaldo", player_id=7)]
                return []

        async def canonicalizer(_query):  # noqa: ANN001, ANN202
            return {"candidate_names": ["Cristiano Ronaldo"], "confidence": 0.91}

        cache: dict[str, dict[str, object]] = {}
        first_client = _Client()
        await football_resolver.resolve_player(first_client, "cr7", canonicalizer=canonicalizer, alias_cache=cache)
        second_client = _Client()
        await football_resolver.resolve_player(second_client, "cr7", alias_cache=cache)

        second_searches = [kwargs["name"] for name, kwargs in second_client.calls if name == "search_players"]
        self.assertEqual(second_searches[0], "Cristiano Ronaldo")

    async def test_player_command_disambiguates_multiple_matches(self) -> None:
        class _AmbiguousClient(_FakeFootballClient):
            async def search_players(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_players", **kwargs)
                return [
                    _player(name="Emiliano Martinez", player_id=1),
                    _player(name="Lautaro Martinez", player_id=2),
                ]

        client = _AmbiguousClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_player.callback(cog, ctx, "martinez")

        self.assertTrue(ctx.sent)
        self.assertIn("Multiple players matched", str(ctx.sent[-1]["content"]))

    async def test_global_team_fallback_avoids_default_league_false_not_found(self) -> None:
        class _FallbackTeamClient(_FakeFootballClient):
            async def search_teams(self, **kwargs):  # noqa: ANN001, ANN202
                self._record("search_teams", **kwargs)
                if kwargs.get("league_id") is not None:
                    return []
                return [{"team": {"id": 22, "name": str(kwargs.get("name", "Argentina")).title()}}]

        client = _FallbackTeamClient()
        cog = FootballCog(SimpleNamespace(api_football_client=client, db=SimpleNamespace(get_guild_settings=_async_return(SimpleNamespace(language_code="en")))))
        ctx = _FakeCtx(client)

        await cog.football_match.callback(cog, ctx, "argentina", "ligamx")

        self.assertIn(("get_next_fixtures", {"league_id": 262, "season": 2026, "next_count": 1, "team_id": 22}), client.calls)


if __name__ == "__main__":
    unittest.main()
