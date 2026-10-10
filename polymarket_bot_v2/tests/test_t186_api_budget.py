"""
test_t186_api_budget.py — T186 OddsPapi/API-Football 调用量优化测试

验证:
  1. sports_fetcher api-football 每日调用计数器
  2. signal_engine 每轮体育付费 API 预算控制
  3. category_min_markets.sports 配额降低后的候选数量
"""

from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# sports_fetcher: api-football 每日调用限额
# ===========================================================================


class TestApiFootballDailyLimit:
    """测试 api-football 每日调用计数器功能。"""

    def setup_method(self):
        from src.utils import sports_fetcher

        sports_fetcher._api_football_daily["date"] = ""
        sports_fetcher._api_football_daily["total"] = 0

    def test_check_limit_returns_true_when_under(self):
        from src.utils.sports_fetcher import _check_api_football_daily_limit

        with patch(
            "src.utils.config_loader.get",
            return_value={"api_football_max_daily_calls": 100},
        ):
            assert _check_api_football_daily_limit() is True

    def test_check_limit_returns_false_when_over(self):
        from src.utils import sports_fetcher
        from src.utils.sports_fetcher import _check_api_football_daily_limit

        sports_fetcher._api_football_daily["total"] = 100
        from datetime import datetime, timezone

        sports_fetcher._api_football_daily["date"] = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

        with patch(
            "src.utils.config_loader.get",
            return_value={"api_football_max_daily_calls": 100},
        ):
            assert _check_api_football_daily_limit() is False

    def test_increment_increases_counter(self):
        from src.utils import sports_fetcher
        from src.utils.sports_fetcher import _increment_api_football_daily

        _increment_api_football_daily()
        assert sports_fetcher._api_football_daily["total"] == 1
        _increment_api_football_daily()
        assert sports_fetcher._api_football_daily["total"] == 2

    def test_counter_resets_on_new_day(self):
        from src.utils import sports_fetcher
        from src.utils.sports_fetcher import _check_api_football_daily_limit

        sports_fetcher._api_football_daily["date"] = "2020-01-01"
        sports_fetcher._api_football_daily["total"] = 999

        with patch(
            "src.utils.config_loader.get",
            return_value={"api_football_max_daily_calls": 100},
        ):
            assert _check_api_football_daily_limit() is True
        assert sports_fetcher._api_football_daily["total"] == 0

    def test_get_api_football_daily_calls(self):
        from src.utils import sports_fetcher
        from src.utils.sports_fetcher import get_api_football_daily_calls

        sports_fetcher._api_football_daily["total"] = 42
        assert get_api_football_daily_calls() == 42

    @patch("src.utils.rapidapi_client.fetch")
    @patch("src.utils.sports_fetcher.extract_teams")
    @patch("src.utils.sports_fetcher._detect_football_league")
    @patch("src.utils.sports_fetcher._check_api_football_daily_limit")
    def test_injury_fetcher_skips_when_limit_reached(
        self, mock_limit, mock_league, mock_teams, mock_fetch
    ):
        """当日调用限额已满时，fetch_api_football_injury_context 应返回空字符串。"""
        from src.utils.sports_fetcher import fetch_api_football_injury_context

        mock_limit.return_value = False
        mock_teams.return_value = ("TeamA", "TeamB")
        mock_league.return_value = 39

        with patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"}):
            result = fetch_api_football_injury_context("Will TeamA beat TeamB?")

        assert result == ""
        mock_fetch.assert_not_called()

    @patch("src.utils.sports_fetcher._format_injuries")
    @patch("src.utils.sports_fetcher._match_fixture")
    @patch("src.utils.rapidapi_client.fetch")
    @patch("src.utils.sports_fetcher.extract_teams")
    @patch("src.utils.sports_fetcher._detect_football_league")
    @patch("src.utils.sports_fetcher._check_api_football_daily_limit")
    def test_injury_fetcher_increments_counter_on_success(
        self, mock_limit, mock_league, mock_teams, mock_fetch, mock_match, mock_fmt
    ):
        """成功调用 API 后应递增计数器。"""
        from src.utils import sports_fetcher
        from src.utils.sports_fetcher import fetch_api_football_injury_context

        mock_limit.return_value = True
        mock_teams.return_value = ("Arsenal", "Chelsea")
        mock_league.return_value = 39
        mock_fetch.side_effect = [
            {
                "response": [
                    {
                        "fixture": {"id": 123},
                        "teams": {
                            "home": {"name": "Arsenal"},
                            "away": {"name": "Chelsea"},
                        },
                    }
                ]
            },
            {
                "response": [
                    {
                        "player": {
                            "name": "Saka",
                            "type": "injury",
                            "reason": "knee",
                        },
                        "team": {"name": "Arsenal"},
                    }
                ]
            },
        ]
        mock_match.return_value = 123
        mock_fmt.return_value = "injury context"

        with patch.dict("os.environ", {"RAPIDAPI_KEY": "test_key"}):

            def _config_side(key):
                if key == "sports_fetcher":
                    return {"api_football_enabled": True}
                return 2025

            with patch("src.utils.config_loader.get", side_effect=_config_side):
                result = fetch_api_football_injury_context(
                    "Premier League: Arsenal vs Chelsea"
                )

        # 两次 API 调用 → 计数器应为 2
        assert sports_fetcher._api_football_daily["total"] == 2


# ===========================================================================
# signal_engine: 每轮体育付费 API 预算
# ===========================================================================


class TestSportsEnrichedBudget:
    """测试 signal_engine 每轮体育付费 API 预算控制。"""

    @patch("src.signal.signal_engine.get")
    def test_sports_enriched_counter_limits_api_calls(self, mock_get):
        """超出 sports_max_enriched_per_cycle 后应只使用免费数据。"""
        from src.signal.signal_engine import SignalEngine

        engine = SignalEngine.__new__(SignalEngine)
        engine._sports_enriched_this_cycle = 0
        engine._log_ctx_sent = set()
        engine.smart_money = MagicMock()
        engine.smart_money.get_smart_context_for_market.return_value = ""

        # 模拟 config: 每轮最多 2 个体育市场使用付费 API
        def config_side_effect(key):
            config_map = {
                "signal": {"sports_max_enriched_per_cycle": 2},
            }
            return config_map.get(key)

        mock_get.side_effect = config_side_effect

        # 模拟所有外部数据源返回空
        with patch(
            "src.signal.signal_engine.fetch_sports_context", return_value="basic data"
        ):
            with patch(
                "src.signal.signal_engine.fetch_today_context", return_value="today"
            ) as mock_today:
                with patch(
                    "src.signal.signal_engine.fetch_odds_context", return_value="odds"
                ) as mock_odds:
                    with patch(
                        "src.signal.signal_engine.fetch_scores_context",
                        return_value="scores",
                    ) as mock_scores:
                        # 第一次调用：应该调用付费 API
                        ctx1 = engine._fetch_category_context(
                            {"id": "m1"}, "m1", "TeamA vs TeamB", "sports"
                        )
                        assert mock_today.called
                        assert mock_odds.called
                        assert engine._sports_enriched_this_cycle == 1

                        mock_today.reset_mock()
                        mock_odds.reset_mock()
                        mock_scores.reset_mock()

                        # 第二次调用：还在预算内
                        ctx2 = engine._fetch_category_context(
                            {"id": "m2"}, "m2", "TeamC vs TeamD", "sports"
                        )
                        assert mock_today.called
                        assert engine._sports_enriched_this_cycle == 2

                        mock_today.reset_mock()
                        mock_odds.reset_mock()
                        mock_scores.reset_mock()

                        # 第三次调用：超出预算，不应调用付费 API
                        ctx3 = engine._fetch_category_context(
                            {"id": "m3"}, "m3", "TeamE vs TeamF", "sports"
                        )
                        assert not mock_today.called
                        assert not mock_odds.called
                        assert not mock_scores.called
                        assert engine._sports_enriched_this_cycle == 2

    def test_log_cycle_stats_resets_counter(self):
        """log_cycle_stats 应重置体育 API 计数器。"""
        from src.signal.signal_engine import SignalEngine

        engine = SignalEngine.__new__(SignalEngine)
        engine._cycle_stats = {"ai_calls": 0, "pre_filtered": 0, "second_skipped": 0}
        engine._sports_enriched_this_cycle = 5

        stats = engine.log_cycle_stats()
        assert stats["sports_enriched"] == 5
        assert engine._sports_enriched_this_cycle == 0

    def test_reset_sports_enriched_count(self):
        """_reset_sports_enriched_count 应将计数器归零。"""
        from src.signal.signal_engine import SignalEngine

        engine = SignalEngine.__new__(SignalEngine)
        engine._sports_enriched_this_cycle = 10
        engine._reset_sports_enriched_count()
        assert engine._sports_enriched_this_cycle == 0
