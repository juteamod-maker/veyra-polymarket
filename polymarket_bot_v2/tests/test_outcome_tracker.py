"""
test_outcome_tracker.py — OutcomeTracker 单元测试
覆盖：_resolve_by_outcome_prices / _parse_outcome / _calc_pnl / CITY_COORDS
"""

import unittest
import unittest.mock
from unittest.mock import patch

from src.simulation.outcome_tracker import CITY_COORDS, OutcomeTracker


class TestResolveByOutcomePrices(unittest.TestCase):
    """测试 _resolve_by_outcome_prices 静态方法。"""

    def test_binary_yes_wins(self):
        status = {"outcomePrices": '["1","0"]', "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "YES")

    def test_binary_no_wins(self):
        status = {"outcomePrices": '["0","1"]', "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "NO")

    def test_named_outcome_first_wins(self):
        """体育市场：Lightning vs Canucks，Lightning 胜。"""
        status = {
            "outcomePrices": '["1","0"]',
            "outcomes": '["Lightning","Canucks"]',
        }
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "LIGHTNING")

    def test_named_outcome_second_wins(self):
        """体育市场：Up vs Down，Down 胜。"""
        status = {"outcomePrices": '["0.0005","0.9995"]', "outcomes": '["Up","Down"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "DOWN")

    def test_no_clear_winner(self):
        """价格均低于 0.95 → 无法判断。"""
        status = {
            "outcomePrices": '["0.775","0.225"]',
            "outcomes": '["Knicks","Nets"]',
        }
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "")

    def test_threshold_exactly_095(self):
        status = {"outcomePrices": '["0.95","0.05"]', "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "YES")

    def test_threshold_below_095(self):
        status = {"outcomePrices": '["0.949","0.051"]', "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "")

    def test_empty_prices(self):
        status = {"outcomePrices": "[]", "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "")

    def test_missing_prices(self):
        status = {"outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "")

    def test_missing_outcomes_binary_fallback(self):
        """无 outcomes 名称时，二元市场回退到 YES/NO。"""
        status = {"outcomePrices": '["1","0"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "YES")

    def test_missing_outcomes_binary_fallback_no(self):
        status = {"outcomePrices": '["0","1"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "NO")

    def test_list_type_prices(self):
        """outcomePrices 已是列表而非 JSON 字符串。"""
        status = {"outcomePrices": ["1", "0"], "outcomes": ["Yes", "No"]}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "YES")

    def test_invalid_json_returns_empty(self):
        status = {"outcomePrices": "not-json", "outcomes": '["Yes","No"]'}
        self.assertEqual(OutcomeTracker._resolve_by_outcome_prices(status), "")


class TestParseOutcome(unittest.TestCase):
    """测试 _parse_outcome 静态方法。"""

    def test_yes(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": "Yes"}), "YES")

    def test_no(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": "No"}), "NO")

    def test_true(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": "True"}), "YES")

    def test_false(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": "False"}), "NO")

    def test_named_outcome(self):
        """命名结果如 'Lightning' 应原样大写返回。"""
        self.assertEqual(
            OutcomeTracker._parse_outcome({"outcome": "Lightning"}), "LIGHTNING"
        )

    def test_named_outcome_down(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": "Down"}), "DOWN")

    def test_empty(self):
        self.assertEqual(OutcomeTracker._parse_outcome({"outcome": ""}), "")

    def test_none(self):
        self.assertEqual(OutcomeTracker._parse_outcome({}), "")


class TestCalcPnl(unittest.TestCase):
    """测试 _calc_pnl 静态方法。"""

    def test_win_yes(self):
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "YES"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "YES")
        self.assertEqual(is_win, 1)
        self.assertAlmostEqual(payout, 2.0, places=2)
        self.assertAlmostEqual(pnl, 1.0, places=2)

    def test_loss_yes(self):
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "YES"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "NO")
        self.assertEqual(is_win, 0)
        self.assertEqual(payout, 0.0)
        self.assertAlmostEqual(pnl, -1.0, places=2)

    def test_named_outcome_win(self):
        """命名结果赢。"""
        bet = {"size_usdc": 2.0, "market_price": 0.4, "outcome": "NETS"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "NETS")
        self.assertEqual(is_win, 1)
        self.assertAlmostEqual(payout, 5.0, places=2)

    def test_named_outcome_loss(self):
        """命名结果输。"""
        bet = {"size_usdc": 2.0, "market_price": 0.4, "outcome": "Canucks"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "LIGHTNING")
        self.assertEqual(is_win, 0)

    def test_case_insensitive_match(self):
        """_calc_pnl 通过 .upper() 比较。"""
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "Down"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "DOWN")
        self.assertEqual(is_win, 1)

    # --- O/U 等价映射测试（Task 107 修复）---

    def test_over_under_yes_wins_over(self):
        """投注 YES，市场解析为 OVER → 应赢。"""
        bet = {"size_usdc": 1.08, "market_price": 0.48, "outcome": "YES"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "OVER")
        self.assertEqual(is_win, 1)
        self.assertAlmostEqual(payout, 2.25, places=2)
        self.assertAlmostEqual(pnl, 1.17, places=2)

    def test_over_under_no_wins_under(self):
        """投注 NO，市场解析为 UNDER → 应赢。"""
        bet = {"size_usdc": 1.0, "market_price": 0.4, "outcome": "NO"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "UNDER")
        self.assertEqual(is_win, 1)
        self.assertAlmostEqual(payout, 2.5, places=2)

    def test_over_under_yes_loses_under(self):
        """投注 YES，市场解析为 UNDER → 应输。"""
        bet = {"size_usdc": 1.0, "market_price": 0.6, "outcome": "YES"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "UNDER")
        self.assertEqual(is_win, 0)
        self.assertEqual(payout, 0.0)

    def test_over_under_no_loses_over(self):
        """投注 NO，市场解析为 OVER → 应输。"""
        bet = {"size_usdc": 1.0, "market_price": 0.4, "outcome": "NO"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "OVER")
        self.assertEqual(is_win, 0)

    def test_over_resolves_as_yes_equivalent(self):
        """投注 OVER 和 YES 应互相视为赢。"""
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "Over"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "YES")
        self.assertEqual(is_win, 1)

    def test_under_resolves_as_no_equivalent(self):
        """投注 Under 和 NO 应互相视为赢。"""
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "Under"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "NO")
        self.assertEqual(is_win, 1)

    def test_named_team_not_equivalent_to_yes(self):
        """具名结果（NETS）不应与 YES 等价。"""
        bet = {"size_usdc": 1.0, "market_price": 0.5, "outcome": "NETS"}
        pnl, payout, is_win = OutcomeTracker._calc_pnl(bet, "YES")
        self.assertEqual(is_win, 0)


class TestCityCoords(unittest.TestCase):
    """验证 CITY_COORDS 包含关键城市。"""

    def test_critical_cities_present(self):
        critical = [
            "Atlanta",
            "Dallas",
            "Shanghai",
            "Seoul",
            "Houston",
            "San Francisco",
            "Boston",
            "Phoenix",
            "Las Vegas",
            "Beijing",
            "Bangkok",
            "Taipei",
        ]
        for city in critical:
            self.assertIn(city, CITY_COORDS, f"CITY_COORDS 缺少关键城市: {city}")

    def test_coords_are_valid_tuples(self):
        for city, coords in CITY_COORDS.items():
            self.assertIsInstance(coords, tuple, f"{city}: 坐标应为 tuple")
            self.assertEqual(len(coords), 2, f"{city}: 坐标应有 2 个元素")
            lat, lon = coords
            self.assertTrue(-90 <= lat <= 90, f"{city}: 纬度 {lat} 超出范围")
            self.assertTrue(-180 <= lon <= 180, f"{city}: 经度 {lon} 超出范围")


# ---------------------------------------------------------------------------
# Phase 33: 止损最小持仓时间保护
# ---------------------------------------------------------------------------


class TestStopLossMinHoldSec(unittest.TestCase):
    """验证 check_stop_loss_exits 尊重 min_hold_minutes 保护。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_bet(self, ts_entered, entry_price=0.50):
        import time

        return {
            "bet_id": "test_sl_hold",
            "market_id": "mkt_sl",
            "question": "Test stop loss hold",
            "outcome": "YES",
            "market_price": str(entry_price),
            "ts_entered": ts_entered,
            "category": "crypto",
            "market_end_date": "",
            "bet_bucket": "",
            "ai_probability": 0.6,
            "source": "auto",
            "ai_reasoning": "",
            "size_usdc": 3.0,
        }

    def _make_market_data(self, price):
        return {
            "mkt_sl": {
                "tokens": [
                    {"outcome": "Yes", "price": price},
                    {"outcome": "No", "price": round(1 - price, 4)},
                ],
                "outcomePrices": f'["{price}", "{round(1 - price, 4)}"]',
            }
        }

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_new_bet_skipped_by_min_hold(self, mock_get):
        """刚买入(<5min)的仓位不应被止损"""
        import time

        def cfg(key, default=None):
            mapping = {
                "stop_loss": {
                    "enabled": True,
                    "floor_price": 0.05,
                    "standard_stop_pct": 0.75,
                    "weather_standard_stop_pct": 0.75,
                    "expiry_stop_hours": 4.0,
                    "expiry_stop_loss_pct": 0.60,
                    "reversal_exit_price": 0.08,
                    "sports_standard_stop_pct": 0.70,
                    "early_stop": {"enabled": False},
                    "liquidity_guard": {"enabled": False},
                },
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.05,
                    "standard_stop_pct": 0.80,
                    "reversal_exit_price": 0.08,
                },
                "profit_taking": {"min_hold_minutes": 5},
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg

        # Bet entered 10 seconds ago — should be protected by min_hold_sec
        bet = self._make_bet(ts_entered=time.time() - 10, entry_price=0.50)
        # Price dropped to 0.05 (floor territory) — extreme drop
        market_data = self._make_market_data(0.05)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 0, "New bet should not be stop-lossed")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_old_bet_can_be_stopped(self, mock_get):
        """持仓>5min 的仓位可正常触发止损"""
        import time

        def cfg(key, default=None):
            mapping = {
                "stop_loss": {
                    "enabled": True,
                    "floor_price": 0.05,
                    "standard_stop_pct": 0.75,
                    "weather_standard_stop_pct": 0.75,
                    "expiry_stop_hours": 4.0,
                    "expiry_stop_loss_pct": 0.60,
                    "reversal_exit_price": 0.08,
                    "sports_standard_stop_pct": 0.70,
                    "early_stop": {"enabled": False},
                    "liquidity_guard": {"enabled": False},
                },
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.05,
                    "standard_stop_pct": 0.80,
                    "reversal_exit_price": 0.08,
                },
                "profit_taking": {"min_hold_minutes": 5},
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg

        # Bet entered 10 minutes ago — past min_hold
        bet = self._make_bet(ts_entered=time.time() - 600, entry_price=0.50)
        # Price dropped to 0.05 (floor territory)
        market_data = self._make_market_data(0.05)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(len(stops), 0, "Old bet should be stop-lossed")


class TestValidateSelfResolution(unittest.TestCase):
    """Phase43: 测试 _validate_self_resolution 交叉验证逻辑。"""

    def test_yes_agrees_with_high_yes_price(self):
        """自解析 YES + outcomePrices YES=0.95 → 通过。"""
        status = {"outcomePrices": '["0.95","0.05"]', "outcomes": '["Yes","No"]'}
        self.assertTrue(OutcomeTracker._validate_self_resolution("YES", status))

    def test_no_agrees_with_high_no_price(self):
        """自解析 NO + outcomePrices NO=0.95 → 通过。"""
        status = {"outcomePrices": '["0.05","0.95"]', "outcomes": '["Yes","No"]'}
        self.assertTrue(OutcomeTracker._validate_self_resolution("NO", status))

    def test_london_bug_blocked(self):
        """London 15°C 复现：自解析 NO 但 YES=0.9995 → 拒绝。"""
        status = {"outcomePrices": '["0.9995","0.0005"]', "outcomes": '["Yes","No"]'}
        self.assertFalse(OutcomeTracker._validate_self_resolution("NO", status))

    def test_seattle_bug_blocked(self):
        """Seattle 48-49°F 复现：自解析 YES 但 YES=0.2515 → 拒绝。"""
        status = {"outcomePrices": '["0.2515","0.7485"]', "outcomes": '["Yes","No"]'}
        self.assertFalse(OutcomeTracker._validate_self_resolution("YES", status))

    def test_dallas_correct_passes(self):
        """Dallas 76-77°F：自解析 NO + NO=0.96 ≥ 0.95 → 通过。"""
        status = {"outcomePrices": '["0.04","0.96"]', "outcomes": '["Yes","No"]'}
        self.assertTrue(OutcomeTracker._validate_self_resolution("NO", status))

    def test_borderline_095_passes(self):
        """恰好 0.95 边界 → 通过。"""
        status = {"outcomePrices": '["0.95","0.05"]', "outcomes": '["Yes","No"]'}
        self.assertTrue(OutcomeTracker._validate_self_resolution("YES", status))

    def test_borderline_094_fails(self):
        """低于 0.95 → 拒绝（Phase48: 防止过早自解析）。"""
        status = {"outcomePrices": '["0.94","0.06"]', "outcomes": '["Yes","No"]'}
        self.assertFalse(OutcomeTracker._validate_self_resolution("YES", status))

    def test_named_outcome_validation(self):
        """体育市场命名 outcome 验证。"""
        status = {
            "outcomePrices": '["0.95","0.05"]',
            "outcomes": '["Lightning","Canucks"]',
        }
        self.assertTrue(OutcomeTracker._validate_self_resolution("LIGHTNING", status))
        self.assertFalse(OutcomeTracker._validate_self_resolution("CANUCKS", status))

    def test_missing_prices_passes(self):
        """无 outcomePrices 时放行（无法验证）。"""
        self.assertTrue(OutcomeTracker._validate_self_resolution("YES", {}))

    def test_empty_prices_passes(self):
        """空价格列表放行。"""
        status = {"outcomePrices": "[]"}
        self.assertTrue(OutcomeTracker._validate_self_resolution("YES", status))

    def test_list_type_prices(self):
        """outcomePrices 已是列表类型。"""
        status = {"outcomePrices": ["0.99", "0.01"], "outcomes": ["Yes", "No"]}
        self.assertTrue(OutcomeTracker._validate_self_resolution("YES", status))
        self.assertFalse(OutcomeTracker._validate_self_resolution("NO", status))

    def test_binary_fallback_no_outcomes(self):
        """无 outcomes 名称时 fallback 到 YES/NO。"""
        status = {"outcomePrices": '["0.01","0.99"]'}
        self.assertTrue(OutcomeTracker._validate_self_resolution("NO", status))
        self.assertFalse(OutcomeTracker._validate_self_resolution("YES", status))


# ---------------------------------------------------------------------------
# Task 109/110: Certainty Sell — 价格接近 $1.00 时无条件卖出
# ---------------------------------------------------------------------------


class TestCertaintySell(unittest.TestCase):
    """验证 check_profit_take_exits 的 certainty_sell 逻辑。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_bet(
        self,
        entry_price=0.82,
        outcome="NO",
        category="weather",
        ts_entered=None,
        end_date="2026-03-25T12:00:00Z",
    ):
        import time

        return {
            "bet_id": "test_certainty",
            "market_id": "mkt_cert",
            "question": "Will temp in Beijing be 22C?",
            "outcome": outcome,
            "market_price": str(entry_price),
            "ts_entered": ts_entered or (time.time() - 3600),
            "category": category,
            "market_end_date": end_date,
            "bet_bucket": "weather",
            "ai_probability": 0.0,
            "source": "auto",
            "ai_reasoning": "",
            "size_usdc": 4.1,
            "bet_type": "entry",
        }

    def _make_market_data(self, yes_price, yes_bid=None, yes_ask=None):
        """构造 market_data，支持极端价格场景。"""
        no_price = round(1 - yes_price, 6)
        data = {
            "mkt_cert": {
                "tokens": [
                    {"outcome": "Yes", "price": yes_price},
                    {"outcome": "No", "price": no_price},
                ],
                "outcomePrices": f'["{yes_price}", "{no_price}"]',
            }
        }
        if yes_bid is not None:
            data["mkt_cert"]["bestBid"] = yes_bid
        if yes_ask is not None:
            data["mkt_cert"]["bestAsk"] = yes_ask
        return data

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_sell_at_099(self, mock_get):
        """NO仓位当NO价格=0.999(YES ask=0.001)时触发certainty_sell"""

        def cfg(key, default=None):
            if key == "profit_taking":
                return {
                    "enabled": True,
                    "certainty_sell_threshold": 0.99,
                    "certainty_hold_categories": [],  # 禁用CertaintyHold以测试certainty_sell
                    "near_ceiling_threshold": 0.95,
                    "hold_to_settle_threshold": 0.90,
                    "hold_to_settle_hours": 48,
                    "min_hold_minutes": 5,
                    "trailing_enabled": False,
                    "quick_exit_enabled": False,
                    "legacy_cleanup_enabled": False,
                    "high_conviction": {"enabled": False},
                }
            if key == "sports_live":
                return {
                    "game_window_hours": 4,
                    "post_expiry_window_hours": 2,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.4,
                }
            return default

        mock_get.side_effect = cfg

        bet = self._make_bet(entry_price=0.82, outcome="NO")
        # YES ask=0.001 → NO sell_price = 1-0.001 = 0.999
        market_data = self._make_market_data(0.001, yes_ask=0.001)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["trigger"], "certainty_sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_sell_overrides_hold_to_settle(self, mock_get):
        """即使满足 hold-to-settle 条件，certainty_sell 仍应优先执行（当CertaintyHold禁用时）"""

        def cfg(key, default=None):
            if key == "profit_taking":
                return {
                    "enabled": True,
                    "certainty_sell_threshold": 0.99,
                    "certainty_hold_categories": [],  # 禁用CertaintyHold以测试certainty_sell规则
                    "near_ceiling_threshold": 0.95,
                    "hold_to_settle_threshold": 0.90,
                    "hold_to_settle_hours": 48,
                    "min_hold_minutes": 5,
                    "trailing_enabled": False,
                    "quick_exit_enabled": False,
                    "legacy_cleanup_enabled": False,
                    "high_conviction": {"enabled": False},
                }
            if key == "sports_live":
                return {
                    "game_window_hours": 4,
                    "post_expiry_window_hours": 2,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.4,
                }
            return default

        mock_get.side_effect = cfg

        # Bet close to expiry (within 48h) AND price >= 0.90 → hold-to-settle
        # BUT price >= 0.99 → certainty_sell should override
        import time
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_bet(entry_price=0.82, outcome="YES", end_date=end_str)
        # YES price at 0.995 → sell price = bestBid = 0.995
        market_data = self._make_market_data(0.995, yes_bid=0.995, yes_ask=0.999)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["trigger"], "certainty_sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_no_certainty_sell_below_threshold(self, mock_get):
        """价格 0.95 不触发 certainty_sell (threshold=0.99)"""

        def cfg(key, default=None):
            if key == "profit_taking":
                return {
                    "enabled": True,
                    "certainty_sell_threshold": 0.99,
                    "near_ceiling_threshold": 0.95,
                    "hold_to_settle_threshold": 0.90,
                    "hold_to_settle_hours": 48,
                    "min_hold_minutes": 5,
                    "trailing_enabled": False,
                    "quick_exit_enabled": False,
                    "legacy_cleanup_enabled": False,
                    "high_conviction": {"enabled": False},
                }
            if key == "sports_live":
                return {
                    "game_window_hours": 4,
                    "post_expiry_window_hours": 2,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.4,
                }
            return default

        mock_get.side_effect = cfg

        import time
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_bet(entry_price=0.82, outcome="YES", end_date=end_str)
        # YES price 0.95 → hold-to-settle should capture this, NOT certainty_sell
        market_data = self._make_market_data(0.95, yes_bid=0.95, yes_ask=0.96)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        # Hold-to-settle (0.95 >= 0.90 AND within 48h) → no exits
        self.assertEqual(len(exits), 0)

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_sell_exactly_at_100(self, mock_get):
        """价格恰好到 1.00 时触发 certainty_sell（当CertaintyHold禁用时）"""

        def cfg(key, default=None):
            if key == "profit_taking":
                return {
                    "enabled": True,
                    "certainty_sell_threshold": 0.99,
                    "certainty_hold_categories": [],  # 禁用CertaintyHold以测试certainty_sell
                    "near_ceiling_threshold": 0.95,
                    "hold_to_settle_threshold": 0.90,
                    "hold_to_settle_hours": 48,
                    "min_hold_minutes": 5,
                    "trailing_enabled": False,
                    "quick_exit_enabled": False,
                    "legacy_cleanup_enabled": False,
                    "high_conviction": {"enabled": False},
                }
            if key == "sports_live":
                return {
                    "game_window_hours": 4,
                    "post_expiry_window_hours": 2,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.4,
                }
            return default

        mock_get.side_effect = cfg

        bet = self._make_bet(entry_price=0.67, outcome="YES")
        market_data = self._make_market_data(1.0, yes_bid=1.0, yes_ask=1.0)

        exits = self.tracker.check_profit_take_exits([bet], market_data=market_data)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["trigger"], "certainty_sell")
        self.assertAlmostEqual(exits[0]["return_pct"], (1.0 - 0.67) / 0.67, places=2)


class TestCertaintyHold(unittest.TestCase):
    """验证 CertaintyHold 逻辑：天气赛道高价格等待结算，不触发 taker 卖出。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_bet(self, entry_price=0.30, outcome="NO", category="weather"):
        import time

        return {
            "bet_id": "test_ch",
            "market_id": "mkt_ch",
            "question": "Will temp exceed 25C?",
            "outcome": outcome,
            "market_price": str(entry_price),
            "ts_entered": time.time() - 3600,
            "category": category,
            "market_end_date": "2026-04-12T12:00:00Z",
            "bet_bucket": "weather",
            "ai_probability": 0.0,
            "source": "auto",
            "ai_reasoning": "",
            "size_usdc": 5.0,
            "bet_type": "entry",
        }

    def _cfg(self, hold_threshold=0.97, hold_categories=None):
        if hold_categories is None:
            hold_categories = ["weather"]

        def cfg(key, default=None):
            if key == "profit_taking":
                return {
                    "enabled": True,
                    "certainty_sell_threshold": 0.99,
                    "certainty_hold_threshold": hold_threshold,
                    "certainty_hold_categories": hold_categories,
                    "near_ceiling_threshold": 0.95,
                    "hold_to_settle_threshold": 0.90,
                    "hold_to_settle_hours": 48,
                    "min_hold_minutes": 0,
                    "trailing_enabled": False,
                    "quick_exit_enabled": False,
                    "legacy_cleanup_enabled": False,
                    "high_conviction": {"enabled": False},
                }
            if key == "sports_live":
                return {
                    "game_window_hours": 4,
                    "post_expiry_window_hours": 2,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.4,
                }
            return default

        return cfg

    def _mkt(self, yes_price):
        no_price = round(1 - yes_price, 6)
        return {
            "mkt_ch": {
                "tokens": [
                    {"outcome": "Yes", "price": yes_price},
                    {"outcome": "No", "price": no_price},
                ],
                "outcomePrices": f'["{yes_price}", "{no_price}"]',
            }
        }

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_weather_at_098(self, mock_get):
        """天气赛道 NO 价格 0.98 ≥ 0.97 → CertaintyHold 拦截，不卖出"""
        mock_get.side_effect = self._cfg()
        bet = self._make_bet(outcome="NO", category="weather")
        # YES ask=0.02 → NO price=0.98
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.02))
        self.assertEqual(
            len(exits), 0, "天气赛道 0.98 应被 CertaintyHold 拦截，等待结算"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_does_not_fire_below_threshold(self, mock_get):
        """天气赛道 NO 价格 0.94 < 0.97 → CertaintyHold 不触发，且低于 near_ceiling 0.95"""
        mock_get.side_effect = self._cfg()
        # 使用高入场价 0.90 避免触发 ceiling（return 仅 +4.4%）
        bet = self._make_bet(outcome="NO", category="weather", entry_price=0.90)
        # YES=0.06 → NO=0.94 < 0.95(near_ceiling): 不触发 ceiling
        # NO=0.94 < 0.97(certainty_hold): 不被 CertaintyHold 拦截
        # NO=0.94 < 0.99(certainty_sell): 不触发 certainty_sell
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.06))
        self.assertEqual(len(exits), 0)

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_does_not_fire_for_sports(self, mock_get):
        """非 weather 赛道（sports）在 0.98 时不受 CertaintyHold 保护 → 继续 certainty_sell"""
        mock_get.side_effect = self._cfg(hold_categories=["weather"])  # only weather
        bet = self._make_bet(outcome="YES", category="sports")
        # YES price=0.999 ≥ 0.99 certainty_sell_threshold → certainty_sell fires
        exits = self.tracker.check_profit_take_exits(
            [bet], market_data=self._mkt(0.999)
        )
        self.assertEqual(
            len(exits), 1, "sports 赛道不受 CertaintyHold 保护，应触发 certainty_sell"
        )
        self.assertEqual(exits[0]["trigger"], "certainty_sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_disabled_by_empty_categories(self, mock_get):
        """certainty_hold_categories=[] 时 CertaintyHold 全局禁用"""
        mock_get.side_effect = self._cfg(hold_categories=[])
        bet = self._make_bet(outcome="NO", category="weather")
        # YES=0.001 → NO=0.999 ≥ 0.99 certainty_sell_threshold → certainty_sell fires
        exits = self.tracker.check_profit_take_exits(
            [bet], market_data=self._mkt(0.001)
        )
        self.assertEqual(
            len(exits), 1, "CertaintyHold 禁用后 certainty_sell 应正常触发"
        )
        self.assertEqual(exits[0]["trigger"], "certainty_sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_saves_fees_vs_taker_sell(self, mock_get):
        """验证 CertaintyHold 拦截使收益保持 $1.00/share（而非付 taker fee 后的 ~$0.97）"""
        mock_get.side_effect = self._cfg()
        bet = self._make_bet(entry_price=0.20, outcome="YES", category="weather")
        # YES=0.99 ≥ 0.97 → CertaintyHold 拦截，不生成 exit
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.99))
        self.assertEqual(
            len(exits), 0, "0.99 天气赛道应等待结算拿满额 $1.00，不应 taker 卖出"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_timeout_forces_sell(self, mock_get):
        """T235: CertaintyHold 超时后应放行卖出释放资金"""
        mock_get.side_effect = self._cfg()
        bet = self._make_bet(outcome="NO", category="weather")
        bet["bet_id"] = "test_ch_timeout"
        # 第一次调用 — 记录首次 hold 时间，不卖出
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.02))
        self.assertEqual(len(exits), 0, "首次应被 CertaintyHold 拦截")
        # 模拟超时：将 _certainty_hold_since 设为 9 小时前（> 默认 8h）
        import time as _time

        from src.simulation import outcome_tracker as _ot

        _ot._certainty_hold_since["test_ch_timeout"] = _time.time() - 9 * 3600
        # 第二次调用 — 超时，应放行卖出
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.02))
        self.assertEqual(len(exits), 1, "超时后应放行卖出")
        self.assertEqual(exits[0]["trigger"], "certainty_hold_timeout")
        # 清理后 dict 中不再有该 bet_id
        self.assertNotIn("test_ch_timeout", _ot._certainty_hold_since)

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_timeout_disabled_by_zero(self, mock_get):
        """T235: certainty_hold_max_hours=0 时禁用超时（永远等待结算）"""

        def cfg_with_zero_timeout(key, default=None):
            if key == "profit_taking":
                base = self._cfg()(key)
                base["certainty_hold_max_hours"] = 0
                return base
            return self._cfg()(key, default)

        mock_get.side_effect = cfg_with_zero_timeout
        bet = self._make_bet(outcome="NO", category="weather")
        bet["bet_id"] = "test_ch_no_timeout"
        # 模拟已等待 100 小时
        import time as _time

        from src.simulation import outcome_tracker as _ot

        _ot._certainty_hold_since["test_ch_no_timeout"] = _time.time() - 100 * 3600
        exits = self.tracker.check_profit_take_exits([bet], market_data=self._mkt(0.02))
        self.assertEqual(len(exits), 0, "max_hours=0 时应永远等待结算，不超时")
        _ot._certainty_hold_since.pop("test_ch_no_timeout", None)

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_certainty_hold_persists_across_restart(self, mock_get):
        """T235追加: CertaintyHold 首次 hold 时间应持久化到 DB，重启后恢复"""
        mock_get.side_effect = self._cfg()
        bet = self._make_bet(outcome="NO", category="weather")
        bet["bet_id"] = "test_ch_persist"
        from src.simulation import outcome_tracker as _ot

        # 确保干净状态
        _ot._certainty_hold_since.pop("test_ch_persist", None)
        # 首次调用 — 应记录 hold 时间到内存
        with unittest.mock.patch("src.simulation.outcome_tracker.db") as mock_db:
            exits = self.tracker.check_profit_take_exits(
                [bet], market_data=self._mkt(0.02)
            )
            self.assertEqual(len(exits), 0)
            self.assertIn("test_ch_persist", _ot._certainty_hold_since)
            # 验证 DB 写入被调用（持久化）
            mock_db.update_certainty_hold_since.assert_called_once()
            call_args = mock_db.update_certainty_hold_since.call_args
            self.assertEqual(call_args[0][0], "test_ch_persist")
            self.assertIsNotNone(call_args[0][1])  # ISO timestamp
        # 清理
        _ot._certainty_hold_since.pop("test_ch_persist", None)


class TestWeatherTimeWeightedStopLoss(unittest.TestCase):
    """Phase51: 天气赛道时间加权止损测试。

    验证：远离结算(>12h)用宽松阈值(45%)，中期(4-12h)用中阈值(30%)，
    近结算(<4h)用严格阈值(20%)。禁用时回退到weather_standard_stop_pct。
    """

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_weather_bet(self, entry_price, end_date_str):
        import time

        return {
            "bet_id": "test_weather_tw",
            "market_id": "mkt_weather_tw",
            "question": "Will the high temp in NYC be 75°F?",
            "outcome": "YES",
            "market_price": str(entry_price),
            "ts_entered": time.time() - 3600,  # 1h ago (past min_hold)
            "category": "weather",
            "market_end_date": end_date_str,
            "bet_bucket": "",
            "ai_probability": 0.65,
            "source": "auto",
            "ai_reasoning": "",
            "size_usdc": 3.0,
        }

    def _make_market_data(self, price):
        return {
            "mkt_weather_tw": {
                "tokens": [
                    {"outcome": "Yes", "price": price},
                    {"outcome": "No", "price": round(1 - price, 4)},
                ],
                "outcomePrices": f'["{price}", "{round(1 - price, 4)}"]',
            }
        }

    def _tw_config(self, tw_enabled=True, far_stop=0.45, mid_stop=0.30, near_stop=0.20):
        """Build config with time-weighted stop block."""

        def cfg(key, default=None):
            mapping = {
                "stop_loss": {
                    "enabled": True,
                    "floor_price": 0.05,
                    "standard_stop_pct": 0.25,
                    "weather_standard_stop_pct": 0.30,
                    "weather_edge_floor_price": 0.03,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.30,
                    "reversal_exit_price": 0.15,
                    "sports_standard_stop_pct": 0.35,
                    "early_stop": {"enabled": False},
                    "liquidity_guard": {"enabled": False},
                    "stoploss_mode": {"default": "market", "per_category": {}},
                    "weather_time_weighted_stop": {
                        "enabled": tw_enabled,
                        "far_hours": 12,
                        "far_stop_pct": far_stop,
                        "mid_hours": 4,
                        "mid_stop_pct": mid_stop,
                        "near_stop_pct": near_stop,
                    },
                },
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.10,
                    "standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.15,
                },
                "profit_take": {"min_hold_minutes": 5},
            }
            return mapping.get(key, default)

        return cfg

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_far_from_expiry_wide_stop(self, mock_get):
        """>12h to expiry → far_stop_pct(45%), 小幅亏损不触发止损"""
        mock_get.side_effect = self._tw_config(tw_enabled=True, far_stop=0.45)

        from datetime import datetime, timedelta, timezone

        # 24h from now
        end_dt = datetime.now(timezone.utc) + timedelta(hours=24)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_weather_bet(entry_price=0.50, end_date_str=end_str)
        # Price dropped 30% → 0.35. With far_stop=45%, should NOT trigger
        market_data = self._make_market_data(0.35)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(
            len(stops), 0, "30% drop with 45% far threshold should NOT trigger"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_far_from_expiry_deep_loss_triggers(self, mock_get):
        """>12h to expiry, 但亏损超50% → 应触发止损"""
        mock_get.side_effect = self._tw_config(tw_enabled=True, far_stop=0.45)

        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=24)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_weather_bet(entry_price=0.50, end_date_str=end_str)
        # Price dropped ~50% → 0.25. With far_stop=45%, should trigger
        market_data = self._make_market_data(0.25)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(
            len(stops), 0, "50% drop exceeds 45% far threshold → trigger"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_mid_term_mid_stop(self, mock_get):
        """4-12h to expiry → mid_stop_pct(30%), 25%亏损不触发"""
        mock_get.side_effect = self._tw_config(tw_enabled=True, mid_stop=0.30)

        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_weather_bet(entry_price=0.50, end_date_str=end_str)
        # Price dropped 25% → 0.375. With mid_stop=30%, should NOT trigger
        market_data = self._make_market_data(0.375)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(
            len(stops), 0, "25% drop within 30% mid threshold should NOT trigger"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_near_expiry_tight_stop(self, mock_get):
        """<4h to expiry → near_stop_pct(20%), 25%亏损应触发"""
        mock_get.side_effect = self._tw_config(tw_enabled=True, near_stop=0.20)

        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_weather_bet(entry_price=0.50, end_date_str=end_str)
        # Price dropped 25% → 0.375. With near_stop=20%, should trigger
        market_data = self._make_market_data(0.375)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertGreater(
            len(stops), 0, "25% drop exceeds 20% near threshold → trigger"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_disabled_falls_back_to_standard(self, mock_get):
        """时间加权禁用时使用固定 weather_standard_stop_pct"""
        mock_get.side_effect = self._tw_config(tw_enabled=False)

        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=24)
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        bet = self._make_weather_bet(entry_price=0.50, end_date_str=end_str)
        # Price dropped 26% → 0.37. weather_standard_stop_pct=0.30 → NOT trigger
        market_data = self._make_market_data(0.37)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(
            len(stops), 0, "26% drop within 30% standard weather threshold"
        )

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_no_expiry_date_falls_back(self, mock_get):
        """无 market_end_date 时 hours_to_expiry 为 None → 回退到固定阈值"""
        mock_get.side_effect = self._tw_config(tw_enabled=True)

        bet = self._make_weather_bet(entry_price=0.50, end_date_str="")
        # Price dropped 26%. weather_standard_stop_pct=0.30 → NOT trigger
        market_data = self._make_market_data(0.37)

        stops = self.tracker.check_stop_loss_exits([bet], market_data=market_data)
        self.assertEqual(len(stops), 0, "No end_date, 26% drop within 30% fallback")


class TestStationMapExpansion(unittest.TestCase):
    """Phase51: 验证 _STATION_MAP 覆盖主要全球城市。"""

    def test_station_map_minimum_coverage(self):
        from src.utils.weather_fetcher import _STATION_MAP

        self.assertGreaterEqual(
            len(_STATION_MAP), 80, "Station map should have 80+ entries"
        )

    def test_major_us_cities_covered(self):
        from src.utils.weather_fetcher import _STATION_MAP

        for city in [
            "new york",
            "los angeles",
            "chicago",
            "houston",
            "phoenix",
            "san francisco",
            "seattle",
            "miami",
            "boston",
            "denver",
        ]:
            self.assertIn(
                city, _STATION_MAP, f"US city {city} missing from station map"
            )

    def test_major_global_cities_covered(self):
        from src.utils.weather_fetcher import _STATION_MAP

        for city in [
            "london",
            "paris",
            "tokyo",
            "sydney",
            "berlin",
            "amsterdam",
            "dubai",
            "istanbul",
            "bangkok",
            "mumbai",
        ]:
            self.assertIn(city, _STATION_MAP, f"Global city {city} missing")

    def test_station_map_entry_format(self):
        """Every entry must be (ICAO, lat, lon, name) tuple."""
        from src.utils.weather_fetcher import _STATION_MAP

        for city, info in _STATION_MAP.items():
            self.assertEqual(len(info), 4, f"{city}: expected 4-tuple, got {len(info)}")
            icao, lat, lon, name = info
            self.assertIsInstance(icao, str)
            self.assertGreaterEqual(len(icao), 3, f"{city}: ICAO too short")
            self.assertIsInstance(lat, (int, float))
            self.assertIsInstance(lon, (int, float))
            self.assertTrue(-90 <= lat <= 90, f"{city}: lat out of range {lat}")
            self.assertTrue(-180 <= lon <= 180, f"{city}: lon out of range {lon}")


class TestWUHeaders(unittest.TestCase):
    """Phase51: 验证 WU 请求头包含现代浏览器指纹字段。"""

    def test_chrome_user_agent(self):
        from src.utils.weather_fetcher import _HEADERS

        ua = _HEADERS.get("User-Agent", "")
        self.assertIn("Chrome/131", ua, "Should use Chrome 131+ user agent")

    def test_sec_ch_ua_headers_present(self):
        from src.utils.weather_fetcher import _HEADERS

        self.assertIn("sec-ch-ua", _HEADERS)
        self.assertIn("sec-ch-ua-mobile", _HEADERS)
        self.assertIn("sec-ch-ua-platform", _HEADERS)

    def test_sec_fetch_headers_present(self):
        from src.utils.weather_fetcher import _HEADERS

        self.assertIn("Sec-Fetch-Dest", _HEADERS)
        self.assertIn("Sec-Fetch-Mode", _HEADERS)
        self.assertIn("Sec-Fetch-Site", _HEADERS)

    def test_referer_set(self):
        from src.utils.weather_fetcher import _HEADERS

        self.assertIn("Referer", _HEADERS)
        self.assertIn("wunderground.com", _HEADERS["Referer"])


class TestSelfResolutionBehavior(unittest.TestCase):
    """自解析行为：DRY RUN允许自解析，实盘模式跳过（防幽灵仓位）。"""

    @patch("src.utils.config_loader.is_dry_run", return_value=True)
    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_dry_run_self_resolve_works(
        self, mock_update, mock_fetch, mock_open, _mock_dry
    ):
        """DRY RUN模式下自解析正常工作。"""
        import time

        bet = {
            "bet_id": "dry-001",
            "market_id": "mkt-001",
            "outcome": "YES",
            "question": "Will it rain?",
            "market_end_date": "2026-03-20T00:00:00Z",
            "ghost_reopened_at": None,
            "market_price": 0.5,
            "size_usdc": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "mkt-001": {
                "resolved": False,
                "closed": True,
                "endDate": "2026-03-20T00:00:00Z",
                "outcomePrices": "[0.95, 0.05]",
            }
        }

        tracker = OutcomeTracker()
        with patch.object(
            tracker, "_self_resolve_weather_market", return_value=""
        ), patch.object(
            tracker, "_resolve_by_outcome_prices", return_value="YES"
        ), patch.object(
            tracker, "_validate_self_resolution", return_value=True
        ):
            updated = tracker.check_and_update()
        assert mock_update.called

    @patch("src.utils.config_loader.is_dry_run", return_value=False)
    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_live_mode_skips_self_resolve(
        self, mock_update, mock_fetch, mock_open, _mock_dry
    ):
        """实盘模式下不自解析（防止DB标记关闭但token未卖出的幽灵仓位）。"""
        bet = {
            "bet_id": "live-001",
            "market_id": "mkt-002",
            "outcome": "YES",
            "question": "Will it rain?",
            "market_end_date": "2026-03-20T00:00:00Z",
            "ghost_reopened_at": None,
            "market_price": 0.5,
            "size_usdc": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "mkt-002": {
                "resolved": False,
                "closed": True,
                "endDate": "2026-03-20T00:00:00Z",
                "outcomePrices": "[0.95, 0.05]",
            }
        }

        tracker = OutcomeTracker()
        updated = tracker.check_and_update()
        assert updated == 0
        mock_update.assert_not_called()

    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_official_resolution_always_works(self, mock_update, mock_fetch, mock_open):
        """平台官方已解决的市场应直接关闭（不受实盘/DRY RUN限制）。"""
        import time

        bet = {
            "bet_id": "resolve-003",
            "market_id": "mkt-003",
            "outcome": "YES",
            "question": "Weather bet",
            "market_end_date": "2026-03-20T00:00:00Z",
            "ghost_reopened_at": time.time() - 30,
            "market_price": 0.5,
            "size_usdc": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "mkt-003": {
                "resolved": True,
                "outcome": "YES",
                "outcomePrices": "[1.0, 0.0]",
                "resolutionTime": "2026-03-20T12:00:00Z",
            }
        }

        tracker = OutcomeTracker()
        updated = tracker.check_and_update()
        assert mock_update.called


class TestOracleTerminalPriceResolution(unittest.TestCase):
    """测试 Bug 修复：oracle 终态价格（closed+prices=[0,1]）实盘关闭逻辑。"""

    @patch("src.utils.config_loader.is_dry_run", return_value=False)
    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_oracle_terminal_prices_closes_in_live_mode(
        self, mock_update, mock_fetch, mock_open, _mock_dry
    ):
        """实盘模式下，closed=True + outcomePrices=['0','1'] 应触发 oracle 终态关闭。"""
        import time

        bet = {
            "bet_id": "d2aae25a-dead",
            "market_id": "1721287",
            "outcome": "YES",
            "question": "UFC YES bet",
            "market_end_date": "2026-04-05T03:59:59Z",
            "ghost_reopened_at": None,
            "market_price": 0.5,
            "size_usdc": 5.0,
            "matched_shares": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "1721287": {
                "resolved": None,
                "closed": True,
                "endDate": "2026-04-05T03:59:59Z",
                "resolutionTime": None,
                "outcomePrices": '["0","1"]',  # oracle 终态：YES=0, NO=1
                "outcomes": '["Yes","No"]',
            }
        }

        tracker = OutcomeTracker()
        updated = tracker.check_and_update()
        assert updated == 1, f"应触发1次结算，实际 updated={updated}"
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["resolved_outcome"] == "NO"  # NO 胜出
        assert call_kwargs["is_win"] in (False, 0)  # YES仓位输了（SQLite存int）

    @patch("src.utils.config_loader.is_dry_run", return_value=False)
    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_oracle_terminal_prices_no_wins(
        self, mock_update, mock_fetch, mock_open, _mock_dry
    ):
        """oracle 终态 YES=1,NO=0 时，YES仓位应该赢。"""
        bet = {
            "bet_id": "bet-yes-win",
            "market_id": "mkt-w1",
            "outcome": "YES",
            "question": "Test YES win",
            "market_end_date": "2026-04-05T03:59:59Z",
            "ghost_reopened_at": None,
            "market_price": 0.5,
            "size_usdc": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "mkt-w1": {
                "resolved": None,
                "closed": True,
                "endDate": "2026-04-05T03:59:59Z",
                "outcomePrices": '["1","0"]',  # YES 胜出
                "outcomes": '["Yes","No"]',
            }
        }

        tracker = OutcomeTracker()
        updated = tracker.check_and_update()
        assert updated == 1
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["resolved_outcome"] == "YES"
        assert call_kwargs["is_win"] in (True, 1)  # YES仓位赢了（SQLite存int）

    @patch("src.utils.config_loader.is_dry_run", return_value=False)
    @patch("src.utils.db.get_open_paper_bets")
    @patch.object(OutcomeTracker, "_fetch_market_statuses")
    @patch("src.utils.db.update_paper_bet_outcome")
    def test_ambiguous_prices_still_skip_in_live_mode(
        self, mock_update, mock_fetch, mock_open, _mock_dry
    ):
        """非终态 outcomePrices (0.95/0.05) 在实盘模式下应保持跳过（保持原行为）。"""
        bet = {
            "bet_id": "bet-ambig",
            "market_id": "mkt-ambig",
            "outcome": "YES",
            "question": "Ambiguous market",
            "market_end_date": "2026-04-05T03:59:59Z",
            "ghost_reopened_at": None,
            "market_price": 0.5,
            "size_usdc": 5.0,
        }
        mock_open.return_value = [bet]
        mock_fetch.return_value = {
            "mkt-ambig": {
                "resolved": None,
                "closed": True,
                "endDate": "2026-04-05T03:59:59Z",
                "outcomePrices": '["0.05","0.95"]',  # 有争议，非终态
                "outcomes": '["Yes","No"]',
            }
        }

        tracker = OutcomeTracker()
        updated = tracker.check_and_update()
        assert updated == 0, "非终态价格在实盘下不应自动关闭"
        mock_update.assert_not_called()


class TestT70BelowMinSharesExemption(unittest.TestCase):
    """T70 低价暂停行为验证：dead positions (sell_fail>=3, price<0.05) 无论 shares 大小
    均进入 T70 暂停，避免无效 API spam。below_min 仓位等待 oracle 终态价格在
    check_and_update 中被处理，而非通过 CLOB 卖出路径。
    """

    def _make_dead_bet(self, matched_shares, sell_fail_count):
        return {
            "bet_id": "fea6e31a-test",
            "market_id": "1721191",
            "outcome": "NO",
            "question": "UFC NO test",
            "market_end_date": "2026-04-05T03:59:59Z",
            "ts_entered": 0,
            "market_price": 0.45,
            "size_usdc": 2.0,
            "matched_shares": matched_shares,
            "sell_fail_count": sell_fail_count,
            "category": "sports",
        }

    @patch("src.utils.config_loader.get")
    def test_below_min_dead_enters_t70_pause(self, mock_get):
        """below_min_order_size 的死仓 sell_fail>=3 → 应进入 T70 暂停，等待 oracle 结算。"""
        mock_get.side_effect = lambda key, default=None: {
            "stop_loss": {
                "floor_stop_pct": 0.05,
                "sports_live_floor": 0.05,
                "expiry_stop_loss_pct": 0.25,
                "standard_stop_pct": 0.4,
                "reversal_stop_pct": 0.15,
                "reversal_entry_min": 0.4,
                "game_window_hours": 4,
                "post_expiry_window_hours": 2,
                "stoploss_mode": {"default": "market"},
            },
            "position_management": {
                "dead_position_max_sell_retries": 3,
                "dead_position_price_threshold": 0.05,
            },
            "executor.min_order_size_shares": 5.0,
        }.get(key, default)

        open_bets = [self._make_dead_bet(matched_shares=4.9, sell_fail_count=43)]
        market_data = {
            "1721191": {
                "outcomePrices": '["0.9997","0.0003"]',
                "bestBid": None,
                "bestAsk": None,
            }
        }

        tracker = OutcomeTracker()
        tracker._dead_paused_bets = set()
        stops = tracker.check_stop_loss_exits(open_bets, market_data=market_data)

        assert (
            "fea6e31a-test" in tracker._dead_paused_bets
        ), "dead + below_min 仓位应进入 T70 暂停，等待 oracle 结算"
        assert len(stops) == 0, "T70 暂停后不应在 stops 中，避免 API spam"

    @patch("src.utils.config_loader.get")
    def test_normal_dead_position_also_paused(self, mock_get):
        """shares >= min_order_size 的正常死仓，sell_fail >= 3 时也进入 T70 暂停。"""
        mock_get.side_effect = lambda key, default=None: {
            "stop_loss": {
                "floor_stop_pct": 0.05,
                "sports_live_floor": 0.05,
                "expiry_stop_loss_pct": 0.25,
                "standard_stop_pct": 0.4,
                "reversal_stop_pct": 0.15,
                "reversal_entry_min": 0.4,
                "game_window_hours": 4,
                "post_expiry_window_hours": 2,
                "stoploss_mode": {"default": "market"},
            },
            "position_management": {
                "dead_position_max_sell_retries": 3,
                "dead_position_price_threshold": 0.05,
            },
            "executor.min_order_size_shares": 5.0,
        }.get(key, default)

        open_bets = [self._make_dead_bet(matched_shares=10.0, sell_fail_count=5)]
        market_data = {
            "1721191": {
                "outcomePrices": '["0.9997","0.0003"]',
                "bestBid": None,
                "bestAsk": None,
            }
        }

        tracker = OutcomeTracker()
        tracker._dead_paused_bets = set()
        stops = tracker.check_stop_loss_exits(open_bets, market_data=market_data)

        assert (
            "fea6e31a-test" in tracker._dead_paused_bets
        ), "shares >= min 的死仓应进入 T70 暂停"
        assert len(stops) == 0


# ── T228追加: _resolve_exit_order_type 测试 ──────────────────────────────


class TestResolveExitOrderType(unittest.TestCase):
    """退出费率应使用 sell_mode 配置，而非入场 order_type。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_sports_buy_maker_sell_taker(self, mock_get):
        """sports buy=maker 入场、sell_mode=taker → 退出费率按 taker 计算"""
        mock_get.return_value = {
            "default": "maker",
            "per_category": {"weather": "taker", "sports": "taker", "crypto": "taker"},
        }
        from src.simulation.outcome_tracker import _resolve_exit_order_type

        result = _resolve_exit_order_type("sports", "maker")
        self.assertEqual(result, "taker")

    @patch("src.simulation.outcome_tracker.get")
    def test_weather_taker_consistent(self, mock_get):
        """weather buy=taker 入场、sell_mode=taker → 退出仍然 taker"""
        mock_get.return_value = {
            "default": "maker",
            "per_category": {"weather": "taker"},
        }
        from src.simulation.outcome_tracker import _resolve_exit_order_type

        result = _resolve_exit_order_type("weather", "taker")
        self.assertEqual(result, "taker")

    @patch("src.simulation.outcome_tracker.get")
    def test_simulated_preserved(self, mock_get):
        """DRY RUN (simulated) 保持不变"""
        from src.simulation.outcome_tracker import _resolve_exit_order_type

        result = _resolve_exit_order_type("weather", "simulated")
        self.assertEqual(result, "simulated")
        mock_get.assert_not_called()

    @patch("src.simulation.outcome_tracker.get")
    def test_unknown_category_falls_back_to_default(self, mock_get):
        """未知赛道回退到 sell_mode.default"""
        mock_get.return_value = {
            "default": "maker",
            "per_category": {"weather": "taker"},
        }
        from src.simulation.outcome_tracker import _resolve_exit_order_type

        result = _resolve_exit_order_type("unknown_cat", "taker")
        self.assertEqual(result, "maker")

    @patch("src.simulation.outcome_tracker.get")
    def test_empty_config_fallback_taker(self, mock_get):
        """配置完全缺失时默认 taker"""
        mock_get.return_value = None
        from src.simulation.outcome_tracker import _resolve_exit_order_type

        result = _resolve_exit_order_type("sports", "maker")
        self.assertEqual(result, "taker")


# ---------------------------------------------------------------------------
# T231: WeatherExitGate — 天气智能退出门控
# ---------------------------------------------------------------------------


class TestParseWeatherQuestion(unittest.TestCase):
    """验证 _parse_weather_question 正确解析城市、阈值和类型。"""

    def test_highest_between_range(self):
        q = "Will the highest temperature in Los Angeles be between 70-71°F on April 15?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["city"], "Los Angeles")
        self.assertEqual(result["temp_low"], 70.0)
        self.assertEqual(result["temp_high"], 71.0)
        self.assertEqual(result["metric"], "highest")
        self.assertEqual(result["unit"], "F")

    def test_lowest_between_range(self):
        q = "Will the lowest temperature in Chicago be between 30-35°F on April 10?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["city"], "Chicago")
        self.assertEqual(result["temp_low"], 30.0)
        self.assertEqual(result["temp_high"], 35.0)
        self.assertEqual(result["metric"], "lowest")
        self.assertEqual(result["unit"], "F")

    def test_above_single_value(self):
        q = "Will the highest temperature in NYC be above 80°F on April 12?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["city"], "NYC")
        self.assertEqual(result["temp_low"], 80.0)
        self.assertEqual(result["temp_high"], 80.0)
        self.assertEqual(result["metric"], "highest")

    def test_non_weather_returns_none(self):
        q = "Will Bitcoin reach $100,000 by December?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNone(result)

    def test_empty_returns_none(self):
        result = OutcomeTracker._parse_weather_question("")
        self.assertIsNone(result)

    def test_en_dash_separator(self):
        q = "Will the highest temperature in Miami be between 85–90°F on April 20?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["temp_low"], 85.0)
        self.assertEqual(result["temp_high"], 90.0)

    def test_celsius_single_value(self):
        """摄氏度市场解析 — 'be 16°C'"""
        q = "Will the highest temperature in Munich be 16°C on April 16?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["city"], "Munich")
        self.assertEqual(result["temp_low"], 16.0)
        self.assertEqual(result["temp_high"], 16.0)
        self.assertEqual(result["unit"], "C")
        self.assertEqual(result["metric"], "highest")

    def test_celsius_range(self):
        """摄氏度区间解析 — 'between 24-25°C'"""
        q = "Will the highest temperature in Buenos Aires be between 24-25°C on April 15?"
        result = OutcomeTracker._parse_weather_question(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["temp_low"], 24.0)
        self.assertEqual(result["temp_high"], 25.0)
        self.assertEqual(result["unit"], "C")


class TestWeatherExitGate(unittest.TestCase):
    """验证 _weather_exit_gate 基于实时温度的门控逻辑。"""

    def setUp(self):
        self.tracker = OutcomeTracker()

    def _make_weather_bet(
        self, question, outcome="NO", entry=0.64, bet_id="test-gate-01"
    ):
        return {
            "bet_id": bet_id,
            "question": question,
            "outcome": outcome,
            "market_price": entry,
            "category": "weather",
        }

    # 温度测试统一config: profit_hold_pct=1.0 禁用价格短路，隔离温度逻辑
    _TEMP_CFG_F = {
        "profit_taking.weather_exit_gate_enabled": True,
        "profit_taking.weather_exit_gate_safe_margin_f": 2,
        "profit_taking.weather_exit_gate_safe_margin_c": 1,
        "profit_taking.weather_exit_gate_profit_hold_pct": 1.0,
    }
    _TEMP_CFG_C = dict(_TEMP_CFG_F)  # °C 与 °F 使用相同config

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_hold_when_temp_far_below_threshold(self, mock_temp, mock_get):
        """实时温度远低于阈值时（NO安全），应阻止卖出。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        # 当前实时温度 18°C ≈ 64.4°F，阈值 70°F，间距 5.6°F > 2°F
        mock_temp.return_value = 18.0

        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "hold")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_sell_when_temp_near_threshold(self, mock_temp, mock_get):
        """实时温度接近阈值时（NO不安全），应放行卖出。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        # 当前实时温度 21°C ≈ 69.8°F，距阈值 70°F 仅 0.2°F < 2°F
        mock_temp.return_value = 21.0

        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_sell_on_fetch_failure(self, mock_temp, mock_get):
        """无法获取温度时（安全默认），应放行卖出。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        mock_temp.return_value = None

        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_sell_on_unparseable_question(self, mock_get):
        """无法解析标题时，应放行卖出。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)

        bet = self._make_weather_bet(
            "Will it rain in Seattle on April 15?", outcome="NO"
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_yes_hold_when_temp_in_range(self, mock_temp, mock_get):
        """YES仓位，实时温度已接近/进入区间时，应阻止卖出。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        # 当前实时温度 21°C ≈ 69.8°F，阈值 70-71°F，距阈值下限 0.2°F < 2°F → YES安全
        mock_temp.return_value = 21.0

        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="YES",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "trailing")
        self.assertEqual(result, "hold")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_realtime_only_not_daily_max(self, mock_temp, mock_get):
        """核心BUG验证: 门控必须使用实时温度，不使用当日最高温。
        场景: 中午最高68°F(过去式)，但现在温度已回落到63°F。
        用63°F判断: 间距 = 70 - 63 = 7°F ≥ 2°F → 应HOLD。
        """
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        # 实时温度 17.2°C ≈ 63°F（下午温度已回落）
        mock_temp.return_value = 17.2

        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        # 63°F 实时，间距 = 70 - 63 = 7°F ≥ 2°F → HOLD
        self.assertEqual(result, "hold")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_celsius_market_hold(self, mock_temp, mock_get):
        """摄氏度市场: 实时温度远低于阈值，使用独立safe_margin_c → HOLD。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_C.get(k, default)
        # 实时温度 12°C，阈值 16°C，间距 = 4°C > 1°C(safe_margin_c) → HOLD
        mock_temp.return_value = 12.0

        bet = self._make_weather_bet(
            "Will the highest temperature in Munich be 16°C on April 16?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "hold")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_celsius_market_sell(self, mock_temp, mock_get):
        """摄氏度市场: 实时温度接近阈值，间距 < safe_margin_c → SELL。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_C.get(k, default)
        # 实时温度 15.5°C，阈值 16°C，间距 = 0.5°C < 1°C(safe_margin_c) → SELL
        mock_temp.return_value = 15.5

        bet = self._make_weather_bet(
            "Will the highest temperature in Munich be 16°C on April 16?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "sell")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_lowest_no_hold(self, mock_temp, mock_get):
        """最低温度NO: 实时温度远高于阈值上限 → HOLD。"""
        mock_get.side_effect = lambda k, default=None: self._TEMP_CFG_F.get(k, default)
        # 实时温度 10°C = 50°F，阈值 30-35°F，间距 = 50 - 35 = 15°F > 2°F → HOLD
        mock_temp.return_value = 10.0

        bet = self._make_weather_bet(
            "Will the lowest temperature in Chicago be between 30-35°F on April 10?",
            outcome="NO",
        )
        result = self.tracker._weather_exit_gate(bet, 0.82, "floor_protection")
        self.assertEqual(result, "hold")

    # ── 价格恢复短路测试 ──────────────────────────

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    def test_profit_hold_shortcircuit(self, mock_get):
        """仓位仍在显著盈利时，直接HOLD不调温度API。"""
        mock_get.side_effect = lambda k, default=None: {
            "profit_taking.weather_exit_gate_profit_hold_pct": 0.15,
        }.get(k, default)
        # entry=0.64, current=0.88 → gain=37.5% > 15% → 短路HOLD
        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
            entry=0.64,
        )
        result = self.tracker._weather_exit_gate(bet, 0.88, "floor_protection")
        self.assertEqual(result, "hold")

    @unittest.mock.patch("src.simulation.outcome_tracker.get")
    @unittest.mock.patch("src.utils.weather_fetcher.fetch_realtime_temperature")
    def test_profit_hold_bypass_when_gain_low(self, mock_temp, mock_get):
        """仓位盈利不足时，不触发短路，进入温度检查。"""
        mock_get.side_effect = lambda k, default=None: {
            "profit_taking.weather_exit_gate_profit_hold_pct": 0.15,
            "profit_taking.weather_exit_gate_safe_margin_f": 2,
            "profit_taking.weather_exit_gate_safe_margin_c": 1,
        }.get(k, default)
        # entry=0.64, current=0.70 → gain=9.4% < 15% → 不短路，继续温度检查
        # 温度 21°C ≈ 69.8°F，阈值 70°F，间距 0.2°F < 2°F → SELL
        mock_temp.return_value = 21.0
        bet = self._make_weather_bet(
            "Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
            entry=0.64,
        )
        result = self.tracker._weather_exit_gate(bet, 0.70, "floor_protection")
        self.assertEqual(result, "sell")


if __name__ == "__main__":
    unittest.main()
if __name__ == "__main__":
    unittest.main()
