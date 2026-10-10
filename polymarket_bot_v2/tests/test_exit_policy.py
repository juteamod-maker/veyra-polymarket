"""
城市NO策略: ExitPolicy 框架单元测试

测试范围：
  - BetContext.from_bet 构建
  - PolicyChain 评估逻辑
  - WeatherConfidenceHoldPolicy（止盈 + 止损）
  - CityNoScoreHoldPolicy（强持有 + 温和持有）
"""

import json
import unittest
from unittest.mock import patch

from src.exit_policy.base import (
    BetContext,
    ExitAction,
    ExitAdvice,
    ExitPolicy,
    PolicyChain,
)
from src.exit_policy.policies import CityNoScoreHoldPolicy, WeatherConfidenceHoldPolicy


def _make_bet(
    entry_price=0.54,
    outcome="NO",
    category="weather",
    ai_probability=0.05,
    strategy_tag=None,
    strategy_params=None,
    **kwargs,
):
    bet = {
        "bet_id": "test-bet-001",
        "market_id": "market-001",
        "question": "Will the highest temperature in Tokyo be 25°C?",
        "category": category,
        "outcome": outcome,
        "market_price": entry_price,
        "ai_probability": ai_probability,
        "market_end_date": "2026-03-28T00:00:00Z",
        "strategy_tag": strategy_tag,
        "strategy_params": (
            json.dumps(strategy_params)
            if isinstance(strategy_params, dict)
            else strategy_params
        ),
    }
    bet.update(kwargs)
    return bet


def _wch_cfg(enabled=True, max_ai_prob=0.25, hours=24):
    return {
        "enabled": enabled,
        "max_ai_prob": max_ai_prob,
        "hours_before_expiry": hours,
    }


def _cs_cfg(enabled=True, score_hold=None):
    base = {
        "enabled": enabled,
        "min_score": 1.2,
        "score_hold": score_hold
        or {
            "enabled": True,
            "hold_hours": 24,
            "strong_hold_min_score": 0.7,
            "mild_hold_min_score": 0.5,
        },
    }
    return base


# ===================================================================
# BetContext Tests
# ===================================================================
class TestBetContext(unittest.TestCase):
    def test_from_bet_basic(self):
        bet = _make_bet(entry_price=0.50, ai_probability=0.10)
        ctx = BetContext.from_bet(bet, 0.60, 12.0, "profit_take", "trailing")

        self.assertEqual(ctx.category, "weather")
        self.assertEqual(ctx.outcome, "NO")
        self.assertAlmostEqual(ctx.entry_price, 0.50)
        self.assertAlmostEqual(ctx.current_price, 0.60)
        self.assertAlmostEqual(ctx.return_pct, 0.20, places=2)
        self.assertAlmostEqual(ctx.ai_probability, 0.10)
        self.assertAlmostEqual(ctx.hours_to_expiry, 12.0)
        self.assertEqual(ctx.trigger_type, "profit_take")
        self.assertEqual(ctx.trigger_name, "trailing")

    def test_from_bet_with_strategy_params_json(self):
        params = {"score": 0.85, "yes_best": 25}
        bet = _make_bet(strategy_tag="city_no", strategy_params=params)
        ctx = BetContext.from_bet(bet, 0.60, 10.0, "stop_loss", "standard_sl")

        self.assertEqual(ctx.strategy_tag, "city_no")
        self.assertEqual(ctx.strategy_params["score"], 0.85)
        self.assertEqual(ctx.strategy_params["yes_best"], 25)

    def test_from_bet_no_strategy_params(self):
        bet = _make_bet()
        ctx = BetContext.from_bet(bet, 0.60, 10.0, "stop_loss", "floor")
        self.assertIsNone(ctx.strategy_tag)
        self.assertEqual(ctx.strategy_params, {})

    def test_from_bet_zero_entry_price(self):
        bet = _make_bet(entry_price=0)
        ctx = BetContext.from_bet(bet, 0.50, 5.0, "profit_take", "ceiling")
        self.assertEqual(ctx.return_pct, 0.0)


# ===================================================================
# PolicyChain Tests
# ===================================================================
class TestPolicyChain(unittest.TestCase):
    def test_empty_chain_returns_none(self):
        chain = PolicyChain([])
        bet = _make_bet()
        ctx = BetContext.from_bet(bet, 0.60, 10.0, "profit_take", "trailing")
        self.assertIsNone(chain.should_hold(ctx))

    def test_chain_returns_first_hold(self):
        class AlwaysHold(ExitPolicy):
            priority = 1

            def evaluate(self, ctx):
                return ExitAdvice(ExitAction.HOLD, "test hold", "AlwaysHold")

        class NeverHold(ExitPolicy):
            priority = 2

            def evaluate(self, ctx):
                return None

        chain = PolicyChain([NeverHold(), AlwaysHold()])
        ctx = BetContext.from_bet(_make_bet(), 0.60, 10.0, "profit_take", "trailing")
        advice = chain.should_hold(ctx)
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)
        self.assertEqual(advice.policy_name, "AlwaysHold")

    def test_chain_skip_returns_none(self):
        class SkipPolicy(ExitPolicy):
            priority = 1

            def evaluate(self, ctx):
                return ExitAdvice(ExitAction.SKIP, "skip", "SkipPolicy")

        chain = PolicyChain([SkipPolicy()])
        ctx = BetContext.from_bet(_make_bet(), 0.60, 10.0, "profit_take", "trailing")
        self.assertIsNone(chain.should_hold(ctx))

    def test_chain_exception_continues(self):
        class BrokenPolicy(ExitPolicy):
            priority = 1

            def evaluate(self, ctx):
                raise RuntimeError("oops")

        class AlwaysHold(ExitPolicy):
            priority = 2

            def evaluate(self, ctx):
                return ExitAdvice(ExitAction.HOLD, "fallback", "AlwaysHold")

        chain = PolicyChain([BrokenPolicy(), AlwaysHold()])
        ctx = BetContext.from_bet(_make_bet(), 0.60, 10.0, "profit_take", "trailing")
        advice = chain.should_hold(ctx)
        self.assertIsNotNone(advice)
        self.assertEqual(advice.policy_name, "AlwaysHold")

    def test_policy_names(self):
        chain = PolicyChain([WeatherConfidenceHoldPolicy(), CityNoScoreHoldPolicy()])
        self.assertIn("WeatherConfidenceHoldPolicy", chain.policy_names)
        self.assertIn("CityNoScoreHoldPolicy", chain.policy_names)


# ===================================================================
# WeatherConfidenceHoldPolicy Tests
# ===================================================================
class TestWeatherConfidenceHoldPolicy(unittest.TestCase):
    def _eval(self, bet, price, h2e, trigger_type, trigger_name, cfg=None):
        policy = WeatherConfidenceHoldPolicy()
        ctx = BetContext.from_bet(bet, price, h2e, trigger_type, trigger_name)
        cfg = cfg or _wch_cfg()
        with patch(
            "src.exit_policy.policies.get",
            side_effect=lambda k: cfg if k == "weather_confidence_hold" else None,
        ):
            return policy.evaluate(ctx)

    def test_holds_weather_no_low_ai_prob(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_holds_stop_loss_non_floor(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.30, 12.0, "stop_loss", "standard_sl")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_allows_certainty_sell(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.99, 12.0, "profit_take", "certainty_sell")
        self.assertIsNone(advice)

    def test_allows_floor_stop(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.03, 12.0, "stop_loss", "floor")
        self.assertIsNone(advice)

    def test_allows_effective_resolution(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.01, 12.0, "stop_loss", "effective_resolution")
        self.assertIsNone(advice)

    def test_disabled_returns_none(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(
            bet, 0.60, 12.0, "profit_take", "trailing", cfg=_wch_cfg(enabled=False)
        )
        self.assertIsNone(advice)

    def test_high_ai_prob_not_held(self):
        bet = _make_bet(ai_probability=0.50, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_yes_outcome_not_held(self):
        bet = _make_bet(ai_probability=0.05, outcome="YES", category="weather")
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_non_weather_not_held(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="sports")
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_outside_window_not_held(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, 30.0, "profit_take", "trailing")  # 30h > 24h
        self.assertIsNone(advice)

    def test_expired_not_held(self):
        """T211: h2e < -2 视为真正过期，不持有"""
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, -3.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_near_zero_h2e_still_holds(self):
        """T211: h2e=0 不再跳过策略（时区误差容错）"""
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.30, 0.0, "stop_loss", "standard_sl")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_slight_negative_h2e_still_holds(self):
        """T211: h2e=-1 在容错窗口内，仍持有"""
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.30, -1.0, "stop_loss", "standard_sl")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_no_ai_prob_not_held(self):
        bet = _make_bet(ai_probability=0, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_holds_rotation_exit(self):
        bet = _make_bet(ai_probability=0.05, outcome="NO", category="weather")
        advice = self._eval(bet, 0.60, 5.0, "profit_take", "weather_rotation_exit")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_holds_reversal_stop(self):
        bet = _make_bet(ai_probability=0.10, outcome="NO", category="weather")
        advice = self._eval(bet, 0.20, 8.0, "stop_loss", "reversal")
        self.assertIsNotNone(advice)


# ===================================================================
# CityNoScoreHoldPolicy Tests
# ===================================================================
class TestCityNoScoreHoldPolicy(unittest.TestCase):
    def _eval(self, bet, price, h2e, trigger_type, trigger_name, cfg=None):
        policy = CityNoScoreHoldPolicy()
        ctx = BetContext.from_bet(bet, price, h2e, trigger_type, trigger_name)
        cfg = cfg or _cs_cfg()
        with patch(
            "src.exit_policy.policies.get",
            side_effect=lambda k: cfg if k == "weather_city_scanner" else None,
        ):
            return policy.evaluate(ctx)

    def test_strong_hold_blocks_trailing(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.8},
        )
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.action, ExitAction.HOLD)

    def test_strong_hold_blocks_standard_sl(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.75},
        )
        advice = self._eval(bet, 0.20, 12.0, "stop_loss", "standard_sl")
        self.assertIsNotNone(advice)

    def test_mild_hold_blocks_rotation(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.55},
        )
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "rotation")
        self.assertIsNotNone(advice)

    def test_mild_hold_does_not_block_standard_sl(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.55},
        )
        advice = self._eval(bet, 0.20, 12.0, "stop_loss", "standard_sl")
        self.assertIsNone(advice)

    def test_low_score_not_held(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.3},
        )
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_non_cn_not_held(self):
        bet = _make_bet(
            strategy_tag="legacy",
            strategy_params={"score": 0.9},
        )
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_no_strategy_tag_not_held(self):
        bet = _make_bet()
        advice = self._eval(bet, 0.60, 12.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_allows_certainty_sell(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.9},
        )
        advice = self._eval(bet, 0.99, 12.0, "profit_take", "certainty_sell")
        self.assertIsNone(advice)

    def test_allows_floor_stop(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.9},
        )
        advice = self._eval(bet, 0.03, 12.0, "stop_loss", "floor")
        self.assertIsNone(advice)

    def test_disabled_returns_none(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.9},
        )
        advice = self._eval(
            bet, 0.60, 12.0, "profit_take", "trailing", cfg=_cs_cfg(enabled=False)
        )
        self.assertIsNone(advice)

    def test_outside_window_not_held(self):
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.9},
        )
        advice = self._eval(bet, 0.60, 30.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_expired_not_held(self):
        """T212: 真正过期（< -2h）不持有"""
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.8},
        )
        advice = self._eval(bet, 0.60, -3.0, "profit_take", "trailing")
        self.assertIsNone(advice)

    def test_near_zero_h2e_still_holds(self):
        """T212: h2e=0（时区偏差场景）仍应持有"""
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.8},
        )
        advice = self._eval(bet, 0.60, 0.0, "profit_take", "trailing")
        self.assertIsNotNone(advice)

    def test_slight_negative_h2e_still_holds(self):
        """T212: h2e=-1（容错窗口内）仍应持有"""
        bet = _make_bet(
            strategy_tag="city_no",
            strategy_params={"score": 0.8},
        )
        advice = self._eval(bet, 0.60, -1.0, "profit_take", "trailing")
        self.assertIsNotNone(advice)


# ===================================================================
# T211: _calc_hours_to_expiry 时区感知测试
# ===================================================================
class TestCalcHoursToExpiry(unittest.TestCase):
    """T211: 验证天气赛道使用城市本地 EOD 而非 VPS 系统时区。"""

    def setUp(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        self.tracker = OutcomeTracker()

    @patch("src.utils.weather_fetcher.get_city_utc_offset", return_value=9.0)
    @patch(
        "src.utils.weather_fetcher.parse_weather_question",
        return_value={
            "city": "Seoul",
            "unit": "C",
            "direction": "gte",
            "variable": "temp_max",
            "threshold_value": 24,
        },
    )
    def test_weather_seoul_date_only(self, mock_parse, mock_offset):
        """Seoul(UTC+9) 纯日期 → EOD 23:59 KST = 14:59 UTC"""
        from datetime import datetime, timezone

        bet = {
            "market_end_date": "2026-04-14",
            "category": "weather",
            "question": "Will the highest temperature in Seoul be 24°C or higher on April 14?",
        }
        # 模拟 03:00 UTC（= 12:00 KST）
        now_ts = datetime(2026, 4, 14, 3, 0, 0, tzinfo=timezone.utc).timestamp()
        h2e = self.tracker._calc_hours_to_expiry(bet, now_ts)
        self.assertIsNotNone(h2e)
        # EOD Seoul = 23:59:59 KST = 14:59:59 UTC
        # 从 03:00 UTC 到 14:59 UTC ≈ 12 小时
        self.assertAlmostEqual(h2e, 12.0, delta=0.1)

    @patch("src.utils.weather_fetcher.get_city_utc_offset", return_value=-4.0)
    @patch(
        "src.utils.weather_fetcher.parse_weather_question",
        return_value={
            "city": "New York",
            "unit": "F",
            "direction": "gte",
            "variable": "temp_max",
            "threshold_value": 75,
        },
    )
    def test_weather_nyc_date_only(self, mock_parse, mock_offset):
        """NYC(UTC-4) 纯日期 → EOD 23:59 EDT = 03:59+1 UTC"""
        from datetime import datetime, timezone

        bet = {
            "market_end_date": "2026-04-14",
            "category": "weather",
            "question": "Will the highest temperature in New York be 75°F or higher on April 14?",
        }
        # 模拟 18:00 UTC（= 14:00 EDT）
        now_ts = datetime(2026, 4, 14, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        h2e = self.tracker._calc_hours_to_expiry(bet, now_ts)
        self.assertIsNotNone(h2e)
        # EOD NYC = 23:59:59 EDT = 03:59:59+1 UTC (April 15 ~04:00 UTC)
        # 从 18:00 UTC 到 04:00 UTC+1 ≈ 10 小时
        self.assertAlmostEqual(h2e, 10.0, delta=0.1)

    def test_weather_iso_format_unchanged(self):
        """ISO 格式（含T）保持原行为"""
        from datetime import datetime, timezone

        bet = {
            "market_end_date": "2026-04-14T12:00:00Z",
            "category": "weather",
            "question": "Will the highest temperature in Paris be 18°C?",
        }
        now_ts = datetime(2026, 4, 14, 6, 0, 0, tzinfo=timezone.utc).timestamp()
        h2e = self.tracker._calc_hours_to_expiry(bet, now_ts)
        self.assertIsNotNone(h2e)
        # 12:00Z - 06:00Z = 6h
        self.assertAlmostEqual(h2e, 6.0, delta=0.1)

    def test_non_weather_date_only_uses_utc_midnight(self):
        """非天气赛道纯日期 → UTC midnight"""
        from datetime import datetime, timezone

        bet = {
            "market_end_date": "2026-04-14",
            "category": "sports",
            "question": "Will team A win?",
        }
        now_ts = datetime(2026, 4, 13, 20, 0, 0, tzinfo=timezone.utc).timestamp()
        h2e = self.tracker._calc_hours_to_expiry(bet, now_ts)
        self.assertIsNotNone(h2e)
        # April 14 00:00 UTC - April 13 20:00 UTC = 4h
        self.assertAlmostEqual(h2e, 4.0, delta=0.1)

    def test_empty_end_date_returns_none(self):
        """无 market_end_date → None"""
        bet = {"market_end_date": "", "category": "weather"}
        h2e = self.tracker._calc_hours_to_expiry(bet, 1700000000)
        self.assertIsNone(h2e)

    @patch("src.utils.weather_fetcher.get_city_utc_offset", return_value=9.0)
    @patch(
        "src.utils.weather_fetcher.parse_weather_question",
        return_value={
            "city": "Seoul",
            "unit": "C",
            "direction": "gte",
            "variable": "temp_max",
            "threshold_value": 24,
        },
    )
    def test_seoul_bug_scenario_not_zero(self, mock_parse, mock_offset):
        """T211 事故复现：VPS(UTC-3)在 03:00 UTC → 不再返回 0.0"""
        from datetime import datetime, timezone

        bet = {
            "market_end_date": "2026-04-14",
            "category": "weather",
            "question": "Will the highest temperature in Seoul be 24°C or higher on April 14?",
        }
        now_ts = datetime(2026, 4, 14, 3, 0, 0, tzinfo=timezone.utc).timestamp()
        h2e = self.tracker._calc_hours_to_expiry(bet, now_ts)
        self.assertIsNotNone(h2e)
        # 绝不应该是 0.0（旧BUG值）
        self.assertGreater(h2e, 5.0, "Seoul 天气不应在 03:00 UTC 计算为 0h 到期")


if __name__ == "__main__":
    unittest.main()
