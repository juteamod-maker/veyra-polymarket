"""Tests for odds_fetcher.py — OddsPapi 体育赔率数据抓取"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")


class TestDetectTournament(unittest.TestCase):
    def setUp(self):
        import src.utils.odds_fetcher as mod

        self._orig = mod._metadata_enriched
        mod._metadata_enriched = True  # 跳过真实 API 元数据增强

    def tearDown(self):
        import src.utils.odds_fetcher as mod

        mod._metadata_enriched = self._orig

    def test_nba_keyword(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will the Lakers win the NBA Finals?")
        self.assertEqual(tid, 132)
        self.assertEqual(sport, "Basketball")

    def test_nba_team_name(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will the Celtics beat the Warriors?")
        self.assertEqual(tid, 132)
        self.assertEqual(sport, "Basketball")

    def test_nhl_keyword(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will the Bruins win the NHL playoffs?")
        self.assertEqual(tid, 234)
        self.assertEqual(sport, "Ice Hockey")

    def test_nhl_team(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, _ = _detect_tournament("Will the Penguins beat the Rangers?")
        self.assertEqual(tid, 234)

    def test_mlb_keyword(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will the Yankees win the MLB World Series?")
        self.assertEqual(tid, 109)
        self.assertEqual(sport, "Baseball")

    def test_nfl_keyword(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will the Chiefs win the NFL Super Bowl?")
        self.assertEqual(tid, 31)
        self.assertEqual(sport, "American Football")

    def test_soccer_epl(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will Arsenal win the EPL title?")
        self.assertEqual(tid, 17)
        self.assertEqual(sport, "Soccer")

    def test_soccer_premier_league(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, _ = _detect_tournament("Who wins the Premier League?")
        self.assertEqual(tid, 17)

    def test_bundesliga(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, _ = _detect_tournament("Will Bayern win the Bundesliga?")
        self.assertEqual(tid, 35)

    def test_unknown_sport(self):
        from src.utils.odds_fetcher import _detect_tournament

        tid, sport = _detect_tournament("Will Bitcoin reach $100k?")
        self.assertEqual(tid, 0)
        self.assertEqual(sport, "")


class TestExtractTeamKeywords(unittest.TestCase):
    def test_vs_match(self):
        from src.utils.odds_fetcher import _extract_team_keywords

        kws = _extract_team_keywords("Will the Lakers vs Celtics in the NBA Finals?")
        self.assertEqual(len(kws), 2)
        self.assertIn("Lakers", kws)
        self.assertIn("Celtics", kws)

    def test_beat_match(self):
        from src.utils.odds_fetcher import _extract_team_keywords

        kws = _extract_team_keywords("Will the Bucks beat the Heat?")
        self.assertEqual(len(kws), 2)

    def test_win_match(self):
        from src.utils.odds_fetcher import _extract_team_keywords

        kws = _extract_team_keywords("Will the Nuggets win?")
        self.assertGreaterEqual(len(kws), 1)


class TestMatchFixture(unittest.TestCase):
    def test_match_found(self):
        from src.utils.odds_fetcher import _match_fixture

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Boston Celtics",
                    "participant2Name": "Golden State Warriors",
                },
                "status": {"statusName": "Pre-Game"},
                "odds": {},
            },
            {
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Miami Heat",
                },
                "status": {"statusName": "Not Started"},
                "odds": {},
            },
        ]
        match = _match_fixture(fixtures, ["Lakers"])
        self.assertIsNotNone(match)
        self.assertEqual(
            match["participants"]["participant1Name"], "Los Angeles Lakers"
        )

    def test_no_match(self):
        from src.utils.odds_fetcher import _match_fixture

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Boston Celtics",
                    "participant2Name": "Golden State Warriors",
                },
                "status": {"statusName": "Pre-Game"},
                "odds": {},
            },
        ]
        match = _match_fixture(fixtures, ["Nuggets"])
        self.assertIsNone(match)

    def test_prefers_upcoming(self):
        from src.utils.odds_fetcher import _match_fixture

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Boston Celtics",
                },
                "status": {"statusName": "Finished"},
                "odds": {},
            },
            {
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Miami Heat",
                },
                "status": {"statusName": "Not Started"},
                "odds": {},
            },
        ]
        match = _match_fixture(fixtures, ["Lakers"])
        self.assertIsNotNone(match)
        self.assertEqual(match["participants"]["participant2Name"], "Miami Heat")


class TestExtractMoneyline(unittest.TestCase):
    def test_extract_from_bookmakers(self):
        from src.utils.odds_fetcher import _extract_moneyline

        fixture = {
            "odds": {
                "draftkings": {
                    "key1": {
                        "bookmaker": "draftkings",
                        "outcomeId": 131,
                        "marketId": 131,
                        "price": 1.95,
                        "active": True,
                    },
                    "key2": {
                        "bookmaker": "draftkings",
                        "outcomeId": 132,
                        "marketId": 131,
                        "price": 1.85,
                        "active": True,
                    },
                },
            },
        }
        ml = _extract_moneyline(fixture)
        self.assertEqual(len(ml["participant1"]), 1)
        self.assertEqual(len(ml["participant2"]), 1)
        self.assertEqual(ml["participant1"][0][0], "draftkings")
        self.assertAlmostEqual(ml["participant1"][0][1], 1.95)
        self.assertAlmostEqual(ml["participant2"][0][1], 1.85)

    def test_empty_odds(self):
        from src.utils.odds_fetcher import _extract_moneyline

        fixture = {"odds": {}}
        ml = _extract_moneyline(fixture)
        self.assertEqual(ml["participant1"], [])
        self.assertEqual(ml["participant2"], [])

    def test_skips_inactive(self):
        from src.utils.odds_fetcher import _extract_moneyline

        fixture = {
            "odds": {
                "betmgm": {
                    "key1": {
                        "outcomeId": 131,
                        "marketId": 131,
                        "price": 1.95,
                        "active": False,
                    },
                    "key2": {
                        "outcomeId": 132,
                        "marketId": 131,
                        "price": 1.85,
                        "active": True,
                    },
                },
            },
        }
        ml = _extract_moneyline(fixture)
        # Only 1 active outcome → not a valid moneyline pair
        self.assertEqual(ml["participant1"], [])

    def test_preferred_bookmaker_first(self):
        from src.utils.odds_fetcher import _extract_moneyline

        fixture = {
            "odds": {
                "zzz_unknown_bm": {
                    "k1": {
                        "outcomeId": 100,
                        "marketId": 100,
                        "price": 2.0,
                        "active": True,
                    },
                    "k2": {
                        "outcomeId": 101,
                        "marketId": 100,
                        "price": 1.8,
                        "active": True,
                    },
                },
                "draftkings": {
                    "k1": {
                        "outcomeId": 131,
                        "marketId": 131,
                        "price": 1.95,
                        "active": True,
                    },
                    "k2": {
                        "outcomeId": 132,
                        "marketId": 131,
                        "price": 1.85,
                        "active": True,
                    },
                },
            },
        }
        ml = _extract_moneyline(fixture)
        # draftkings should appear first (preferred)
        self.assertEqual(ml["participant1"][0][0], "draftkings")


class TestFetchOddsContext(unittest.TestCase):
    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key(self):
        from src.utils.odds_fetcher import fetch_odds_context

        ctx = fetch_odds_context("Will the Lakers beat the Celtics?")
        self.assertEqual(ctx, "")

    @patch("src.utils.odds_fetcher._get_fixtures_odds")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key_123"}, clear=False)
    def test_successful_fetch(self, mock_get_fixtures):
        from src.utils.odds_fetcher import fetch_odds_context

        mock_get_fixtures.return_value = [
            {
                "fixtureId": "test123",
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Boston Celtics",
                },
                "status": {"statusName": "Pre-Game"},
                "startTime": 1774213200,
                "scores": {},
                "odds": {
                    "draftkings": {
                        "key1": {
                            "outcomeId": 131,
                            "marketId": 131,
                            "price": 2.10,
                            "active": True,
                        },
                        "key2": {
                            "outcomeId": 132,
                            "marketId": 131,
                            "price": 1.75,
                            "active": True,
                        },
                    }
                },
            }
        ]

        ctx = fetch_odds_context("Will the Lakers beat the Celtics?")
        self.assertIn("OddsPapi", ctx)
        self.assertIn("Lakers", ctx)
        self.assertIn("draftkings", ctx)
        self.assertIn("Implied probability", ctx)

    @patch("src.utils.odds_fetcher._get_fixtures_odds")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key_123"}, clear=False)
    def test_no_match(self, mock_get_fixtures):
        from src.utils.odds_fetcher import fetch_odds_context

        mock_get_fixtures.return_value = [
            {
                "participants": {
                    "participant1Name": "Team A",
                    "participant2Name": "Team B",
                },
                "status": {"statusName": "Pre-Game"},
                "odds": {},
            }
        ]

        ctx = fetch_odds_context("Will the Warriors win tonight?")
        self.assertEqual(ctx, "")


class TestBuildContext(unittest.TestCase):
    def test_build_with_odds(self):
        from src.utils.odds_fetcher import _build_context

        fixture = {
            "participants": {
                "participant1Name": "Team A",
                "participant2Name": "Team B",
            },
            "status": {"statusName": "Pre-Game"},
            "startTime": 1774213200,
            "scores": {},
        }
        moneyline = {
            "participant1": [("draftkings", 2.0, 0.5)],
            "participant2": [("draftkings", 1.8, 0.5556)],
        }
        ctx = _build_context(fixture, moneyline)
        self.assertIn("Team A vs Team B", ctx)
        self.assertIn("Implied probability (normalized)", ctx)
        self.assertIn("draftkings", ctx)

    def test_build_no_odds(self):
        from src.utils.odds_fetcher import _build_context

        fixture = {
            "participants": {
                "participant1Name": "Team A",
                "participant2Name": "Team B",
            },
            "status": {"statusName": "Pre-Game"},
            "scores": {},
        }
        moneyline = {"participant1": [], "participant2": []}
        ctx = _build_context(fixture, moneyline)
        self.assertIn("No moneyline odds available", ctx)

    def test_build_with_scores(self):
        from src.utils.odds_fetcher import _build_context

        fixture = {
            "participants": {
                "participant1Name": "Team A",
                "participant2Name": "Team B",
            },
            "status": {"statusName": "Finished"},
            "startTime": 1774213200,
            "scores": {
                "fulltime": {
                    "participant1Score": 110,
                    "participant2Score": 98,
                }
            },
        }
        moneyline = {"participant1": [], "participant2": []}
        ctx = _build_context(fixture, moneyline)
        self.assertIn("110", ctx)
        self.assertIn("98", ctx)


class TestDailyLimit(unittest.TestCase):
    def test_check_and_increment(self):
        from src.utils.odds_fetcher import (
            _check_daily_limit,
            _daily_calls,
            _increment_daily_calls,
        )

        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}
        self.assertTrue(_check_daily_limit())
        _increment_daily_calls("/fixtures/odds/main")
        self.assertEqual(_daily_calls["total"], 1)
        self.assertEqual(_daily_calls["by_endpoint"]["/fixtures/odds/main"], 1)

    @patch("src.utils.odds_fetcher._get_max_daily_calls", return_value=100)
    def test_limit_exceeded(self, mock_max):
        from datetime import datetime, timezone

        from src.utils.odds_fetcher import _check_daily_limit, _daily_calls

        _daily_calls["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _daily_calls["total"] = 100
        _daily_calls["by_endpoint"] = {}
        self.assertFalse(_check_daily_limit())


class TestBuildScoresContext(unittest.TestCase):
    def test_scores_with_finished_games(self):
        from src.utils.odds_fetcher import _build_scores_context

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Boston Celtics",
                },
                "status": {"statusName": "Finished"},
                "startTime": 1774213200,
                "scores": {
                    "fulltime": {
                        "participant1Score": 115,
                        "participant2Score": 108,
                    }
                },
            },
        ]
        ctx = _build_scores_context(fixtures, ["Lakers"])
        self.assertIn("115-108", ctx)
        self.assertIn("FINAL", ctx)
        self.assertIn("1W-0L", ctx)

    def test_no_finished_games(self):
        from src.utils.odds_fetcher import _build_scores_context

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Team A",
                    "participant2Name": "Team B",
                },
                "status": {"statusName": "Not Started"},
                "scores": {},
            },
        ]
        ctx = _build_scores_context(fixtures, ["Team A"])
        self.assertEqual(ctx, "")


class TestFilterTeamFixtures(unittest.TestCase):
    def test_filters_by_team(self):
        from src.utils.odds_fetcher import _filter_team_fixtures

        fixtures = [
            {
                "participants": {
                    "participant1Name": "Los Angeles Lakers",
                    "participant2Name": "Boston Celtics",
                },
            },
            {
                "participants": {
                    "participant1Name": "Phoenix Suns",
                    "participant2Name": "Miami Heat",
                },
            },
        ]
        result = _filter_team_fixtures(fixtures, ["Lakers"])
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# 新端点函数测试 (PRO 极限压榨)
# ---------------------------------------------------------------------------


class TestFetchTodayFixtures(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls, _today_cache

        _today_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_today_fixtures

        mock_fetch.return_value = [
            {"fixtureId": "f1", "participants": {}},
            {"fixtureId": "f2", "participants": {}},
        ]
        result = fetch_today_fixtures(sport_id=11)
        self.assertEqual(len(result), 2)
        mock_fetch.assert_called_once()
        args = mock_fetch.call_args
        self.assertIn("/fixtures/today", args[0])

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_cache_hit(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_today_fixtures

        mock_fetch.return_value = [{"fixtureId": "f1"}]
        fetch_today_fixtures(sport_id=11)
        fetch_today_fixtures(sport_id=11)
        mock_fetch.assert_called_once()

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key(self):
        from src.utils.odds_fetcher import fetch_today_fixtures

        result = fetch_today_fixtures(sport_id=10)
        self.assertEqual(result, [])

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_all_sports(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_today_fixtures

        mock_fetch.return_value = [{"fixtureId": "f1"}]
        fetch_today_fixtures(sport_id=0)
        args = mock_fetch.call_args
        params = args[1].get("params") or (args[0][2] if len(args[0]) > 2 else {})
        # sport_id=0 should not pass sportId param
        if isinstance(params, dict):
            self.assertNotIn("sportId", params)


class TestFetchLiveFixtures(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls, _live_cache

        _live_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_live_fixtures

        mock_fetch.return_value = [{"fixtureId": "live1", "status": "live"}]
        result = fetch_live_fixtures(sport_id=10)
        self.assertEqual(len(result), 1)

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_cache_hit(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_live_fixtures

        mock_fetch.return_value = [{"fixtureId": "live1"}]
        fetch_live_fixtures(sport_id=10)
        fetch_live_fixtures(sport_id=10)
        mock_fetch.assert_called_once()


class TestFetchFixtureCLV(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _clv_cache, _daily_calls

        _clv_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    def test_empty_fixture_id(self):
        from src.utils.odds_fetcher import fetch_fixture_clv

        self.assertIsNone(fetch_fixture_clv(""))

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_fixture_clv

        mock_fetch.return_value = {
            "fixtureId": "id1100013262924543",
            "odds": {
                "pinnacle": {
                    "key1": {
                        "olv": {"price": 1.8, "outcomeId": 131},
                        "clv": {"price": 1.75, "outcomeId": 131},
                    }
                },
            },
        }
        result = fetch_fixture_clv("id1100013262924543")
        self.assertIsNotNone(result)
        self.assertIn("odds", result)
        self.assertIn("pinnacle", result["odds"])

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_cache_hit(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_fixture_clv

        mock_fetch.return_value = {"fixtureId": "f1", "odds": {"bk": {}}}
        fetch_fixture_clv("f1")
        fetch_fixture_clv("f1")
        mock_fetch.assert_called_once()


class TestFetchFixtureSettlement(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls, _settlement_cache

        _settlement_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    def test_empty_fixture_id(self):
        from src.utils.odds_fetcher import fetch_fixture_settlement

        self.assertIsNone(fetch_fixture_settlement(""))

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_fixture_settlement

        mock_fetch.return_value = {
            "fixtureId": "id1100013262924543",
            "settlements": [
                {
                    "marketId": 131,
                    "marketType": "Moneyline",
                    "outcomeId": 131,
                    "status": "won",
                    "margin": 0,
                }
            ],
        }
        result = fetch_fixture_settlement("id1100013262924543")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["settlements"]), 1)
        self.assertEqual(result["settlements"][0]["status"], "won")


class TestFetchSportsMetadata(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls, _metadata_cache

        _metadata_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_sports_metadata

        mock_fetch.return_value = [
            {"sportId": 10, "slug": "soccer", "sportName": "Soccer"},
            {"sportId": 11, "slug": "basketball", "sportName": "Basketball"},
        ]
        result = fetch_sports_metadata()
        self.assertEqual(len(result), 2)

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_cache_hit(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_sports_metadata

        mock_fetch.return_value = [{"sportId": 10}]
        fetch_sports_metadata()
        fetch_sports_metadata()
        mock_fetch.assert_called_once()


class TestFetchTournaments(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls, _metadata_cache

        _metadata_cache.clear()
        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_tournaments

        mock_fetch.return_value = [
            {"tournamentId": 132, "tournamentName": "NBA", "sportId": 11},
            {"tournamentId": 109, "tournamentName": "MLB", "sportId": 11},
        ]
        result = fetch_tournaments(11)
        self.assertEqual(len(result), 2)

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_cache_per_sport(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_tournaments

        mock_fetch.return_value = [{"tournamentId": 132}]
        fetch_tournaments(11)
        fetch_tournaments(10)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_same_sport_cached(self, mock_fetch):
        from src.utils.odds_fetcher import fetch_tournaments

        mock_fetch.return_value = [{"tournamentId": 132}]
        fetch_tournaments(11)
        fetch_tournaments(11)
        mock_fetch.assert_called_once()


class TestGetApiUsageStats(unittest.TestCase):
    def test_no_calls_today(self):
        from src.utils.odds_fetcher import _daily_calls, get_api_usage_stats

        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}
        stats = get_api_usage_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertGreater(stats["max_daily"], 0)
        self.assertEqual(stats["remaining"], stats["max_daily"])

    def test_with_calls(self):
        from datetime import datetime, timezone

        from src.utils.odds_fetcher import _daily_calls, get_api_usage_stats

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _daily_calls["date"] = today
        _daily_calls["total"] = 42
        _daily_calls["by_endpoint"] = {"/fixtures/odds/main": 30, "/fixtures/today": 12}
        stats = get_api_usage_stats()
        self.assertEqual(stats["total_calls"], 42)
        self.assertEqual(stats["by_endpoint"]["/fixtures/odds/main"], 30)
        self.assertEqual(stats["remaining"], stats["max_daily"] - 42)


class TestAnalyzeClvEdge(unittest.TestCase):
    def test_none_input(self):
        from src.utils.odds_fetcher import analyze_clv_edge

        self.assertIsNone(analyze_clv_edge(None))

    def test_empty_odds(self):
        from src.utils.odds_fetcher import analyze_clv_edge

        self.assertIsNone(analyze_clv_edge({"fixtureId": "f1", "odds": {}}))

    def test_valid_clv_data(self):
        from src.utils.odds_fetcher import analyze_clv_edge

        data = {
            "fixtureId": "id1100013262924543",
            "odds": {
                "pinnacle": {
                    "key1": {
                        "olv": {"price": 2.0, "outcomeId": 131},
                        "clv": {"price": 1.8, "outcomeId": 131},
                    },
                    "key2": {
                        "olv": {"price": 1.9, "outcomeId": 132},
                        "clv": {"price": 2.1, "outcomeId": 132},
                    },
                }
            },
        }
        result = analyze_clv_edge(data)
        self.assertIsNotNone(result)
        self.assertEqual(result["bookmaker"], "pinnacle")
        self.assertEqual(len(result["outcomes"]), 2)
        # First outcome: (1.8 - 2.0) / 2.0 * 100 = -10%
        self.assertAlmostEqual(result["outcomes"][0]["line_moved_pct"], -10.0)


class TestClearAllCaches(unittest.TestCase):
    def test_clears(self):
        from src.utils.odds_fetcher import (
            _cache,
            _clv_cache,
            _live_cache,
            _metadata_cache,
            _settlement_cache,
            _today_cache,
            clear_all_caches,
        )

        _cache["test"] = (0, [])
        _today_cache[0] = (0, [])
        _live_cache[0] = (0, [])
        _clv_cache["f1"] = (0, {})
        _settlement_cache["f1"] = (0, {})
        _metadata_cache["sports"] = (0, [])

        clear_all_caches()

        self.assertEqual(len(_cache), 0)
        self.assertEqual(len(_today_cache), 0)
        self.assertEqual(len(_live_cache), 0)
        self.assertEqual(len(_clv_cache), 0)
        self.assertEqual(len(_settlement_cache), 0)
        self.assertEqual(len(_metadata_cache), 0)


class TestRapidapiCall(unittest.TestCase):
    def setUp(self):
        from src.utils.odds_fetcher import _daily_calls

        _daily_calls["date"] = None
        _daily_calls["total"] = 0
        _daily_calls["by_endpoint"] = {}

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key(self):
        from src.utils.odds_fetcher import _rapidapi_call

        self.assertIsNone(_rapidapi_call("/sports"))

    @patch("src.utils.odds_fetcher._get_max_daily_calls", return_value=0)
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_daily_limit_reached(self, mock_max):
        from datetime import datetime, timezone

        from src.utils.odds_fetcher import _daily_calls, _rapidapi_call

        _daily_calls["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _daily_calls["total"] = 0
        self.assertIsNone(_rapidapi_call("/sports"))

    @patch("src.utils.rapidapi_client.fetch")
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_success_increments_calls(self, mock_fetch):
        from src.utils.odds_fetcher import _daily_calls, _rapidapi_call

        mock_fetch.return_value = [{"sportId": 10}]
        result = _rapidapi_call("/sports")
        self.assertIsNotNone(result)
        self.assertEqual(_daily_calls["total"], 1)
        self.assertEqual(_daily_calls["by_endpoint"].get("/sports", 0), 1)

    @patch("src.utils.rapidapi_client.fetch", side_effect=Exception("timeout"))
    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    def test_exception_returns_none(self, mock_fetch):
        from src.utils.odds_fetcher import _rapidapi_call

        self.assertIsNone(_rapidapi_call("/sports"))


# ---------------------------------------------------------------------------
# Integration 函数测试（TASK 163）
# ---------------------------------------------------------------------------


class TestEnrichTournamentInfo(unittest.TestCase):
    """_enrich_tournament_info() 懒加载元数据增强"""

    def setUp(self):
        import src.utils.odds_fetcher as mod

        self.mod = mod
        self._orig_enriched = mod._metadata_enriched
        mod._metadata_enriched = False

    def tearDown(self):
        self.mod._metadata_enriched = self._orig_enriched

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch("src.utils.odds_fetcher.fetch_tournaments")
    @patch("src.utils.odds_fetcher.config_get", return_value=[10, 11])
    def test_adds_new_tournaments(self, mock_cfg, mock_fetch):
        mock_fetch.return_value = [
            {
                "tournamentName": "test_league_xyz",
                "tournamentId": 99999,
                "sportName": "Soccer",
            }
        ]
        before = len(self.mod._TOURNAMENT_INFO)
        self.mod._enrich_tournament_info()
        self.assertIn("test_league_xyz", self.mod._TOURNAMENT_INFO)
        self.assertEqual(
            self.mod._TOURNAMENT_INFO["test_league_xyz"], (99999, "Soccer")
        )
        self.assertTrue(self.mod._metadata_enriched)
        # cleanup
        del self.mod._TOURNAMENT_INFO["test_league_xyz"]

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch("src.utils.odds_fetcher.fetch_tournaments")
    @patch("src.utils.odds_fetcher.config_get", return_value=[10])
    def test_does_not_overwrite_existing(self, mock_cfg, mock_fetch):
        # "nba" 已在 _TOURNAMENT_INFO 里，不应被覆盖
        mock_fetch.return_value = [
            {"tournamentName": "nba", "tournamentId": 77777, "sportName": "Basketball"}
        ]
        orig = self.mod._TOURNAMENT_INFO.get("nba")
        self.mod._enrich_tournament_info()
        self.assertEqual(self.mod._TOURNAMENT_INFO.get("nba"), orig)

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key_skips(self):
        self.mod._enrich_tournament_info()
        self.assertTrue(self.mod._metadata_enriched)

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher.fetch_tournaments", side_effect=Exception("API down")
    )
    @patch("src.utils.odds_fetcher.config_get", return_value=[10])
    def test_exception_nonfatal(self, mock_cfg, mock_fetch):
        # 不应抛出异常
        self.mod._enrich_tournament_info()
        self.assertTrue(self.mod._metadata_enriched)

    def test_idempotent_second_call(self):
        self.mod._metadata_enriched = True
        with patch("src.utils.odds_fetcher.fetch_tournaments") as mock_fetch:
            self.mod._enrich_tournament_info()
            mock_fetch.assert_not_called()


class TestTournamentToSportId(unittest.TestCase):
    """_tournament_to_sport_id() 锦标赛→sportId 反查"""

    def test_hardcoded_nba(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(132), 11)

    def test_hardcoded_nhl(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(234), 15)

    def test_hardcoded_nfl(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(31), 14)

    def test_hardcoded_mlb(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(109), 16)

    def test_hardcoded_epl(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(17), 10)

    def test_unknown_returns_zero(self):
        from src.utils.odds_fetcher import _tournament_to_sport_id

        self.assertEqual(_tournament_to_sport_id(999999), 0)

    def test_dynamic_fallback(self):
        """从 _TOURNAMENT_INFO 动态条目中反查"""
        import src.utils.odds_fetcher as mod

        # 临时注入 dynamic mapping
        mod._TOURNAMENT_INFO["test_dynamic_league"] = (88888, "Ice Hockey")
        try:
            self.assertEqual(mod._tournament_to_sport_id(88888), 15)
        finally:
            del mod._TOURNAMENT_INFO["test_dynamic_league"]


class TestFetchTodayContext(unittest.TestCase):
    """fetch_today_context() 今日赛程上下文"""

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=11)
    @patch("src.utils.odds_fetcher.fetch_today_fixtures")
    def test_formats_schedule(self, mock_today, mock_sid, mock_detect):
        mock_today.return_value = [
            {
                "participants": {
                    "participant1Name": "Lakers",
                    "participant2Name": "Celtics",
                },
                "status": {"statusName": "Not Started"},
                "startTime": 1750000000,
            },
            {
                "participants": {
                    "participant1Name": "Warriors",
                    "participant2Name": "Bucks",
                },
                "status": {"statusName": "Live"},
                "startTime": 1749990000,
            },
        ]
        from src.utils.odds_fetcher import fetch_today_context

        result = fetch_today_context("Will the Lakers win NBA game?")
        self.assertIn("Today's Basketball Schedule (2 games)", result)
        self.assertIn("Lakers vs Celtics", result)
        self.assertIn("Warriors vs Bucks", result)
        self.assertIn("[Live]", result)

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=11)
    @patch("src.utils.odds_fetcher.fetch_today_fixtures", return_value=[])
    def test_empty_fixtures_returns_empty(self, mock_today, mock_sid, mock_detect):
        from src.utils.odds_fetcher import fetch_today_context

        self.assertEqual(fetch_today_context("NBA game?"), "")

    def test_empty_question_returns_empty(self):
        from src.utils.odds_fetcher import fetch_today_context

        self.assertEqual(fetch_today_context(""), "")

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key_returns_empty(self):
        from src.utils.odds_fetcher import fetch_today_context

        self.assertEqual(fetch_today_context("NBA game?"), "")

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch("src.utils.odds_fetcher._detect_tournament", return_value=(0, ""))
    def test_no_tournament_returns_empty(self, mock_detect):
        from src.utils.odds_fetcher import fetch_today_context

        self.assertEqual(fetch_today_context("unknown sport question?"), "")

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=0)
    def test_no_sport_id_returns_empty(self, mock_sid, mock_detect):
        from src.utils.odds_fetcher import fetch_today_context

        self.assertEqual(fetch_today_context("NBA game?"), "")


class TestCheckFixtureLive(unittest.TestCase):
    """check_fixture_live() 实时比赛状态检查"""

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=11)
    @patch(
        "src.utils.odds_fetcher._extract_team_keywords",
        return_value=["lakers", "celtics"],
    )
    @patch("src.utils.odds_fetcher._match_fixture")
    @patch("src.utils.odds_fetcher.fetch_live_fixtures")
    def test_live_match_found(
        self, mock_live, mock_match, mock_kw, mock_sid, mock_detect
    ):
        mock_live.return_value = [{"fixtureId": "f1"}]
        mock_match.return_value = {"fixtureId": "f1"}
        from src.utils.odds_fetcher import check_fixture_live

        self.assertTrue(check_fixture_live("Will the Lakers beat the Celtics NBA?"))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=11)
    @patch(
        "src.utils.odds_fetcher._extract_team_keywords",
        return_value=["lakers", "celtics"],
    )
    @patch("src.utils.odds_fetcher._match_fixture", return_value=None)
    @patch("src.utils.odds_fetcher.fetch_live_fixtures")
    def test_no_live_match(self, mock_live, mock_match, mock_kw, mock_sid, mock_detect):
        mock_live.return_value = [{"fixtureId": "f2"}]
        from src.utils.odds_fetcher import check_fixture_live

        self.assertFalse(check_fixture_live("Will the Lakers beat the Celtics NBA?"))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._tournament_to_sport_id", return_value=11)
    @patch("src.utils.odds_fetcher.fetch_live_fixtures", return_value=[])
    def test_no_live_fixtures(self, mock_live, mock_sid, mock_detect):
        from src.utils.odds_fetcher import check_fixture_live

        self.assertFalse(check_fixture_live("Will the Lakers beat Celtics?"))

    def test_empty_question(self):
        from src.utils.odds_fetcher import check_fixture_live

        self.assertFalse(check_fixture_live(""))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key(self):
        from src.utils.odds_fetcher import check_fixture_live

        self.assertFalse(check_fixture_live("NBA Lakers game?"))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch("src.utils.odds_fetcher._detect_tournament", return_value=(0, ""))
    def test_unknown_tournament(self, mock_detect):
        from src.utils.odds_fetcher import check_fixture_live

        self.assertFalse(check_fixture_live("some unknown sport?"))


class TestFetchSettlementVerification(unittest.TestCase):
    """fetch_settlement_verification() 独立结算验证 + CLV"""

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._get_fixtures_odds")
    @patch(
        "src.utils.odds_fetcher._extract_team_keywords",
        return_value=["lakers", "celtics"],
    )
    @patch("src.utils.odds_fetcher._match_fixture")
    @patch("src.utils.odds_fetcher.analyze_clv_edge")
    @patch("src.utils.odds_fetcher.fetch_fixture_clv")
    @patch("src.utils.odds_fetcher.fetch_fixture_settlement")
    def test_full_result(
        self,
        mock_settle,
        mock_clv,
        mock_analyze,
        mock_match,
        mock_kw,
        mock_fix,
        mock_detect,
    ):
        mock_fix.return_value = [{"fixtureId": "f100"}]
        mock_match.return_value = {"fixtureId": "f100"}
        mock_settle.return_value = {"fixtureId": "f100", "bets": []}
        mock_clv.return_value = {"fixtureId": "f100", "odds": {}}
        mock_analyze.return_value = {"bookmaker": "pinnacle", "outcomes": []}

        from src.utils.odds_fetcher import fetch_settlement_verification

        result = fetch_settlement_verification("Will the Lakers beat the Celtics NBA?")
        self.assertIsNotNone(result)
        self.assertEqual(result["fixture_id"], "f100")
        self.assertIsNotNone(result["settlement"])
        self.assertIsNotNone(result["clv_edge"])

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._get_fixtures_odds")
    @patch(
        "src.utils.odds_fetcher._extract_team_keywords",
        return_value=["lakers", "celtics"],
    )
    @patch("src.utils.odds_fetcher._match_fixture")
    @patch("src.utils.odds_fetcher.fetch_fixture_clv", return_value=None)
    @patch("src.utils.odds_fetcher.fetch_fixture_settlement", return_value=None)
    def test_no_data_returns_nones(
        self, mock_settle, mock_clv, mock_match, mock_kw, mock_fix, mock_detect
    ):
        mock_fix.return_value = [{"fixtureId": "f200"}]
        mock_match.return_value = {"fixtureId": "f200"}

        from src.utils.odds_fetcher import fetch_settlement_verification

        result = fetch_settlement_verification("Will the Lakers beat the Celtics NBA?")
        self.assertIsNotNone(result)
        self.assertEqual(result["fixture_id"], "f200")
        self.assertIsNone(result["settlement"])
        self.assertIsNone(result["clv_edge"])

    def test_empty_question(self):
        from src.utils.odds_fetcher import fetch_settlement_verification

        self.assertIsNone(fetch_settlement_verification(""))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": ""}, clear=False)
    def test_no_api_key(self):
        from src.utils.odds_fetcher import fetch_settlement_verification

        self.assertIsNone(fetch_settlement_verification("NBA game?"))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._get_fixtures_odds", return_value=[])
    def test_no_fixtures(self, mock_fix, mock_detect):
        from src.utils.odds_fetcher import fetch_settlement_verification

        self.assertIsNone(fetch_settlement_verification("NBA Lakers?"))

    @patch.dict(os.environ, {"RAPIDAPI_KEY": "test_key"}, clear=False)
    @patch(
        "src.utils.odds_fetcher._detect_tournament", return_value=(132, "Basketball")
    )
    @patch("src.utils.odds_fetcher._get_fixtures_odds")
    @patch("src.utils.odds_fetcher._extract_team_keywords", return_value=["lakers"])
    @patch("src.utils.odds_fetcher._match_fixture", return_value=None)
    def test_no_match(self, mock_match, mock_kw, mock_fix, mock_detect):
        mock_fix.return_value = [{"fixtureId": "f300"}]

        from src.utils.odds_fetcher import fetch_settlement_verification

        self.assertIsNone(fetch_settlement_verification("Lakers NBA?"))


if __name__ == "__main__":
    unittest.main()
