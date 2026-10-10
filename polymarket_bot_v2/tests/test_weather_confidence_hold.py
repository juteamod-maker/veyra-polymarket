"""
T269: Weather AI Confidence Hold 测试

验证天气NO高置信仓位的持有到结算保护逻辑：
- check_profit_take_exits: 跳过所有止盈（certainty_sell 除外）
- check_stop_loss_exits: 跳过所有非地板止损
"""

import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.simulation.outcome_tracker import OutcomeTracker


def _cfg_profit_take_base():
    """基础 profit_taking 配置"""
    return {
        "enabled": True,
        "certainty_sell_threshold": 0.99,
        "near_ceiling_threshold": 0.99,
        "profit_take_pct": 0.99,
        "expiry_hours_threshold": 12,
        "expiry_profit_pct": 0.80,
        "weather_quick_exit_price": 0,
        "weather_quick_entry_max": 0.23,
        "quick_exit_enabled": False,
        "quick_exit_entry_max": 0.25,
        "quick_exit_profit_pct": 0.05,
        "trailing_enabled": True,
        "trailing_drop_pct": 0.08,
        "trailing_min_profit_pct": 0.05,
        "trailing_floor_min_profit_pct": 0.0,
        "trailing_partial_sell_enabled": False,
        "trailing_tiers": [],
        "weather_trailing_min_drop": 0.10,
        "hold_to_settle_threshold": 0.90,
        "hold_to_settle_hours": 18,
        "min_hold_minutes": 5,
        "legacy_cleanup_enabled": False,
    }


def _cfg_stop_loss_base():
    """基础 stop_loss 配置"""
    return {
        "enabled": True,
        "floor_price": 0.05,
        "weather_edge_floor_price": 0.03,
        "standard_stop_pct": 0.75,
        "weather_standard_stop_pct": 0.70,
        "sports_standard_stop_pct": 0.70,
        "reversal_exit_price": 0.08,
        "expiry_stop_hours": 4,
        "expiry_stop_loss_pct": 0.60,
        "early_stop": {"enabled": False},
        "weather_time_weighted_stop": {"enabled": False},
        "weather_rotation_exit": {"enabled": False},
        "stoploss_mode": {"default": "market", "per_category": {}},
        "liquidity_guard": {"enabled": False},
    }


def _cfg_wch(enabled=True, max_ai_prob=0.25, hours=24):
    """weather_confidence_hold 配置"""
    return {
        "enabled": enabled,
        "max_ai_prob": max_ai_prob,
        "hours_before_expiry": hours,
    }


def _make_bet(
    entry_price=0.54,
    outcome="NO",
    category="weather",
    ai_probability=0.05,
    end_date=None,
    bet_id="test_wch_001",
    market_id="mkt_wch",
):
    if end_date is None:
        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_date = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "bet_id": bet_id,
        "market_id": market_id,
        "question": "Will Milan high be 17°C on March 29?",
        "outcome": outcome,
        "market_price": str(entry_price),
        "ts_entered": time.time() - 7200,
        "category": category,
        "market_end_date": end_date,
        "bet_bucket": "standard",
        "ai_probability": ai_probability,
        "source": "auto",
        "ai_reasoning": "GFS 0.1%, ECMWF 0.0%",
        "size_usdc": 3.24,
        "bet_type": "entry",
        "matched_shares": 6,
    }


def _make_market_data(yes_price, yes_bid=None, yes_ask=None, market_id="mkt_wch"):
    no_price = round(1 - yes_price, 6)
    data = {
        market_id: {
            "tokens": [
                {"outcome": "Yes", "price": yes_price},
                {"outcome": "No", "price": no_price},
            ],
            "outcomePrices": f'["{yes_price}", "{no_price}"]',
        }
    }
    if yes_bid is not None:
        data[market_id]["bestBid"] = yes_bid
    if yes_ask is not None:
        data[market_id]["bestAsk"] = yes_ask
    return data


# =====================================================================
# Profit Take Tests
# =====================================================================


class TestWeatherConfidenceHoldProfitTake(unittest.TestCase):
    """验证 weather_confidence_hold 在止盈侧的保护。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _get_cfg(self, wch_enabled=True, wch_max_prob=0.25, wch_hours=24):
        pt = _cfg_profit_take_base()
        wch = _cfg_wch(wch_enabled, wch_max_prob, wch_hours)

        def cfg(key, default=None):
            if key == "profit_taking":
                return pt
            if key == "weather_confidence_hold":
                return wch
            if key == "stop_loss":
                return _cfg_stop_loss_base()
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        return cfg

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_skips_rotation_exit(self, mock_get):
        """高置信天气NO不被weather_rotation_exit提前卖出（São Paulo案例）"""
        mock_get.side_effect = self._get_cfg()
        # Enable rotation exit in stop_loss config
        sl = _cfg_stop_loss_base()
        sl["weather_rotation_exit"] = {
            "enabled": True,
            "trigger_hours": 6.0,
            "min_return_pct": -0.05,
        }

        def cfg(key, default=None):
            if key == "profit_taking":
                return _cfg_profit_take_base()
            if key == "weather_confidence_hold":
                return _cfg_wch(True)
            if key == "stop_loss":
                return sl
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        # NO@0.90, current NO price=0.89 (YES ask=0.11), within 4h of expiry
        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.90, ai_probability=0.08, end_date=end_str)
        # YES ask=0.11 → NO sell price = 1-0.11 = 0.89
        market_data = _make_market_data(0.11, yes_ask=0.11)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertEqual(len(exits), 0, "高置信天气NO不应被任何profit_take退出")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_still_allows_certainty_sell(self, mock_get):
        """天气NO@0.995 → CertaintyHold(0.97)优先，等待结算而非certainty_sell"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.54, ai_probability=0.05, end_date=end_str)
        # YES ask=0.005 → NO sell = 0.995 → CertaintyHold at 0.97 intercepts first
        market_data = _make_market_data(0.005, yes_ask=0.005)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # CertaintyHold (0.97) blocks for weather: hold to settlement ($1.00 > taker ~$0.975)
        self.assertEqual(
            len(exits),
            0,
            "天气赛道@0.995: CertaintyHold应持有到结算，不触发certainty_sell",
        )

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_ignores_high_ai_prob(self, mock_get):
        """AI概率>阈值(0.25)的天气NO不受保护（可能是错误的NO）"""
        # Enable trailing with low threshold for testing
        pt = _cfg_profit_take_base()
        pt["trailing_enabled"] = True
        pt["trailing_min_profit_pct"] = 0.01
        pt["trailing_drop_pct"] = 0.05
        pt["weather_trailing_min_drop"] = 0.05

        def cfg(key, default=None):
            if key == "profit_taking":
                return pt
            if key == "weather_confidence_hold":
                return _cfg_wch(True, max_ai_prob=0.25)
            if key == "stop_loss":
                return _cfg_stop_loss_base()
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # ai_probability=0.35 > 0.25 → NOT protected
        bet = _make_bet(entry_price=0.50, ai_probability=0.35, end_date=end_str)

        # Set up trailing: peak was 0.60, now dropped to 0.55
        peak_key = bet["bet_id"]
        self.tracker._peak_prices[peak_key] = 0.60
        # YES ask=0.45 → NO sell = 0.55
        market_data = _make_market_data(0.45, yes_ask=0.45)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # With ai_prob=0.35 > 0.25, confidence hold doesn't apply
        # Trailing should fire: drop 8.3% >= 5%, return 10% >= 1%
        self.assertGreater(len(exits), 0, "高AI概率的NO不受保护，应正常止盈")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_ignores_non_weather(self, mock_get):
        """非天气赛道不受影响"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(
            entry_price=0.54, ai_probability=0.05, end_date=end_str, category="sports"
        )
        # Set high ceiling trigger: YES ask=0.005 → NO sell=0.995
        market_data = _make_market_data(0.005, yes_ask=0.005)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # For sports, certainty_sell still triggers at 0.995
        self.assertGreater(len(exits), 0, "非天气赛道不受confidence hold限制")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_ignores_yes_outcome(self, mock_get):
        """天气YES@0.995 → CertaintyHold(0.97)同样优先，持有到结算"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(
            entry_price=0.50, ai_probability=0.05, end_date=end_str, outcome="YES"
        )
        # YES bid=0.995 → CertaintyHold at 0.97 intercepts first
        market_data = _make_market_data(0.995, yes_bid=0.995)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # CertaintyHold blocks weather bets at >= 0.97 regardless of outcome direction
        self.assertEqual(len(exits), 0, "天气YES@0.995: CertaintyHold应持有到结算")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_disabled(self, mock_get):
        """enabled=false时不生效"""
        pt = _cfg_profit_take_base()
        pt["trailing_enabled"] = True
        pt["trailing_min_profit_pct"] = 0.01
        pt["trailing_drop_pct"] = 0.05
        pt["weather_trailing_min_drop"] = 0.05

        def cfg(key, default=None):
            if key == "profit_taking":
                return pt
            if key == "weather_confidence_hold":
                return _cfg_wch(enabled=False)
            if key == "stop_loss":
                return _cfg_stop_loss_base()
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.50, ai_probability=0.05, end_date=end_str)

        peak_key = bet["bet_id"]
        self.tracker._peak_prices[peak_key] = 0.60
        # YES ask=0.45 → NO sell = 0.55
        market_data = _make_market_data(0.45, yes_ask=0.45)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # Disabled → trailing fires normally
        self.assertGreater(len(exits), 0, "disabled时不应阻止止盈")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_no_ai_data(self, mock_get):
        """ai_probability=0（无AI数据）时不保护"""
        pt = _cfg_profit_take_base()
        pt["trailing_enabled"] = True
        pt["trailing_min_profit_pct"] = 0.01
        pt["trailing_drop_pct"] = 0.05
        pt["weather_trailing_min_drop"] = 0.05

        def cfg(key, default=None):
            if key == "profit_taking":
                return pt
            if key == "weather_confidence_hold":
                return _cfg_wch(True)
            if key == "stop_loss":
                return _cfg_stop_loss_base()
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.50, ai_probability=0.0, end_date=end_str)

        peak_key = bet["bet_id"]
        self.tracker._peak_prices[peak_key] = 0.60
        market_data = _make_market_data(0.45, yes_ask=0.45)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertGreater(len(exits), 0, "无AI数据时不应保护")


# =====================================================================
# Stop Loss Tests
# =====================================================================


class TestWeatherConfidenceHoldStopLoss(unittest.TestCase):
    """验证 weather_confidence_hold 在止损侧的保护。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _get_cfg(self, wch_enabled=True, wch_max_prob=0.25, wch_hours=24):
        sl = _cfg_stop_loss_base()
        wch = _cfg_wch(wch_enabled, wch_max_prob, wch_hours)

        def cfg(key, default=None):
            if key == "stop_loss":
                return sl
            if key == "weather_confidence_hold":
                return wch
            if key == "profit_taking":
                return {"min_hold_minutes": 5}
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        return cfg

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_skips_standard_stop_loss(self, mock_get):
        """高置信天气NO跳过标准百分比止损"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # entry 0.54, YES ask 0.75 → NO sell = 0.25 → return -53.7% → standard stop
        bet = _make_bet(entry_price=0.54, ai_probability=0.05, end_date=end_str)
        market_data = _make_market_data(0.75, yes_ask=0.75)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 0, "高置信天气NO应跳过标准止损")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_allows_floor_stop(self, mock_get):
        """高置信天气NO仍允许地板止损（价格接近0）"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # entry 0.54, YES ask 0.97 → NO sell = 0.03 → below floor 0.05
        bet = _make_bet(entry_price=0.54, ai_probability=0.05, end_date=end_str)
        market_data = _make_market_data(0.97, yes_ask=0.97)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 1, "地板止损应正常触发")
        self.assertEqual(stops[0]["trigger"], "floor")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_skips_reversal_stop(self, mock_get):
        """高置信天气NO跳过方向翻转止损"""
        sl = _cfg_stop_loss_base()
        sl["reversal_exit_price"] = 0.35

        def cfg(key, default=None):
            if key == "stop_loss":
                return sl
            if key == "weather_confidence_hold":
                return _cfg_wch(True)
            if key == "profit_taking":
                return {"min_hold_minutes": 5}
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # entry 0.50, YES ask 0.72 → NO sell = 0.28 < reversal 0.35
        bet = _make_bet(entry_price=0.50, ai_probability=0.05, end_date=end_str)
        market_data = _make_market_data(0.72, yes_ask=0.72)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 0, "高置信天气NO应跳过翻转止损")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_not_applied_to_sports(self, mock_get):
        """体育赛道不受confidence hold影响"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Sports NO, deep loss
        bet = _make_bet(
            entry_price=0.54, ai_probability=0.05, end_date=end_str, category="sports"
        )
        # YES ask 0.85 → NO sell = 0.15 → return -72.2% >= -70% stop
        market_data = _make_market_data(0.85, yes_ask=0.85)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(len(stops), 0, "体育赛道不受confidence hold保护")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_disabled_allows_stop(self, mock_get):
        """disabled时标准止损正常触发"""
        mock_get.side_effect = self._get_cfg(wch_enabled=False)

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.54, ai_probability=0.05, end_date=end_str)
        # YES ask 0.85 → NO sell = 0.15 → return -72.2% >= -70% stop
        market_data = _make_market_data(0.85, yes_ask=0.85)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(len(stops), 0, "disabled时止损正常")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_high_ai_prob_allows_stop(self, mock_get):
        """ai_prob > max_ai_prob时止损正常"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # ai_probability=0.40 > 0.25
        bet = _make_bet(entry_price=0.54, ai_probability=0.40, end_date=end_str)
        market_data = _make_market_data(0.85, yes_ask=0.85)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(len(stops), 0, "高AI概率的NO不受保护")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_expired_market_not_protected(self, mock_get):
        """已过期的市场不受保护"""
        mock_get.side_effect = self._get_cfg()

        # Already expired (2 hours ago)
        end_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.54, ai_probability=0.05, end_date=end_str)
        market_data = _make_market_data(0.85, yes_ask=0.85)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        # hours_to_expiry < 0 → confidence hold skipped → stop triggers
        self.assertGreater(len(stops), 0, "已过期市场不受保护")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_milan_case(self, mock_get):
        """Milan 17°C案例: NO@0.54, ai=0.001 → 应持有到结算"""
        mock_get.side_effect = self._get_cfg()

        end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.54, ai_probability=0.001, end_date=end_str)
        # YES price goes up a bit: ask=0.50 → NO sell=0.50 → return -7.4%
        market_data = _make_market_data(0.50, yes_ask=0.50)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 0, "Milan案例: 高置信NO应持有")

    @patch("src.simulation.outcome_tracker.get")
    def test_hold_sao_paulo_case_profit_take(self, mock_get):
        """São Paulo 28°C案例: NO@0.90, ai=0.08 → 不应被rotation退出"""
        sl = _cfg_stop_loss_base()
        sl["weather_rotation_exit"] = {
            "enabled": True,
            "trigger_hours": 6.0,
            "min_return_pct": -0.05,
        }

        def cfg(key, default=None):
            if key == "profit_taking":
                return _cfg_profit_take_base()
            if key == "weather_confidence_hold":
                return _cfg_wch(True)
            if key == "stop_loss":
                return sl
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        mock_get.side_effect = cfg

        end_dt = datetime.now(timezone.utc) + timedelta(hours=3)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = _make_bet(entry_price=0.90, ai_probability=0.08, end_date=end_str)
        # YES ask=0.11 → NO sell=0.89 → return -1.1%
        market_data = _make_market_data(0.11, yes_ask=0.11)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertEqual(len(exits), 0, "São Paulo案例: 高置信NO不应被rotation退出")


# =====================================================================
# T183: Rotation Exit Certainty Buffer Tests
# =====================================================================


class TestRotationExitCertaintyBuffer(unittest.TestCase):
    """T183: weather_rotation_exit 接近确定性缓冲保护测试

    验证：当价格接近 CertaintyHold 阈值时，rotation exit 被跳过，
    防止 Munich 类事件（价格0.89被轮动退出，之后涨至0.95+）
    """

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_config(
        self,
        cert_buffer=0.10,
        cert_threshold=0.95,
        rotation_enabled=True,
        rotation_min_return=-0.05,
    ):
        sl = _cfg_stop_loss_base()
        sl["weather_rotation_exit"] = {
            "enabled": rotation_enabled,
            "trigger_hours": 6.0,
            "min_return_pct": rotation_min_return,
            "certainty_buffer": cert_buffer,
        }
        pt = _cfg_profit_take_base()
        pt["certainty_hold_threshold"] = cert_threshold
        pt["certainty_hold_categories"] = ["weather"]
        # Disable trailing to isolate rotation exit
        pt["trailing_enabled"] = False

        def cfg(key, default=None):
            if key == "profit_taking":
                return pt
            if key == "weather_confidence_hold":
                return _cfg_wch(False)  # Disable confidence hold for isolation
            if key == "stop_loss":
                return sl
            if key == "sports_live":
                return {"game_window_hours": 4, "post_expiry_window_hours": 2}
            return default

        return cfg

    @patch("src.simulation.outcome_tracker.get")
    def test_near_certainty_skips_rotation_exit(self, mock_get):
        """Munich案例: price=0.89, threshold=0.95, buffer=0.10 → 0.89>=0.85 → 跳过rotation"""
        mock_get.side_effect = self._make_config(cert_buffer=0.10, cert_threshold=0.95)

        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # NO@0.770, current YES ask=0.11 → NO sell=0.89 → return=15.6%
        bet = _make_bet(entry_price=0.770, ai_probability=0.30, end_date=end_str)
        market_data = _make_market_data(0.11, yes_ask=0.11)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # Should NOT trigger rotation_exit because 0.89 >= 0.95-0.10=0.85
        rotation_exits = [
            e for e in exits if e.get("trigger") == "weather_rotation_exit"
        ]
        self.assertEqual(
            len(rotation_exits),
            0,
            "价格0.89接近确定性(0.95-0.10=0.85)，不应触发rotation exit",
        )

    @patch("src.simulation.outcome_tracker.get")
    def test_low_price_allows_rotation_exit(self, mock_get):
        """price=0.70, threshold=0.95, buffer=0.10 → 0.70<0.85 → 允许rotation exit"""
        mock_get.side_effect = self._make_config(
            cert_buffer=0.10, cert_threshold=0.95, rotation_min_return=-0.05
        )

        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # NO@0.660, current YES ask=0.30 → NO sell=0.70 → return=6.1%
        bet = _make_bet(entry_price=0.660, ai_probability=0.30, end_date=end_str)
        market_data = _make_market_data(0.30, yes_ask=0.30)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        rotation_exits = [
            e for e in exits if e.get("trigger") == "weather_rotation_exit"
        ]
        self.assertEqual(
            len(rotation_exits), 1, "价格0.70远低于确定性阈值，应正常触发rotation exit"
        )

    @patch("src.simulation.outcome_tracker.get")
    def test_buffer_zero_disables_protection(self, mock_get):
        """buffer=0 → 不保护，rotation exit正常触发（即使价格高）"""
        mock_get.side_effect = self._make_config(
            cert_buffer=0.0, cert_threshold=0.95, rotation_min_return=-0.05
        )

        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # NO@0.770, YES ask=0.11 → NO sell=0.89 → return=15.6%
        bet = _make_bet(entry_price=0.770, ai_probability=0.30, end_date=end_str)
        market_data = _make_market_data(0.11, yes_ask=0.11)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        rotation_exits = [
            e for e in exits if e.get("trigger") == "weather_rotation_exit"
        ]
        self.assertEqual(
            len(rotation_exits), 1, "buffer=0时不保护，应正常触发rotation exit"
        )

    @patch("src.simulation.outcome_tracker.get")
    def test_exact_boundary_skips_rotation(self, mock_get):
        """price恰好等于threshold-buffer → 跳过rotation"""
        mock_get.side_effect = self._make_config(cert_buffer=0.10, cert_threshold=0.95)

        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # NO@0.700, YES ask=0.15 → NO sell=0.85 → exactly at threshold-buffer
        bet = _make_bet(entry_price=0.700, ai_probability=0.30, end_date=end_str)
        market_data = _make_market_data(0.15, yes_ask=0.15)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        rotation_exits = [
            e for e in exits if e.get("trigger") == "weather_rotation_exit"
        ]
        self.assertEqual(
            len(rotation_exits),
            0,
            "价格恰好等于threshold-buffer(0.85)，应跳过rotation exit",
        )

    @patch("src.simulation.outcome_tracker.get")
    def test_non_weather_category_not_affected(self, mock_get):
        """非天气赛道不受certainty_buffer保护影响（rotation_exit仅对weather生效）"""
        mock_get.side_effect = self._make_config(cert_buffer=0.10, cert_threshold=0.95)

        end_dt = datetime.now(timezone.utc) + timedelta(hours=4)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # 用sports类别
        bet = _make_bet(
            entry_price=0.770, ai_probability=0.30, end_date=end_str, category="sports"
        )
        market_data = _make_market_data(0.11, yes_ask=0.11)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # rotation_exit只对weather生效，sports不触发
        rotation_exits = [
            e for e in exits if e.get("trigger") == "weather_rotation_exit"
        ]
        self.assertEqual(len(rotation_exits), 0, "非天气赛道不应触发rotation exit")


if __name__ == "__main__":
    unittest.main()
