"""
test_sports_fetcher.py — Sports Fetcher 单元测试
"""

from unittest.mock import MagicMock, patch

import pytest
from src.utils.sports_fetcher import (
    _build_context,
    _find_h2h,
    _format_record,
    _resolve_team_name,
    _safe_int,
    extract_teams,
    fetch_sports_context,
)

# ===========================================================================
# extract_teams
# ===========================================================================


class TestExtractTeams:
    def test_standard_vs_dot(self):
        assert extract_teams("Trail Blazers vs. 76ers") == (
            "Trail Blazers",
            "76ers",
        )

    def test_standard_vs_no_dot(self):
        assert extract_teams("Lakers vs Celtics") == ("Lakers", "Celtics")

    def test_will_beat(self):
        assert extract_teams("Will the Bulls beat the Knicks?") == (
            "Bulls",
            "Knicks",
        )

    def test_will_defeat(self):
        assert extract_teams("Will the Hawks defeat the Heat?") == (
            "Hawks",
            "Heat",
        )

    def test_no_match(self):
        assert extract_teams("What will be the temperature in NYC?") is None

    def test_same_team(self):
        # Same team both sides → None
        assert extract_teams("Lakers vs. Lakers") is None

    def test_case_insensitive(self):
        result = extract_teams("lakers VS celtics")
        assert result == ("lakers", "celtics")

    def test_ou_suffix_stripped(self):
        """O/U 后缀不应污染 team2"""
        assert extract_teams("Senators vs. Rangers: O/U 5.5") == (
            "Senators",
            "Rangers",
        )

    def test_ou_suffix_nba(self):
        assert extract_teams("Thunder vs. 76ers: O/U 222.5") == (
            "Thunder",
            "76ers",
        )

    def test_over_under_suffix(self):
        assert extract_teams("Lakers vs Celtics: Over/Under 215.5") == (
            "Lakers",
            "Celtics",
        )


# ===========================================================================
# _resolve_team_name
# ===========================================================================


class TestResolveTeamName:
    def test_short_name_alias(self):
        assert _resolve_team_name("76ers") == "Philadelphia 76ers"

    def test_case_insensitive(self):
        assert _resolve_team_name("Trail Blazers") == "Portland Trail Blazers"

    def test_unknown_name_passthrough(self):
        assert _resolve_team_name("SomeUnknownTeam") == "SomeUnknownTeam"

    def test_nfl_alias(self):
        assert _resolve_team_name("niners") == "San Francisco 49ers"

    def test_nhl_alias(self):
        assert _resolve_team_name("habs") == "Montreal Canadiens"


# ===========================================================================
# _format_record
# ===========================================================================


class TestFormatRecord:
    def test_basic_record(self):
        events = [
            {
                "strHomeTeam": "Philadelphia 76ers",
                "strAwayTeam": "Brooklyn Nets",
                "intHomeScore": "104",
                "intAwayScore": "97",
            },
            {
                "strHomeTeam": "Boston Celtics",
                "strAwayTeam": "Philadelphia 76ers",
                "intHomeScore": "110",
                "intAwayScore": "105",
            },
        ]
        result = _format_record(events, "Philadelphia 76ers")
        assert "W" in result
        assert "L" in result
        assert "1W-1L" in result

    def test_empty_events(self):
        assert _format_record([], "Any Team") == "无数据"

    def test_none_scores(self):
        events = [
            {
                "strHomeTeam": "TeamA",
                "strAwayTeam": "TeamB",
                "intHomeScore": None,
                "intAwayScore": None,
            }
        ]
        assert _format_record(events, "TeamA") == "无数据"


# ===========================================================================
# _safe_int
# ===========================================================================


class TestSafeInt:
    def test_normal(self):
        assert _safe_int("104") == 104

    def test_none(self):
        assert _safe_int(None) is None

    def test_invalid(self):
        assert _safe_int("N/A") is None

    def test_int_input(self):
        assert _safe_int(42) == 42


# ===========================================================================
# _find_h2h
# ===========================================================================


class TestFindH2H:
    def test_finds_h2h(self):
        team1 = {
            "team_info": {"strTeam": "Philadelphia 76ers"},
            "last_events": [
                {
                    "strHomeTeam": "Philadelphia 76ers",
                    "strAwayTeam": "Portland Trail Blazers",
                    "intHomeScore": "110",
                    "intAwayScore": "95",
                    "dateEvent": "2026-03-10",
                },
            ],
        }
        team2 = {
            "team_info": {"strTeam": "Portland Trail Blazers"},
            "last_events": [],
        }
        result = _find_h2h(team1, team2)
        assert result is not None
        assert "110" in result

    def test_no_h2h(self):
        team1 = {
            "team_info": {"strTeam": "Philadelphia 76ers"},
            "last_events": [
                {
                    "strHomeTeam": "Philadelphia 76ers",
                    "strAwayTeam": "Brooklyn Nets",
                    "intHomeScore": "100",
                    "intAwayScore": "90",
                    "dateEvent": "2026-03-10",
                },
            ],
        }
        team2 = {
            "team_info": {"strTeam": "Portland Trail Blazers"},
            "last_events": [],
        }
        assert _find_h2h(team1, team2) is None

    def test_none_data(self):
        assert _find_h2h(None, None) is None


# ===========================================================================
# _build_context
# ===========================================================================


class TestBuildContext:
    def test_both_teams(self):
        d1 = {
            "team_info": {
                "strTeam": "Philadelphia 76ers",
                "strLeague": "NBA",
                "strSport": "Basketball",
                "strStadium": "Wells Fargo Center",
            },
            "last_events": [
                {
                    "strHomeTeam": "Philadelphia 76ers",
                    "strAwayTeam": "Nets",
                    "intHomeScore": "104",
                    "intAwayScore": "97",
                    "dateEvent": "2026-03-14",
                },
            ],
        }
        d2 = {
            "team_info": {
                "strTeam": "Portland Trail Blazers",
                "strLeague": "NBA",
                "strSport": "Basketball",
                "strStadium": "Moda Center",
            },
            "last_events": [
                {
                    "strHomeTeam": "Portland Trail Blazers",
                    "strAwayTeam": "Lakers",
                    "intHomeScore": "95",
                    "intAwayScore": "110",
                    "dateEvent": "2026-03-13",
                },
            ],
        }
        ctx = _build_context("76ers", d1, "Trail Blazers", d2)
        assert "Philadelphia 76ers" in ctx
        assert "Portland Trail Blazers" in ctx
        assert "NBA" in ctx
        assert "体育数据分析" in ctx

    def test_one_team_missing(self):
        d1 = {
            "team_info": {
                "strTeam": "76ers",
                "strLeague": "NBA",
                "strSport": "Basketball",
                "strStadium": "",
            },
            "last_events": [],
        }
        ctx = _build_context("76ers", d1, "Trail Blazers", None)
        assert "数据获取失败" in ctx


# ===========================================================================
# fetch_sports_context (integration with mocking)
# ===========================================================================


class TestFetchSportsContext:
    @patch("src.utils.sports_fetcher._search_team")
    @patch("src.utils.sports_fetcher._get_last_events")
    def test_full_flow(self, mock_events, mock_search):
        mock_search.return_value = {
            "idTeam": "134863",
            "strTeam": "Philadelphia 76ers",
            "strLeague": "NBA",
            "strSport": "Basketball",
            "strStadium": "Wells Fargo Center",
        }
        mock_events.return_value = [
            {
                "strHomeTeam": "Philadelphia 76ers",
                "strAwayTeam": "Brooklyn Nets",
                "intHomeScore": "104",
                "intAwayScore": "97",
                "dateEvent": "2026-03-14",
            }
        ]

        # Clear cache to ensure mocks are called
        from src.utils.sports_fetcher import _team_cache

        _team_cache.clear()

        ctx = fetch_sports_context("Trail Blazers vs. 76ers")
        assert "Philadelphia 76ers" in ctx
        assert "NBA" in ctx

    def test_no_teams_extracted(self):
        result = fetch_sports_context("What will the temperature be?")
        assert result == ""

    @patch("src.utils.sports_fetcher._search_team")
    def test_api_failure_returns_empty(self, mock_search):
        mock_search.return_value = None
        from src.utils.sports_fetcher import _team_cache

        _team_cache.clear()

        result = fetch_sports_context("Lakers vs Celtics")
        assert result == ""


# ===========================================================================
# RapidAPI Client
# ===========================================================================


class TestRapidAPIClient:
    """Tests for src.utils.rapidapi_client — 统一 RapidAPI 薄层封装"""

    def setup_method(self):
        import src.utils.rapidapi_client as rc

        rc.clear_cache()

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key_123"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_fetch_success(self, mock_get):
        """正常请求返回 JSON 数据"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "evt1"}]
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        from src.utils.rapidapi_client import fetch

        result = fetch("odds", "/v4/sports/basketball_nba/odds")
        assert result == [{"id": "evt1"}]
        # 验证 headers 包含 rapidapi-key
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["x-rapidapi-key"] == "test_key_123"
        assert headers["x-rapidapi-host"] == "odds-api1.p.rapidapi.com"

    @patch.dict("os.environ", {"RAPIDAPI_KEY": ""})
    def test_fetch_no_key_returns_none(self):
        """无 RAPIDAPI_KEY 时返回 None"""
        from src.utils.rapidapi_client import fetch

        result = fetch("odds", "/v4/sports/basketball_nba/odds")
        assert result is None

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_fetch_429_returns_none(self, mock_get):
        """429 限流时返回 None，不抛异常"""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        from src.utils.rapidapi_client import fetch

        result = fetch("odds", "/v4/sports/nfl/odds")
        assert result is None

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_fetch_403_returns_none(self, mock_get):
        """403 未授权时返回 None"""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp

        from src.utils.rapidapi_client import fetch

        result = fetch("football", "/v3/fixtures")
        assert result is None

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_fetch_cache_hit(self, mock_get):
        """第二次请求直接返回缓存，不再发 HTTP"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cached": True}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        from src.utils.rapidapi_client import fetch

        first = fetch("odds", "/v4/sports/nhl/odds", cache_ttl=3600)
        second = fetch("odds", "/v4/sports/nhl/odds", cache_ttl=3600)
        assert first == second
        assert mock_get.call_count == 1  # 只发了一次 HTTP 请求

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    def test_is_available_true(self):
        from src.utils.rapidapi_client import is_available

        assert is_available() is True

    @patch.dict("os.environ", {"RAPIDAPI_KEY": ""})
    def test_is_available_false(self):
        from src.utils.rapidapi_client import is_available

        assert is_available() is False

    def test_get_host_known(self):
        from src.utils.rapidapi_client import get_host

        assert get_host("odds") == "odds-api1.p.rapidapi.com"
        assert get_host("football") == "api-football-v1.p.rapidapi.com"
        assert get_host("nba") == "api-nba-v1.p.rapidapi.com"

    def test_get_host_unknown(self):
        from src.utils.rapidapi_client import get_host

        assert get_host("nonexistent_api") == ""

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_fetch_timeout_returns_none(self, mock_get):
        """请求超时时返回 None"""
        import requests as req_lib

        mock_get.side_effect = req_lib.exceptions.Timeout()

        from src.utils.rapidapi_client import fetch

        result = fetch("odds", "/v4/sports/mlb/odds", timeout=1)
        assert result is None


# ===========================================================================
# API-Football Injury Context
# ===========================================================================


class TestApiFootballInjuries:
    """Tests for fetch_api_football_injury_context()"""

    def setup_method(self):
        import src.utils.rapidapi_client as rc

        rc.clear_cache()

    @patch.dict("os.environ", {"RAPIDAPI_KEY": ""})
    def test_no_key_returns_empty(self):
        """无 RAPIDAPI_KEY 时返回空字符串"""
        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context(
            "Arsenal vs Chelsea in Premier League"
        )
        assert result == ""

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    def test_no_league_detected_returns_empty(self):
        """无法识别联赛时返回空字符串"""
        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context("Lakers vs Celtics")
        assert result == ""

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    def test_no_teams_extracted_returns_empty(self):
        """无法解析队名时返回空字符串"""
        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context(
            "What will happen in Premier League?"
        )
        assert result == ""

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_no_fixtures_returns_empty(self, mock_get):
        """API 返回空 fixtures 时返回空字符串"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": []}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context(
            "Arsenal vs Chelsea in Premier League"
        )
        assert result == ""

    @patch(
        "src.utils.config_loader.get",
        side_effect=lambda k: (
            {"api_football_enabled": True} if k == "sports_fetcher" else None
        ),
    )
    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_injury_context_populated(self, mock_get, mock_cfg):
        """正常返回伤病信息时上下文包含球队名和球员信息"""
        fixtures_resp = MagicMock()
        fixtures_resp.status_code = 200
        fixtures_resp.json.return_value = {
            "response": [
                {
                    "fixture": {"id": 9999},
                    "teams": {
                        "home": {"name": "Arsenal"},
                        "away": {"name": "Chelsea"},
                    },
                }
            ]
        }
        fixtures_resp.raise_for_status.return_value = None

        injury_resp = MagicMock()
        injury_resp.status_code = 200
        injury_resp.json.return_value = {
            "response": [
                {
                    "player": {
                        "name": "Bukayo Saka",
                        "type": "Injury",
                        "reason": "Hamstring",
                    },
                    "team": {"name": "Arsenal"},
                },
            ]
        }
        injury_resp.raise_for_status.return_value = None

        mock_get.side_effect = [fixtures_resp, injury_resp]

        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context(
            "Arsenal vs Chelsea in Premier League"
        )
        assert "Bukayo Saka" in result
        assert "Arsenal" in result
        assert "Hamstring" in result

    @patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"})
    @patch("src.utils.rapidapi_client.requests.get")
    def test_no_injuries_returns_empty(self, mock_get):
        """没有伤病球员时返回空字符串"""
        fixtures_resp = MagicMock()
        fixtures_resp.status_code = 200
        fixtures_resp.json.return_value = {
            "response": [
                {
                    "fixture": {"id": 8888},
                    "teams": {
                        "home": {"name": "Man City"},
                        "away": {"name": "Liverpool"},
                    },
                }
            ]
        }
        fixtures_resp.raise_for_status.return_value = None

        injury_resp = MagicMock()
        injury_resp.status_code = 200
        injury_resp.json.return_value = {"response": []}
        injury_resp.raise_for_status.return_value = None

        mock_get.side_effect = [fixtures_resp, injury_resp]

        from src.utils.sports_fetcher import fetch_api_football_injury_context

        result = fetch_api_football_injury_context(
            "Man City vs Liverpool in Premier League"
        )
        assert result == ""
